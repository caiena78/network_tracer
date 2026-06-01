"""
gunicorn.conf.py — production server configuration for Network Tracer API.

Usage:
    gunicorn -c gunicorn.conf.py tracer_api.main:app

Install gunicorn:
    pip install gunicorn

IMPORTANT — single-worker requirement
--------------------------------------
The application uses in-process shared state:
  - tracer_api.task_store.task_store    (in-memory task registry)
  - tracer_api.cache.*                  (in-memory TTL caches)

Multiple gunicorn WORKERS (processes) would NOT share this state and would
cause requests to land on the wrong worker.  Keep workers=1 unless you
move the task store and result cache to Redis.

Concurrency is achieved via the thread pool inside the single process
(TRACER_MAX_CONCURRENT_TRACES controls the pool size).  The async event
loop handles the rest.
"""

import multiprocessing
import os

# ── Server socket ─────────────────────────────────────────────────────────────
bind    = f"{os.getenv('TRACER_HOST', '0.0.0.0')}:{os.getenv('TRACER_PORT', '8000')}"
backlog = 512                    # max pending connections

# ── Workers ───────────────────────────────────────────────────────────────────
# KEEP AT 1 unless using Redis-backed shared state (see note above).
workers    = 1
worker_class = "uvicorn.workers.UvicornWorker"

# ── Timeouts ──────────────────────────────────────────────────────────────────
# Traces can run for up to 15 minutes; set well above that.
timeout        = 1800   # 30 min hard kill
keepalive      = 65     # seconds to keep idle connections open
graceful_timeout = 60   # wait this long for workers to finish on reload

# ── Performance ───────────────────────────────────────────────────────────────
worker_connections = 1000   # max concurrent connections per worker
threads            = 1      # uvicorn workers are async; extra threads not needed

# ── Logging ───────────────────────────────────────────────────────────────────
accesslog  = "-"        # stdout
errorlog   = "-"        # stderr
loglevel   = "info"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s %(D)s µs'

# ── Process naming ────────────────────────────────────────────────────────────
proc_name    = "tracer-api"
daemon       = False
preload_app  = True      # load the app before forking workers (faster start)

# ── Reload (dev only — disable in production) ─────────────────────────────────
reload       = False
