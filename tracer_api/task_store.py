"""
tracer_api/task_store.py
========================
Thread-safe, in-memory task registry.

Each trace job lives as a TraceTask.  Background threads update task state and
push progress events; async SSE handlers consume them via per-task asyncio.Queue
instances.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

from .models import TraceStatus


_SENTINEL = object()   # posted to subscriber queues to signal stream end


class TraceTask:
    """Holds the mutable state of one trace job."""

    def __init__(self, trace_id: str, src_ip: str, dst_ip: str) -> None:
        self.trace_id   = trace_id
        self.src_ip     = src_ip
        self.dst_ip     = dst_ip
        self.status     = TraceStatus.PENDING
        self.created_at = datetime.now(timezone.utc)
        self.updated_at = datetime.now(timezone.utc)
        self.progress:  List[str]            = []
        self.result:    Optional[List[dict]] = None   # flat paths
        self.error:     Optional[str]        = None
        self.finished_at: Optional[datetime] = None

        # asyncio event loop reference — set when a coroutine posts the first event
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock  = threading.Lock()
        # One queue per active SSE subscriber
        self._queues: List[asyncio.Queue] = []

    # ------------------------------------------------------------------
    # State mutations (called from background threads)
    # ------------------------------------------------------------------

    def _touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc)

    def set_running(self) -> None:
        with self._lock:
            self.status = TraceStatus.RUNNING
            self._touch()

    def set_topology(self, flat_paths: List[dict]) -> None:
        """Topology phase complete — store sparse flat paths and signal the frontend to draw."""
        with self._lock:
            self.result  = flat_paths
            self.status  = TraceStatus.ENRICHING
            self._touch()
        self._broadcast_event({"type": "topology", "data": flat_paths})

    def broadcast_enrichment(self, event: dict) -> None:
        """Broadcast a single interface enrichment event without changing task state."""
        self._broadcast_event(event)

    def set_completed(self, result: List[dict]) -> None:
        with self._lock:
            self.status       = TraceStatus.COMPLETED
            self.result       = result
            self.finished_at  = datetime.now(timezone.utc)
            self._touch()
        self._broadcast_sentinel()

    def set_failed(self, error: str) -> None:
        with self._lock:
            self.status      = TraceStatus.FAILED
            self.error       = error
            self.finished_at = datetime.now(timezone.utc)
            self._touch()
        self._broadcast_sentinel()

    def set_cancelled(self) -> None:
        with self._lock:
            self.status      = TraceStatus.CANCELLED
            self.finished_at = datetime.now(timezone.utc)
            self._touch()
        self._broadcast_sentinel()

    def add_progress(self, message: str) -> None:
        """Append a progress line and push it to all SSE subscribers."""
        with self._lock:
            self.progress.append(message)
            self._touch()
        self._broadcast_event({"type": "progress", "message": message})

    # ------------------------------------------------------------------
    # SSE subscriber management (called from async handlers)
    # ------------------------------------------------------------------

    def subscribe(
        self,
        loop: asyncio.AbstractEventLoop,
    ) -> asyncio.Queue:
        """Register a new SSE subscriber.  Returns a queue to pull events from."""
        q: asyncio.Queue = asyncio.Queue()
        with self._lock:
            if self._loop is None:
                self._loop = loop
            self._queues.append(q)

            # If the task is already done, immediately push all missed progress
            # and then the sentinel so the stream closes at once.
            if self.status in (
                TraceStatus.COMPLETED,
                TraceStatus.FAILED,
                TraceStatus.CANCELLED,
            ):
                for msg in self.progress:
                    q.put_nowait({"type": "progress", "message": msg})
                q.put_nowait({"type": "done", "status": self.status.value})
                q.put_nowait(_SENTINEL)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            try:
                self._queues.remove(q)
            except ValueError:
                pass

    # ------------------------------------------------------------------
    # Internal broadcast helpers
    # ------------------------------------------------------------------

    def _broadcast_event(self, event: dict) -> None:
        """Push *event* to every subscriber queue (thread-safe)."""
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        with self._lock:
            queues = list(self._queues)
        for q in queues:
            try:
                loop.call_soon_threadsafe(q.put_nowait, event)
            except Exception:
                pass

    def _broadcast_sentinel(self) -> None:
        """Signal all subscribers that the stream is finished."""
        event = {"type": "done", "status": self.status.value}
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        with self._lock:
            queues = list(self._queues)
        for q in queues:
            try:
                loop.call_soon_threadsafe(q.put_nowait, event)
                loop.call_soon_threadsafe(q.put_nowait, _SENTINEL)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Duration helper
    # ------------------------------------------------------------------

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.created_at).total_seconds()


# ---------------------------------------------------------------------------
# TaskStore — the singleton registry
# ---------------------------------------------------------------------------

class TaskStore:
    """Thread-safe registry of all trace tasks."""

    def __init__(self, ttl_seconds: int = 3600) -> None:
        self._tasks: Dict[str, TraceTask] = {}
        self._ttl   = ttl_seconds
        self._lock  = threading.Lock()

    def create(self, src_ip: str, dst_ip: str) -> TraceTask:
        trace_id = str(uuid.uuid4())
        task     = TraceTask(trace_id, src_ip, dst_ip)
        with self._lock:
            self._tasks[trace_id] = task
        return task

    def get(self, trace_id: str) -> Optional[TraceTask]:
        with self._lock:
            return self._tasks.get(trace_id)

    def list_all(self) -> List[TraceTask]:
        with self._lock:
            return list(self._tasks.values())

    def delete(self, trace_id: str) -> bool:
        with self._lock:
            return self._tasks.pop(trace_id, None) is not None

    def evict_expired(self) -> int:
        """Remove tasks older than *ttl_seconds*.  Returns the eviction count."""
        now = datetime.now(timezone.utc)
        expired = []
        with self._lock:
            for tid, task in self._tasks.items():
                age = (now - task.created_at).total_seconds()
                if age > self._ttl and task.status not in (
                    TraceStatus.PENDING, TraceStatus.RUNNING
                ):
                    expired.append(tid)
            for tid in expired:
                del self._tasks[tid]
        return len(expired)

    @property
    def active_count(self) -> int:
        with self._lock:
            return sum(
                1 for t in self._tasks.values()
                if t.status in (TraceStatus.PENDING, TraceStatus.RUNNING)
            )

    @property
    def total_count(self) -> int:
        with self._lock:
            return len(self._tasks)


# Module-level singleton shared across the app.
task_store = TaskStore()

# Expose the sentinel so the SSE generator can recognise end-of-stream.
SENTINEL = _SENTINEL
