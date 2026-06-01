#!/usr/bin/env python3
"""
run_api.py  (inside tracer_api/)
=================================
Standalone launcher for the Network Tracer API.

Run from the folder that CONTAINS tracer_api/ — i.e., the standalone root:

    cd /path/to/standalone_root
    python tracer_api/run_api.py

Or, if tracer_api/ is already the working directory:

    python run_api.py

Environment variables (or .env file in the standalone root):
    TRACER_HOST             default 0.0.0.0
    TRACER_PORT             default 8000
    TRACER_RELOAD           default false  (set to true for development only)
    TRACER_TOOLS_PATH       path to the folder containing network_tracer.py and
                            its siblings (cisco_device_client.py, etc.).
                            If not set, the standalone root itself is checked first,
                            then any PYTHONPATH entries already on sys.path.
    (see .env.example for the full list)
"""

from __future__ import annotations

import os
import sys

# ---------------------------------------------------------------------------
# sys.path bootstrap
# ---------------------------------------------------------------------------
# 1. Add the standalone root (one level above tracer_api/) so that Python can
#    find network_tracer.py when it lives there.
_THIS_DIR   = os.path.dirname(os.path.abspath(__file__))
_STANDALONE = os.path.dirname(_THIS_DIR)   # one level up from tracer_api/
if _STANDALONE not in sys.path:
    sys.path.insert(0, _STANDALONE)

# 2. Change CWD to the standalone root so .env is loaded from the right place.
os.chdir(_STANDALONE)

# 3. If TRACER_TOOLS_PATH is set (or network_tracer.py isn't in _STANDALONE),
#    add that directory too so the import resolves correctly.
#    This lets the network-tracing modules live in a separate folder without
#    copying them into the project root.
_tools_path = os.environ.get("TRACER_TOOLS_PATH", "").strip()
if _tools_path and os.path.isdir(_tools_path) and _tools_path not in sys.path:
    sys.path.insert(0, _tools_path)
elif not os.path.exists(os.path.join(_STANDALONE, "network_tracer.py")):
    # Auto-detect: walk up looking for a sibling folder that contains network_tracer.py
    _parent = os.path.dirname(_STANDALONE)
    for _candidate in os.listdir(_parent):
        _cand_path = os.path.join(_parent, _candidate)
        if (
            os.path.isdir(_cand_path)
            and os.path.exists(os.path.join(_cand_path, "network_tracer.py"))
            and _cand_path not in sys.path
        ):
            sys.path.insert(0, _cand_path)
            break

import uvicorn
from tracer_api.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "tracer_api.main:app",
        host      = settings.host,
        port      = settings.port,
        reload    = settings.reload,
        log_level = "info",
    )
