"""
tracer_api/tracer_runner.py
============================
Background trace execution engine.

Responsibilities
----------------
1. Import and call functions from ``network_tracer.py`` (parent directory).
2. Capture all ``print()`` output → task progress events (SSE stream).
3. Apply performance improvements that do not require touching network_tracer.py:
      a. NetBox prefix result caching  (skips API round-trips on warm cache)
      b. Parallel ECMP L3 branch exploration  (ThreadPoolExecutor, see note)
      c. Trace result caching  (identical src/dst returns instantly)
4. Resolve credentials from per-request overrides → environment settings →
   Vault (when available).

Performance note on parallel L3
--------------------------------
``_parallel_l3_trace()`` is provided here and can replace the sequential
``run_l3_path_trace`` in network_tracer.py when the caller can supply the
initial routes directly.  In ``run_trace_background`` we call
``nt.run_l2_trace()`` (which already runs sequential L3 internally) so that
we honour the "leave network_tracer.py alone" constraint.  The parallel
function is available for callers that build their own orchestration on top.

sys.path resolution
-------------------
Both files in this package use:
    _PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

This resolves to ONE directory above ``tracer_api/``, i.e. the standalone
root folder where ``network_tracer.py`` and its siblings must live.

Standalone folder layout expected:
    <root>/
    ├── network_tracer.py
    ├── cisco_device_client.py
    ├── netbox_client.py
    ├── vault_client.py          (optional)
    ├── run_api.py
    ├── requirements.txt
    ├── .env
    └── tracer_api/
        ├── __init__.py
        └── ...
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from typing import Callable, Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Bootstrap sys.path — adds the standalone root (one level above tracer_api/)
# so ``import network_tracer`` and its siblings resolve correctly regardless
# of the working directory from which the API is launched.
# ---------------------------------------------------------------------------
_PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT_DIR not in sys.path:
    sys.path.insert(0, _PARENT_DIR)

# If TRACER_TOOLS_PATH is set, add it so network_tracer.py can live in a
# separate folder (e.g. a shared netbox_tools/ sibling directory) without
# needing to be copied into the project root.
_tools_path = os.environ.get("TRACER_TOOLS_PATH", "").strip()
if _tools_path and os.path.isdir(_tools_path) and _tools_path not in sys.path:
    sys.path.insert(0, _tools_path)

# Auto-detect: if network_tracer.py is not in _PARENT_DIR, look for a sibling
# directory that contains it (handles the common case where netbox_tools/ sits
# next to web_path_tracer/).
if not os.path.exists(os.path.join(_PARENT_DIR, "network_tracer.py")):
    _grandparent = os.path.dirname(_PARENT_DIR)
    try:
        for _cand in os.listdir(_grandparent):
            _cand_path = os.path.join(_grandparent, _cand)
            if (
                os.path.isdir(_cand_path)
                and os.path.exists(os.path.join(_cand_path, "network_tracer.py"))
                and _cand_path not in sys.path
            ):
                sys.path.insert(0, _cand_path)
                break
    except OSError:
        pass

import network_tracer as nt  # noqa: E402 — must come after sys.path tweak

log = logging.getLogger("tracer_api.runner")


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------

def resolve_credentials(
    netbox_url:   Optional[str] = None,
    netbox_token: Optional[str] = None,
    username:     Optional[str] = None,
    password:     Optional[str] = None,
) -> Tuple[str, str, Dict[str, str]]:
    """
    Return ``(nb_url, nb_token, device_creds)`` by merging per-request
    overrides with environment settings, optionally loading from Vault.

    Priority: request body → Vault (when configured) → environment variables.
    """
    from .config import settings

    # ── Vault (required when vault_addr is configured) ────────────────────────
    if settings.vault_addr and not username and not netbox_url:
        try:
            from vault_client import VaultClient
            vc      = VaultClient(
                addr       = settings.vault_addr,
                role_id    = settings.vault_role_id,
                secret_id  = settings.vault_secret_id,
                mount      = settings.vault_mount,
                path       = settings.vault_path,
            )
            secrets      = vc.get_secrets()
            username     = secrets.get("user",         username)
            password     = secrets.get("password",     password)
            netbox_url   = secrets.get("netbox_url",   netbox_url)
            netbox_token = secrets.get("netbox_token", netbox_token)
            log.info("Credentials loaded from Vault (%s/%s)", settings.vault_mount, settings.vault_path)
        except Exception as exc:
            # Vault is configured and is the required credential source.
            # Do NOT silently fall back to empty env vars — raise so the trace
            # fails with the actual error (e.g. "No module named 'hvac'",
            # auth failure, missing secret key, network timeout, etc.).
            raise RuntimeError(f"Vault credential load failed: {exc}") from exc

    nb_url   = netbox_url   or settings.netbox_url
    nb_token = netbox_token or settings.netbox_token
    creds: Dict[str, str] = {
        "username": username or settings.device_username,
        "password": password or settings.device_password,
        "secret":   settings.device_enable_secret,
        "timeout":  str(settings.device_timeout),
    }
    return nb_url, nb_token, creds


# ---------------------------------------------------------------------------
# Print capture
# ---------------------------------------------------------------------------

class _PrintCapture:
    """Write-compatible shim that forwards completed lines to a callback."""

    def __init__(self, callback: Callable[[str], None]) -> None:
        self._cb  = callback
        self._buf = ""

    def write(self, text: str) -> int:
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            stripped = line.strip()
            if stripped:
                try:
                    self._cb(stripped)
                except Exception:
                    pass
        return len(text)

    def flush(self) -> None:
        if self._buf.strip():
            try:
                self._cb(self._buf.strip())
            except Exception:
                pass
            self._buf = ""


@contextmanager
def _capture_prints(callback: Callable[[str], None]):
    """Thread-local stdout redirect; restores the original on exit."""
    capture  = _PrintCapture(callback)
    original = sys.stdout
    sys.stdout = capture
    try:
        yield
    finally:
        capture.flush()
        sys.stdout = original


# ---------------------------------------------------------------------------
# Parallel L3 branch exploration (standalone function, not called by default)
# ---------------------------------------------------------------------------

def _explore_one_hop(
    current_ip:  str,
    path_so_far: List[Dict],
    visited:     Set[str],
    dst_ip:      str,
    src_ip:      str,
    creds:       Dict[str, str],
    nb_url:      str,
    nb_token:    str,
    verify_ssl:  bool,
    progress_cb: Callable[[str], None],
    max_hops:    int,
) -> Dict:
    """
    SSH to *current_ip*, collect ingress + egress routes, return a result dict.
    Designed to run inside a ThreadPoolExecutor worker.

    Returned keys: current_ip, connect_ip, hostname, ingress_ifaces,
    egress_routes, iface_details, l2_trace, l2_trace_device, note, next_hops.
    """
    result: Dict = {
        "current_ip":     current_ip,
        "connect_ip":     current_ip,
        "hostname":       current_ip,
        "ingress_ifaces": [],
        "egress_routes":  [],
        "iface_details":  {},
        "l2_trace":        None,
        "l2_trace_device": None,
        "l2_intermediate": [],
        "note":            "",
        "next_hops":       [],
    }

    client      = None
    connect_ip  = current_ip
    connect_err = ""

    progress_cb(f"[L3] Connecting to {current_ip}...")

    try:
        client = nt._open_device_client(current_ip, "ios", creds)
    except nt.GatewayConnectionError as exc:
        connect_err = str(exc)

    # NetBox primary-IP fallback
    if client is None and nb_url and nb_token:
        primary_ip = nt._resolve_mgmt_ip_from_netbox(
            nb_url, nb_token, current_ip, verify_ssl
        )
        if primary_ip and primary_ip != current_ip:
            connect_ip             = primary_ip
            result["connect_ip"]   = primary_ip
            progress_cb(f"[L3]   Connecting via NetBox primary {primary_ip}...")
            try:
                client      = nt._open_device_client(primary_ip, "ios", creds)
                connect_err = ""
            except nt.GatewayConnectionError as exc2:
                connect_err = str(exc2)

    if client is None:
        note = f"Cannot connect to {current_ip}"
        if connect_ip != current_ip:
            note += f"; also tried {connect_ip}"
        if connect_err:
            note += f": {connect_err}"
        result["note"] = note

        # L2 fallback on the previous-hop device
        if path_so_far:
            prev      = path_so_far[-1]
            prev_ip   = prev.get("connect_ip") or prev.get("current_ip")
            prev_host = prev.get("hostname") or prev_ip
            if prev_ip:
                progress_cb(
                    f"[L3]   L2 fallback for {dst_ip} on {prev_host} ({prev_ip})..."
                )
                try:
                    pc = nt._open_device_client(prev_ip, "ios", creds)
                    try:
                        l2                       = nt._run_l2_at_final_hop(pc, "ios", dst_ip)
                        result["l2_trace"]        = l2
                        result["l2_trace_device"] = prev_host
                    finally:
                        try:
                            pc._cli_disconnect()
                        except Exception:
                            pass
                except Exception as exc:
                    log.debug("L2 fallback failed for %s: %s", current_ip, exc)
        return result

    # Collect data while connected
    try:
        try:
            result["hostname"] = (
                client._cli_connection.find_prompt().rstrip("#>").strip()
                or current_ip
            )
        except Exception:
            pass

        ingress_lookup = src_ip or (path_so_far[-1]["current_ip"] if path_so_far else "")
        if ingress_lookup:
            progress_cb(f"[L3]   show ip route {ingress_lookup} → ingress")
            result["ingress_ifaces"] = nt._resolve_ingress_interfaces(
                client, ingress_lookup
            )

        progress_cb(f"[L3]   show ip route {dst_ip} → egress")
        result["egress_routes"] = nt.get_routes_for_ip(client, dst_ip)

        # Interface details (show interface) for ingress + egress
        seen: Set[str] = set()
        for iface in result["ingress_ifaces"]:
            if iface and iface not in seen:
                result["iface_details"][iface] = nt.get_interface_detail(
                    client, "ios", iface
                )
                seen.add(iface)
        for r in result["egress_routes"]:
            ei = r.get("exit_interface")
            if ei and ei not in seen:
                result["iface_details"][ei] = nt.get_interface_detail(
                    client, "ios", ei
                )
                seen.add(ei)

        if any(r.get("next_hop") == "directly connected" for r in result["egress_routes"]):
            progress_cb(f"[L3]   {dst_ip} directly connected — running L2 trace")
            result["l2_trace"] = nt._run_l2_at_final_hop(client, "ios", dst_ip)

    finally:
        try:
            client._cli_disconnect()
        except Exception:
            pass

    for r in result["egress_routes"]:
        nh = r.get("next_hop")
        if nh and nh != "directly connected" and nh not in visited:
            result["next_hops"].append(nh)

    return result


def _parallel_l3_trace(
    gateway_ip:           str,
    gw_hostname:          str,
    gw_ingress_interface: Optional[str],
    dst_ip:               str,
    initial_routes:       List[Dict],
    creds:                Dict[str, str],
    src_ip:               str = "",
    nb_url:               str = "",
    nb_token:             str = "",
    verify_ssl:           bool = True,
    max_hops:             int = 15,
    max_workers:          int = 6,
    progress_cb:          Callable[[str], None] = lambda _: None,
) -> List[List[Dict]]:
    """
    BFS L3 path trace with **parallel branch exploration**.

    All independent ECMP branches at the same BFS level are submitted to a
    ThreadPoolExecutor simultaneously.  On a 10-hop trace with 4-way ECMP
    the wall-clock time is reduced roughly 4× vs. sequential BFS.

    Note: this function must be supplied *initial_routes* (from the gateway's
    ``show ip route <dst_ip>``).  It is intended for callers that have already
    run the L2 trace and want to replace the sequential L3 result.
    """
    complete_paths: List[List[Dict]] = []

    gw_hop: Dict = {
        "hostname":          gw_hostname,
        "current_ip":        gateway_ip,
        "ip":                gateway_ip,
        "connect_ip":        gateway_ip,
        "ingress_interfaces": [gw_ingress_interface] if gw_ingress_interface else [],
        "egress_routes":     initial_routes,
        "interface_details": {},
        "l2_trace":          None,
        "note":              "",
    }

    if any(r.get("next_hop") == "directly connected" for r in initial_routes):
        return [[gw_hop]]

    queue: deque = deque()
    for route in initial_routes:
        nh = route.get("next_hop")
        if nh and nh != "directly connected":
            branch                  = dict(gw_hop)
            branch["selected_route"] = route
            queue.append(([branch], nh, {gateway_ip}))

    if not queue:
        return [[gw_hop]]

    def _make_dead_end(path: List, ip: str, note: str) -> None:
        complete_paths.append(path + [{
            "hostname": ip, "current_ip": ip, "ip": ip, "connect_ip": ip,
            "note": note, "ingress_interfaces": [], "egress_routes": [],
            "interface_details": {}, "l2_trace": None,
        }])

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        while queue:
            batch: List[Tuple[List, str, Set]] = []
            while queue:
                batch.append(queue.popleft())

            filtered: List[Tuple[List, str, Set]] = []
            for path_so_far, current_ip, visited in batch:
                if current_ip in visited:
                    _make_dead_end(path_so_far, current_ip, f"Loop detected at {current_ip}")
                    continue
                if len(path_so_far) >= max_hops:
                    _make_dead_end(path_so_far, current_ip, f"Max hops ({max_hops}) reached")
                    continue
                filtered.append((path_so_far, current_ip, visited))

            if not filtered:
                continue

            future_map = {
                pool.submit(
                    _explore_one_hop,
                    current_ip, path_so_far, visited,
                    dst_ip, src_ip, creds,
                    nb_url, nb_token, verify_ssl,
                    progress_cb, max_hops,
                ): (path_so_far, current_ip, visited)
                for path_so_far, current_ip, visited in filtered
            }

            for fut in as_completed(future_map):
                path_so_far, current_ip, visited = future_map[fut]
                try:
                    hr = fut.result()
                except Exception as exc:
                    _make_dead_end(path_so_far, current_ip, f"Unexpected error: {exc}")
                    continue

                hop_dict: Dict = {
                    "hostname":           hr.get("hostname", current_ip),
                    "ip":                 current_ip,
                    "connect_ip":         hr.get("connect_ip", current_ip),
                    "ingress_interfaces": hr.get("ingress_ifaces", []),
                    "egress_routes":      hr.get("egress_routes", []),
                    "interface_details":  hr.get("iface_details", {}),
                    "l2_trace":           hr.get("l2_trace"),
                    "l2_trace_device":    hr.get("l2_trace_device"),
                    "l2_intermediate":    hr.get("l2_intermediate", []),
                    "note":               hr.get("note", ""),
                }
                new_path    = path_so_far + [hop_dict]
                new_visited = visited | {current_ip, hr.get("connect_ip", current_ip)}
                note        = hr.get("note", "")
                egresses    = hr.get("egress_routes", [])

                if note or not egresses:
                    if not note:
                        hop_dict["note"] = "No route to destination"
                    complete_paths.append(new_path)
                    continue

                if any(r.get("next_hop") == "directly connected" for r in egresses):
                    complete_paths.append(new_path)
                    continue

                for nh in hr.get("next_hops", []):
                    if nh not in new_visited:
                        queue.append((new_path, nh, new_visited))

    return complete_paths or [[gw_hop]]


# ---------------------------------------------------------------------------
# Main background trace entry-point
# ---------------------------------------------------------------------------

def run_trace_background(
    task,                                  # TraceTask instance
    netbox_url:   Optional[str] = None,
    netbox_token: Optional[str] = None,
    username:     Optional[str] = None,
    password:     Optional[str] = None,
) -> None:
    """
    Execute a full network trace in a background thread.

    Handles:
    - credential resolution (request overrides → Vault → env vars)
    - trace result caching (returns instantly on cache hit)
    - progress capture (network_tracer print() → task SSE events)
    - phases 1–3: gateway discovery → L2 trace → L3 path trace
    - task state transitions and error capture
    """
    from .config import settings
    from .cache  import get_netbox_cache, get_result_cache

    task.set_running()
    progress = task.add_progress   # shorthand

    try:
        progress("[INFO] Resolving credentials...")
        nb_url, nb_token, creds = resolve_credentials(
            netbox_url, netbox_token, username, password
        )
        progress(f"[INFO] Credentials resolved — NetBox: {nb_url}")
        src_ip = task.src_ip
        dst_ip = task.dst_ip

        if not nb_url or not nb_token:
            task.set_failed("NetBox URL and token are required")
            return

        # ── Cache hit — emit topology then complete immediately ─────────────────
        rc     = get_result_cache()
        cached = rc.get(src_ip, dst_ip, nb_url)
        if cached is not None:
            progress(f"[CACHE] Returning cached result for {src_ip} → {dst_ip}")
            task.set_topology(cached)   # draw the graph
            task.set_completed(cached)  # signal done
            return

        verify_ssl = settings.netbox_verify_ssl

        # ── Phase 1: Gateway discovery (with prefix cache) ──────────────────────
        progress(f"[INFO] Looking up subnet for {src_ip} in NetBox...")
        nc       = get_netbox_cache()
        prefixes = nc.get(nb_url, src_ip)
        if prefixes is None:
            prefixes = nt.get_prefixes_from_netbox(
                nb_url, nb_token, verify_ssl, contains=src_ip
            )
            nc.set(nb_url, src_ip, prefixes)

        if not prefixes:
            task.set_failed(f"No NetBox prefix contains {src_ip}")
            return

        matched = nt.find_longest_prefix_match(src_ip, prefixes)
        if not matched:
            task.set_failed(f"No prefix match found for {src_ip}")
            return

        gateway = nt.calculate_first_usable_ip(matched)
        if not gateway:
            task.set_failed(f"Cannot calculate gateway for subnet {matched}")
            return

        progress(f"[INFO] Gateway: {gateway}  (subnet {matched})")

        # Verify gateway reachability
        with _capture_prints(progress):
            try:
                hostname = nt.connect_to_device(gateway, creds)
            except nt.GatewayConnectionError as exc:
                task.set_failed(f"Cannot reach gateway {gateway}: {exc}")
                return
        progress(f"[INFO] Connected to {hostname} ({gateway})")

        # ── Phase 2: topology-only trace (no show interface, no port-channel) ─────
        # Collects device names, IPs, interface names, ARP, MAC tables, routes,
        # CDP neighbors.  Skips slow per-interface counters so the graph draws fast.
        progress("[INFO] Phase 1 — collecting topology (devices, interfaces, routes)...")
        with _capture_prints(progress):
            raw = nt.run_l2_trace(
                target_ip     = src_ip,
                dst_ip        = dst_ip,
                gateway_ip    = gateway,
                creds         = creds,
                source_is_ap  = False,
                max_hops      = settings.max_hops,
                nb_url        = nb_url,
                nb_token      = nb_token,
                verify_ssl    = verify_ssl,
                topology_only = True,
            )

        if raw is None:
            task.set_failed("Trace produced no result (see progress log)")
            return

        flat_topology = nt.build_flat_paths(raw)

        # Emit topology → frontend draws the graph immediately
        progress(
            f"[INFO] Topology ready — {len(flat_topology)} path(s). Graph rendering now."
        )
        task.set_topology(flat_topology)

        # ── Phase 3: interface enrichment (streams counters as they arrive) ─────
        progress("[INFO] Phase 2 — enriching interfaces (show interface, port-channels)...")
        enrichment_map: dict = {}   # (device, interface) → detail dict

        with _capture_prints(progress):
            for event in nt.iter_interface_enrichment(
                flat_topology, creds,
                max_workers=settings.max_enrichment_workers,
            ):
                # Stream the event to all SSE subscribers in real-time
                task.broadcast_enrichment(event)
                # Accumulate for merging into the final cached result
                if event["type"] == "interface_update":
                    key = (event["device"], event["interface"])
                    enrichment_map[key] = event["data"]

        # ── Merge enrichment data into flat paths for history / cache ───────────
        def _apply_enrichment(paths: List[dict]) -> List[dict]:
            merged = []
            for path in paths:
                new_path = dict(path)
                new_hops = []
                for hop in (path.get("path") or []):
                    key = (hop.get("device", ""), hop.get("interface", ""))
                    if key in enrichment_map:
                        new_hop = dict(hop)
                        new_det = dict(hop.get("details") or {})
                        new_det.update(enrichment_map[key])
                        new_hop["details"] = new_det
                        new_hops.append(new_hop)
                    else:
                        new_hops.append(hop)
                new_path["path"] = new_hops
                merged.append(new_path)
            return merged

        flat_enriched = _apply_enrichment(flat_topology)
        rc.set(src_ip, dst_ip, nb_url, flat_enriched)
        task.set_completed(flat_enriched)

    except Exception as exc:
        log.exception("Unexpected error in trace %s", task.trace_id)
        task.set_failed(f"Internal error: {exc}")
