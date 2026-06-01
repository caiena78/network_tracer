"""
tracer_api/graph_builder.py
============================
Convert the flat-path list produced by network_tracer.build_flat_paths()
into a Cytoscape.js-compatible graph structure.

Graph anatomy
-------------
* A dedicated **source endpoint** node (node_type "src") is prepended to every
  path, representing the host at src_ip.  The first switch hop is no longer
  mis-typed as "src".
* Every unique device becomes a **node** (deduplication by device name).
* Consecutive hops produce an **edge**; the source endpoint ↔ first-switch
  edge carries the switch-port interface details (speed, duplex, errors, etc.).
* Multiple ECMP paths that share the same device/link are deduplicated in the
  elements list but tracked per-path in ``paths``.

Node types
----------
  src       — the source host endpoint (synthetic node for src_ip)
  switch    — L2 device (Gi/Te/Twe/Fa/Eth interface prefix)
  router    — L3 device
  dst       — last device in the trace path
  unknown   — cannot be inferred

NetBox URL fields (when netbox_url is non-empty)
------------------------------------------------
  node.data.netbox_url
  edge.data.src_interface_netbox_url
  edge.data.dst_interface_netbox_url
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set
from urllib.parse import quote as _urlencode


# ---------------------------------------------------------------------------
# Node-type helpers
# ---------------------------------------------------------------------------

_SWITCH_PREFIXES = re.compile(
    r"^(Gi|Fa|Te|Twe|Fo|Hu|Eth|GigabitEthernet|FastEthernet"
    r"|TenGigabitEthernet|TwentyFiveGigE|HundredGigE|Ethernet)",
    re.IGNORECASE,
)


def _infer_node_type(layer: str, interface: str, hop_index: int, total_hops: int) -> str:
    """
    Infer device node type.
    hop_index == 0 is NO LONGER typed as "src"  — dedicated source endpoint node.
    hop_index == total_hops-1 is NO LONGER "dst" — dedicated destination endpoint node.
    Both endpoint nodes are created separately in build_graph().
    """
    if layer == "L3":
        return "router"
    if layer == "L2":
        if _SWITCH_PREFIXES.match(interface or ""):
            return "switch"
    return "unknown"


# ---------------------------------------------------------------------------
# NetBox URL helpers
# ---------------------------------------------------------------------------

def _device_url(netbox_url: str, device: str) -> str:
    return f"{netbox_url.rstrip('/')}/dcim/devices/?name={_urlencode(device)}"


def _interface_url(netbox_url: str, device: str, interface: str) -> str:
    return (
        f"{netbox_url.rstrip('/')}/dcim/interfaces/"
        f"?device={_urlencode(device)}&name={_urlencode(interface)}"
    )


# ---------------------------------------------------------------------------
# Interface detail fields copied onto edge data
# ---------------------------------------------------------------------------

_IFACE_COUNTER_FIELDS = (
    "state",
    "description",
    "speed",
    "duplex",
    "vlan",
    "runts",
    "giants",
    "crc",
    "input_error",
    "rx_runts",
    "rx_crc",
    "output_error",
    "tx_output_error",
    "total_output_drops",
    "output_discard",
    "unknown_protocol_drops",
)


# ---------------------------------------------------------------------------
# Core builder
# ---------------------------------------------------------------------------

def build_graph(
    flat_paths: List[Dict[str, Any]],
    netbox_url: str = "",
) -> Dict[str, Any]:
    """
    Convert *flat_paths* into a Cytoscape.js-compatible elements list.

    Parameters
    ----------
    flat_paths : list[dict]
        Each entry is one ECMP path:
          src_ip, dst_ip, gateway_ip, path: [{layer, device, interface, details}]
    netbox_url : str
        Base URL of the NetBox instance for generating clickable links.

    Returns
    -------
    dict  { elements, paths, metadata }
    """
    elements: List[Dict[str, Any]] = []
    node_ids:  Set[str] = set()
    edge_ids:  Set[str] = set()
    paths_out: List[Dict[str, Any]] = []

    src_ip     = flat_paths[0]["src_ip"]        if flat_paths else ""
    dst_ip     = flat_paths[0]["dst_ip"]        if flat_paths else ""
    gateway_ip = flat_paths[0].get("gateway_ip", "")

    # ── Dedicated source-endpoint node (one per graph, not per hop) ──────────
    src_host_id = f"host::{src_ip}"
    if src_ip and src_host_id not in node_ids:
        node_ids.add(src_host_id)
        elements.append({"data": {
            "id":        src_host_id,
            "label":     src_ip,
            "node_type": "src",
            "layer":     "L2",
            "ip":        src_ip,
        }})

    # ── Dedicated destination-endpoint node (one per graph, not per hop) ─────
    dst_host_id = f"host::{dst_ip}"
    if dst_ip and dst_host_id not in node_ids:
        node_ids.add(dst_host_id)
        elements.append({"data": {
            "id":        dst_host_id,
            "label":     dst_ip,
            "node_type": "dst",
            "layer":     "L2",
            "ip":        dst_ip,
        }})

    # ── Node-deduplication helper ─────────────────────────────────────────────
    def _ensure_node(
        device:    str,
        layer:     str,
        interface: str,
        hop_idx:   int,
        total:     int,
        details:   Dict,
    ) -> str:
        node_id   = f"device::{device}"
        node_type = _infer_node_type(layer, interface, hop_idx, total)
        if node_id not in node_ids:
            node_ids.add(node_id)
            node_data: Dict[str, Any] = {
                "id":        node_id,
                "label":     device,
                "node_type": node_type,
                "layer":     layer,
            }
            if hop_idx == total - 1:
                node_data["ip"] = dst_ip
            for key in ("state", "description", "speed", "duplex"):
                if key in details:
                    node_data[key] = details[key]
            if netbox_url:
                node_data["netbox_url"] = _device_url(netbox_url, device)
            elements.append({"data": node_data})
        return node_id

    # ── Edge-data builder ─────────────────────────────────────────────────────
    def _make_edge(
        edge_id:     str,
        src_node:    str,
        tgt_node:    str,
        edge_layer:  str,
        src_dev:     str,
        dst_dev:     str,
        src_iface:   Optional[str],
        dst_iface:   Optional[str],
        src_details: Dict,
        dst_details: Dict,
    ) -> Dict[str, Any]:
        ed: Dict[str, Any] = {
            "id":            edge_id,
            "source":        src_node,
            "target":        tgt_node,
            "label":         _edge_label(src_iface, dst_iface),
            "layer":         edge_layer,
            "src_device":    src_dev,
            "dst_device":    dst_dev,
            "src_interface": src_iface or None,
            "dst_interface": dst_iface or None,
        }
        # Prefer src-side values, fall back to dst-side for each counter
        for cf in _IFACE_COUNTER_FIELDS:
            val = src_details.get(cf)
            if val is None:
                val = dst_details.get(cf)
            if val is not None:
                ed[cf] = val
        if netbox_url:
            if src_iface:
                ed["src_interface_netbox_url"] = _interface_url(netbox_url, src_dev, src_iface)
            if dst_iface:
                ed["dst_interface_netbox_url"] = _interface_url(netbox_url, dst_dev, dst_iface)
        # Raw show-interface output — kept separate from numeric counter fields.
        if src_details.get("raw_output"):
            ed["src_raw_output"] = src_details["raw_output"]
        if dst_details.get("raw_output"):
            ed["dst_raw_output"] = dst_details["raw_output"]
        return ed

    # ── Per-path processing ───────────────────────────────────────────────────
    for path_idx, flat_path in enumerate(flat_paths):
        hops       = flat_path.get("path", [])
        n_hops     = len(hops)
        path_edges: List[str] = []

        if n_hops == 0:
            paths_out.append({
                "path_id": path_idx, "edge_ids": [],
                "src_ip": flat_path.get("src_ip", ""),
                "dst_ip": flat_path.get("dst_ip", ""),
                "gateway_ip": flat_path.get("gateway_ip", ""),
                "ecmp_variant": path_idx,
            })
            continue

        first_hop     = hops[0]
        first_dev     = first_hop.get("device")    or "unknown_0"
        first_iface   = first_hop.get("interface") or ""
        first_layer   = first_hop.get("layer")     or "L2"
        first_details = first_hop.get("details")   or {}

        # Ensure the first real device node exists (typed as switch/router, NOT src)
        first_node_id = _ensure_node(
            first_dev, first_layer, first_iface, 0, n_hops, first_details
        )

        # Edge: source endpoint → first switch (carries the switch-port details)
        src_edge_id = f"edge::source::{first_dev}::{first_iface}"
        path_edges.append(src_edge_id)
        if src_edge_id not in edge_ids:
            edge_ids.add(src_edge_id)
            elements.append({"data": _make_edge(
                edge_id     = src_edge_id,
                src_node    = src_host_id,
                tgt_node    = first_node_id,
                edge_layer  = "L2",
                src_dev     = src_ip,
                dst_dev     = first_dev,
                src_iface   = None,
                dst_iface   = first_iface,
                src_details = {},
                dst_details = first_details,
            )})

        # Edges between consecutive real hops
        for i, hop in enumerate(hops):
            device    = hop.get("device")    or f"unknown_{i}"
            interface = hop.get("interface") or ""
            layer     = hop.get("layer")     or "L2"
            details   = hop.get("details")   or {}

            _ensure_node(device, layer, interface, i, n_hops, details)

            if i == 0:
                continue  # source→hop0 edge already created above

            prev_hop   = hops[i - 1]
            prev_dev   = prev_hop.get("device")    or f"unknown_{i-1}"
            prev_iface = prev_hop.get("interface") or ""
            prev_layer = prev_hop.get("layer")     or "L2"
            prev_det   = prev_hop.get("details")   or {}

            src_node   = f"device::{prev_dev}"
            tgt_node   = f"device::{device}"
            edge_layer = layer if layer == prev_layer else "mixed"

            canonical = "::".join(sorted([prev_dev, device]) + [prev_iface, interface])
            edge_id   = f"edge::{canonical}"
            path_edges.append(edge_id)

            if edge_id not in edge_ids:
                edge_ids.add(edge_id)
                elements.append({"data": _make_edge(
                    edge_id     = edge_id,
                    src_node    = src_node,
                    tgt_node    = tgt_node,
                    edge_layer  = edge_layer,
                    src_dev     = prev_dev,
                    dst_dev     = device,
                    src_iface   = prev_iface or None,
                    dst_iface   = interface  or None,
                    src_details = prev_det,
                    dst_details = details,
                )})

        # ── Edge: last hop → destination endpoint ────────────────────────────
        # Use the very last hop in the path to represent the port on the final
        # device that faces the destination host.
        if hops and dst_ip:
            last_hop     = hops[-1]
            last_dev     = last_hop.get("device") or f"unknown_{n_hops-1}"
            last_iface   = last_hop.get("interface") or ""
            last_details = last_hop.get("details") or {}
            last_layer   = last_hop.get("layer") or "L2"

            dst_edge_id = f"edge::dst::{last_dev}::{last_iface}::{dst_ip}"
            path_edges.append(dst_edge_id)
            if dst_edge_id not in edge_ids:
                edge_ids.add(dst_edge_id)
                elements.append({"data": _make_edge(
                    edge_id     = dst_edge_id,
                    src_node    = f"device::{last_dev}",
                    tgt_node    = dst_host_id,
                    edge_layer  = last_layer,
                    src_dev     = last_dev,
                    dst_dev     = dst_ip,
                    src_iface   = last_iface or None,
                    dst_iface   = None,
                    src_details = last_details,
                    dst_details = {},
                )})

        paths_out.append({
            "path_id":      path_idx,
            "edge_ids":     path_edges,
            "src_ip":       flat_path.get("src_ip", ""),
            "dst_ip":       flat_path.get("dst_ip", ""),
            "gateway_ip":   flat_path.get("gateway_ip", ""),
            "ecmp_variant": path_idx,
        })

    return {
        "elements": elements,
        "paths":    paths_out,
        "metadata": {
            "src_ip":      src_ip,
            "dst_ip":      dst_ip,
            "gateway_ip":  gateway_ip,
            "total_paths": len(flat_paths),
            "netbox_url":  netbox_url or None,
        },
    }


def _edge_label(src_iface: Optional[str], dst_iface: Optional[str]) -> str:
    parts = [p for p in (src_iface, dst_iface) if p]
    return " → ".join(parts) if parts else ""
