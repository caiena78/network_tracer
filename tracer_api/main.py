"""
tracer_api/main.py
==================
FastAPI application entry-point.

API surface
-----------
  POST   /api/v1/traces                    Start a new trace (non-blocking)
  GET    /api/v1/traces                    List recent traces
  GET    /api/v1/traces/{trace_id}         Full trace status + result
  GET    /api/v1/traces/{trace_id}/graph   Cytoscape.js graph from result
  GET    /api/v1/traces/{trace_id}/stream  SSE stream for live progress
  DELETE /api/v1/traces/{trace_id}         Cancel / delete a trace
  GET    /api/v1/history                   List saved trace history
  GET    /api/v1/history/{entry_id}        Full history entry with graph
  DELETE /api/v1/history/{entry_id}        Delete a history entry
  GET    /api/v1/health                    Liveness check

Run with:
  uvicorn tracer_api.main:app --host 0.0.0.0 --port 8000

Or via the helper script at the repo root:
  python run_api.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

# ---------------------------------------------------------------------------
# Bootstrap sys.path — same calculation used in tracer_runner.py:
#   dirname(dirname(__file__))  resolves to the standalone root, one level
#   above tracer_api/, where network_tracer.py and its siblings must live.
# ---------------------------------------------------------------------------
_HERE   = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from .cache         import sweep_all
from .config        import settings
from .graph_builder import build_graph
from .history       import get_history_db
from .models        import (
    HealthResponse,
    HistoryDetail,
    HistoryListResponse,
    HistorySummary,
    InterfaceDetailRequest,
    InterfaceDetailResponse,
    TraceRequest,
    TraceResponse,
    TraceSummary,
    TraceStatus,
)
from .task_store    import SENTINEL, task_store
from .tracer_runner import run_trace_background

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("tracer_api")

# ---------------------------------------------------------------------------
# Thread pool — SSH is blocking; each trace occupies one worker thread
# ---------------------------------------------------------------------------
_thread_pool = ThreadPoolExecutor(
    max_workers=settings.max_concurrent_traces,
    thread_name_prefix="tracer-",
)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title=settings.api_title,
    version=settings.api_version,
    description=(
        "REST API for the Cisco network path tracer. "
        "Provides hop-by-hop L2/L3 path discovery, ECMP-aware routing, "
        "and Cytoscape.js-compatible topology graphs."
    ),
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# API key guard (optional)
# ---------------------------------------------------------------------------

async def _check_api_key(request: Request) -> None:
    """Dependency: validates the X-API-Key header when api_key is configured."""
    if not settings.api_key:
        return
    key = request.headers.get("X-API-Key", "")
    if key != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key header",
        )


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def _startup() -> None:
    log.info("Network Tracer API starting — version %s", settings.api_version)

    # Initialise history DB (creates file + schema if needed)
    try:
        get_history_db()
        log.info("History DB initialised at %s", settings.history_db_path)
    except Exception:
        log.exception("Failed to initialise history DB — history will be unavailable")

    # Background housekeeping: sweep expired tasks + cache entries every 60s
    async def _housekeeping() -> None:
        while True:
            await asyncio.sleep(60)
            evicted = task_store.evict_expired()
            sweep_all()
            if evicted:
                log.debug("Housekeeping: evicted %d expired tasks", evicted)

    asyncio.create_task(_housekeeping())


@app.on_event("shutdown")
async def _shutdown() -> None:
    log.info("Network Tracer API shutting down")
    _thread_pool.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _task_to_summary(task) -> Dict:
    return {
        "trace_id":   task.trace_id,
        "status":     task.status.value,
        "src_ip":     task.src_ip,
        "dst_ip":     task.dst_ip,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


def _task_to_response(task) -> Dict:
    d = _task_to_summary(task)
    d["progress"]         = list(task.progress)
    d["result"]           = task.result
    d["error"]            = task.error
    d["duration_seconds"] = task.duration_seconds
    return d


def _get_task_or_404(trace_id: str):
    task = task_store.get(trace_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Trace {trace_id!r} not found",
        )
    return task


# ---------------------------------------------------------------------------
# Routes — Traces
# ---------------------------------------------------------------------------

@app.post(
    "/api/v1/traces",
    response_model=TraceSummary,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start a new network trace",
    tags=["traces"],
    dependencies=[Depends(_check_api_key)],
)
async def start_trace(body: TraceRequest, request: Request) -> Dict:
    """
    Submit a new trace job.  Returns ``202 Accepted`` immediately with the
    ``trace_id``; poll ``GET /api/v1/traces/{trace_id}`` or subscribe to the
    SSE stream for progress and results.
    """
    # Enforce concurrency limit
    if task_store.active_count >= settings.max_concurrent_traces:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Maximum concurrent traces ({settings.max_concurrent_traces}) reached. "
                "Wait for an existing trace to complete."
            ),
        )

    task = task_store.create(body.src_ip, body.dst_ip)
    loop = asyncio.get_running_loop()
    effective_netbox_url = body.netbox_url or settings.netbox_url

    def _run() -> None:
        # Bind the task's event loop so SSE broadcasts work from this thread
        task._loop = loop
        run_trace_background(
            task          = task,
            netbox_url    = body.netbox_url,
            netbox_token  = body.netbox_token,
            username      = body.username,
            password      = body.password,
        )
        # Auto-save completed traces to persistent history
        if task.status == TraceStatus.COMPLETED and task.result:
            try:
                graph_data = build_graph(task.result, netbox_url=effective_netbox_url)
                get_history_db().save(
                    src_ip     = task.src_ip,
                    dst_ip     = task.dst_ip,
                    status     = "completed",
                    created_at = task.created_at.isoformat(),
                    duration_s = task.duration_seconds,
                    flat_paths = task.result,
                    graph_json = graph_data,
                )
                log.info("Trace %s saved to history", task.trace_id)
            except Exception:
                log.exception("Failed to save trace %s to history", task.trace_id)

    _thread_pool.submit(_run)

    log.info("Trace %s queued: %s → %s", task.trace_id, body.src_ip, body.dst_ip)
    return _task_to_summary(task)


@app.get(
    "/api/v1/traces",
    response_model=List[TraceSummary],
    summary="List all recent traces",
    tags=["traces"],
    dependencies=[Depends(_check_api_key)],
)
async def list_traces() -> List[Dict]:
    """Return all trace records currently held in memory (newest first)."""
    tasks = sorted(
        task_store.list_all(),
        key=lambda t: t.created_at,
        reverse=True,
    )
    return [_task_to_summary(t) for t in tasks]


@app.get(
    "/api/v1/traces/{trace_id}",
    response_model=TraceResponse,
    summary="Get trace status and result",
    tags=["traces"],
    dependencies=[Depends(_check_api_key)],
)
async def get_trace(trace_id: str) -> Dict:
    """
    Return the full trace record including status, progress log, and (when
    completed) the flat-path result array.
    """
    return _task_to_response(_get_task_or_404(trace_id))


@app.get(
    "/api/v1/traces/{trace_id}/graph",
    summary="Get Cytoscape.js topology graph",
    tags=["traces"],
    dependencies=[Depends(_check_api_key)],
)
async def get_graph(trace_id: str) -> Dict:
    """
    Convert the completed trace result into a Cytoscape.js-compatible graph
    with nodes (devices) and edges (links).  NetBox URLs are injected when
    ``TRACER_NETBOX_URL`` is configured.

    Returns ``404`` if the trace is not found and ``409 Conflict`` if the
    trace has not yet completed.
    """
    task = _get_task_or_404(trace_id)

    if task.status == TraceStatus.FAILED:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Trace failed: {task.error}",
        )
    if task.status in (TraceStatus.PENDING, TraceStatus.RUNNING):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Trace is still {task.status.value}; try again when completed",
        )
    # ENRICHING means topology is ready — return the sparse graph while counters stream in
    if not task.result:
        raise HTTPException(
            status_code=status.HTTP_204_NO_CONTENT,
            detail="Trace completed but produced no path data",
        )

    graph = build_graph(task.result, netbox_url=settings.netbox_url)
    return graph


@app.get(
    "/api/v1/traces/{trace_id}/stream",
    summary="SSE stream of live progress",
    tags=["traces"],
    dependencies=[Depends(_check_api_key)],
)
async def stream_trace(trace_id: str, request: Request) -> StreamingResponse:
    """
    Server-Sent Events (SSE) stream.  Each event is a JSON object:
      ``{"type": "progress", "message": "..."}``
      ``{"type": "done", "status": "completed"|"failed"|"cancelled"}``

    The stream closes automatically when the trace finishes.  Reconnect to
    re-play all buffered progress messages (already-finished traces emit the
    full backlog immediately).
    """
    task = _get_task_or_404(trace_id)
    loop = asyncio.get_running_loop()
    queue = task.subscribe(loop)

    async def _generate() -> AsyncGenerator[str, None]:
        try:
            while True:
                # Respect client disconnect
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # Keep-alive comment so proxies don't close the connection
                    yield ": keep-alive\n\n"
                    continue

                if event is SENTINEL:
                    break

                yield f"data: {json.dumps(event)}\n\n"
        finally:
            task.unsubscribe(queue)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",   # Disable Nginx buffering
        },
    )


@app.delete(
    "/api/v1/traces/{trace_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cancel or delete a trace",
    tags=["traces"],
    dependencies=[Depends(_check_api_key)],
)
async def delete_trace(trace_id: str) -> None:
    """
    Remove a trace record.  If the trace is still running it is marked
    ``cancelled`` (the background thread will not be forcibly killed, but
    its result will be discarded).
    """
    task = _get_task_or_404(trace_id)
    if task.status in (TraceStatus.PENDING, TraceStatus.RUNNING):
        task.set_cancelled()
    task_store.delete(trace_id)
    log.info("Trace %s deleted", trace_id)


# ---------------------------------------------------------------------------
# Routes — Trace History
# ---------------------------------------------------------------------------

@app.get(
    "/api/v1/history",
    response_model=HistoryListResponse,
    summary="List saved trace history",
    tags=["history"],
    dependencies=[Depends(_check_api_key)],
)
async def list_history(
    src_ip: Optional[str] = Query(None, description="Filter by source IP (substring)"),
    dst_ip: Optional[str] = Query(None, description="Filter by destination IP (substring)"),
    q:      Optional[str] = Query(None, description="Free-text search (src/dst IP, timestamp)"),
    limit:  int           = Query(50, ge=1, le=500),
    offset: int           = Query(0, ge=0),
) -> Dict:
    """Return saved trace history, newest first."""
    db      = get_history_db()
    entries = db.list(src_ip=src_ip, dst_ip=dst_ip, q=q, limit=limit, offset=offset)
    total   = db.count(src_ip=src_ip, dst_ip=dst_ip, q=q)
    return {
        "entries": [e.to_summary_dict() for e in entries],
        "total":   total,
        "limit":   limit,
        "offset":  offset,
    }


@app.get(
    "/api/v1/history/{entry_id}",
    response_model=HistoryDetail,
    summary="Get a saved history entry with graph",
    tags=["history"],
    dependencies=[Depends(_check_api_key)],
)
async def get_history_entry(entry_id: str) -> Dict:
    """Return a full history entry including the rendered graph payload."""
    entry = get_history_db().get(entry_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"History entry {entry_id!r} not found",
        )
    return entry.to_detail_dict()


@app.delete(
    "/api/v1/history/{entry_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a history entry",
    tags=["history"],
    dependencies=[Depends(_check_api_key)],
)
async def delete_history_entry(entry_id: str) -> None:
    """Permanently remove a history entry from the database."""
    deleted = get_history_db().delete(entry_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"History entry {entry_id!r} not found",
        )



# ---------------------------------------------------------------------------
# On-demand interface detail
# ---------------------------------------------------------------------------

@app.post(
    "/api/v1/interfaces/detail",
    response_model=InterfaceDetailResponse,
    summary="Fetch live show-interface output for one interface",
    tags=["interfaces"],
    dependencies=[Depends(_check_api_key)],
)
async def get_interface_detail_ondemand(
    body: InterfaceDetailRequest,
) -> Dict:
    """
    SSH to *device_ip*, run ``show interface <interface>``, and return
    the full raw output together with the parsed counter fields.

    This endpoint is used by the frontend "Get interface details" button
    so operators can refresh stale counter data without re-running the
    full trace.
    """
    import ipaddress

    # Basic input validation
    try:
        ipaddress.ip_address(body.device_ip)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{body.device_ip!r} is not a valid IP address",
        )
    if not body.interface.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="interface must not be empty",
        )

    def _run_sync() -> Dict:
        from .tracer_runner import resolve_credentials
        import network_tracer as nt  # available after tracer_runner bootstraps sys.path

        _, _, creds = resolve_credentials()

        try:
            client = nt._open_device_client(body.device_ip, "ios", creds)
        except nt.GatewayConnectionError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Cannot connect to {body.device_ip}: {exc}",
            )

        try:
            detail = nt.get_interface_detail(client, "ios", body.interface)
        finally:
            try:
                client._cli_disconnect()
            except Exception:
                pass

        raw_output = detail.pop("raw_output", "") or ""
        return {
            "device_ip":  body.device_ip,
            "interface":  body.interface,
            "raw_output": raw_output,
            "parsed":     detail,
        }

    loop   = asyncio.get_running_loop()
    result = await loop.run_in_executor(_thread_pool, _run_sync)
    return result


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------

@app.delete(
    "/api/v1/cache",
    status_code=status.HTTP_200_OK,
    summary="Invalidate trace result cache",
    tags=["ops"],
    dependencies=[Depends(_check_api_key)],
)
async def clear_cache(
    src_ip: Optional[str] = Query(None, description="Source IP — required when dst_ip is provided"),
    dst_ip: Optional[str] = Query(None, description="Destination IP — required when src_ip is provided"),
) -> dict:
    """
    Clear cached trace results.

    - **With** ``src_ip`` + ``dst_ip``: removes only that specific pair so the
      next trace runs fresh against the network.
    - **Without** query params: removes **all** cached results.
    """
    from .cache import get_result_cache

    rc = get_result_cache()

    if src_ip and dst_ip:
        # Use invalidate_src_dst so the correct entry is removed regardless of
        # which netbox_url was in effect when the result was cached (e.g. Vault
        # provides the URL at trace time, not from settings.netbox_url).
        removed = rc.invalidate_src_dst(src_ip, dst_ip)
        log.info("Cache invalidated for %s → %s (%d entries)", src_ip, dst_ip, removed)
        return {"cleared": removed, "src_ip": src_ip, "dst_ip": dst_ip}

    cleared = rc.clear_all()
    log.info("Full cache cleared — %d entries removed", cleared)
    return {"cleared": cleared}


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get(
    "/api/v1/health",
    response_model=HealthResponse,
    summary="API liveness / stats",
    tags=["ops"],
)
async def health() -> Dict:
    """Quick liveness check that also reports runtime counters."""
    from .cache import get_netbox_cache, get_result_cache

    return {
        "status":         "ok",
        "api_version":    settings.api_version,
        "active_traces":  task_store.active_count,
        "queued_traces":  task_store.active_count,   # same pool, for parity
        "cached_results": get_result_cache().size,
    }


# ---------------------------------------------------------------------------
# Root redirect
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def root() -> JSONResponse:
    return JSONResponse(
        {"message": "Network Tracer API", "docs": "/api/docs"}
    )
