#!/usr/bin/env python3
"""
network_tracer.py — Phase 1 + 2: Gateway discovery + Layer 2 MAC trace.

Given a source IP address:
  Phase 1:
    1. Find the most specific NetBox prefix that contains it.
    2. Calculate the first usable IP in that subnet (the expected gateway).
    3. Attempt an SSH connection to the gateway and report the device hostname.

  Phase 2 (L2 trace):
    4. ARP lookup on the gateway to resolve the source IP to a MAC address.
    5. Hop-by-hop MAC table lookup starting at the gateway:
         a. Find the VLAN and switchport for the MAC.
         b. Expand port-channels to their physical members.
         c. Check CDP/LLDP on the resolved interface.
         d. If the neighbor is a switch or router, connect to it and repeat.
         e. Stop at APs, VMware hosts, endpoints (no CDP/LLDP), or when
            the neighbor IP cannot be resolved.

Later phases will extend this with routing-table analysis, ECMP parallel
tracing, and full hop-by-hop output.
"""

from __future__ import annotations

import argparse
import io
import ipaddress
import json
import logging
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

try:
    from cisco_device_client import (
        CiscoDeviceClient,
        AuthenticationError as DeviceAuthError,
        TransportError    as DeviceTransportError,
    )
except ImportError:
    print("ERROR: cisco_device_client.py is required in the same directory", file=sys.stderr)
    sys.exit(1)

try:
    from netbox_client import NetBoxClient, NetBoxClientError
except ImportError:
    print("ERROR: netbox_client.py is required in the same directory", file=sys.stderr)
    sys.exit(1)

# Vault is optional — gracefully degrade when vault_client.py is absent.
try:
    from vault_client import (
        VaultClient,
        VaultError,
        add_vault_parser_args,
        is_vault_configured,
        resolve_vault_auth,
    )
    _VAULT_AVAILABLE = True
except ImportError:
    _VAULT_AVAILABLE = False

    class VaultError(Exception):  # type: ignore[no-redef]
        pass

    class VaultClient:  # type: ignore[no-redef]
        pass

    def add_vault_parser_args(*_) -> None:  # type: ignore[misc]
        pass

    def is_vault_configured(*_) -> bool:  # type: ignore[misc]
        return False

    def resolve_vault_auth(*_) -> Tuple[str, str, str]:  # type: ignore[misc]
        raise SystemExit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

LOG_FILE = "network_tracer.log"


def _configure_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)-8s %(name)-25s %(message)s"
    logging.basicConfig(
        level=level,
        format=fmt,
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stderr),
        ],
    )


log = logging.getLogger("network_tracer")


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_VMWARE_KEYWORDS: Tuple[str, ...] = (
    "vmware", "esxi", "vsphere", "vswitch", "vmnic", "esx",
)

_AP_ROLE_KEYWORDS: Tuple[str, ...] = (
    "ap", "access-point", "wireless", "aironet",
    "catalyst-9100", "catalyst-9105", "catalyst-9115",
    "catalyst-9120", "catalyst-9130",
)


# ─────────────────────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────────────────────


class GatewayConnectionError(Exception):
    """Raised when SSH to a network device fails."""


# ─────────────────────────────────────────────────────────────────────────────
# NetBox helpers
# ─────────────────────────────────────────────────────────────────────────────


def _get_nb_client(nb_url: str, nb_token: str, verify_ssl: bool = True) -> NetBoxClient:
    """Return a configured NetBoxClient instance."""
    return NetBoxClient(nb_url, nb_token, verify_ssl=verify_ssl)


def get_prefixes_from_netbox(
    nb_url: str,
    nb_token: str,
    verify_ssl: bool = True,
    contains: Optional[str] = None,
) -> List[str]:
    """Return prefix strings from NetBox IPAM.

    When *contains* is supplied the NetBox ``contains`` filter is used so only
    prefixes that contain that address are fetched.
    """
    try:
        nb = _get_nb_client(nb_url, nb_token, verify_ssl)
        if contains:
            raw = list(nb.nb.ipam.prefixes.filter(contains=contains))
        else:
            raw = list(nb.nb.ipam.prefixes.all())
        prefixes = [str(p.prefix) for p in raw if p.prefix]
        log.debug("Fetched %d prefix(es) from NetBox (contains=%s)", len(prefixes), contains)
        return prefixes
    except NetBoxClientError as exc:
        log.error("NetBox prefix lookup failed: %s", exc)
        return []
    except Exception as exc:
        log.error("NetBox prefix lookup unexpected error: %s", exc)
        return []


def _resolve_mgmt_ip_from_netbox(
    nb_url: str,
    nb_token: str,
    next_hop_ip: str,
    verify_ssl: bool = True,
) -> Optional[str]:
    """Resolve *next_hop_ip* to a Cisco device in NetBox and return its primary IPv4.

    Flow:
      1. Search NetBox IPAM for an IP address record matching *next_hop_ip*.
      2. Follow the assignment from that IP record → device interface → device.
      3. Return the device's ``primary_ip4`` (the management address to SSH to).

    Returns None when the IP is not found in NetBox, is not assigned to a
    device interface, or has no primary IPv4 set.

    Note: virtual-machine interfaces are intentionally skipped — only
    physical Cisco device interfaces are followed.
    """
    try:
        nb = _get_nb_client(nb_url, nb_token, verify_ssl)

        # Step 1 — find the IPAM record for this IP.
        # NetBox stores IPs in CIDR notation; try /32 first, then bare.
        ip_recs: list = []
        for addr in (f"{next_hop_ip}/32", next_hop_ip):
            ip_recs = list(nb.nb.ipam.ip_addresses.filter(address=addr))
            if ip_recs:
                break

        if not ip_recs:
            log.debug("NetBox: no IP address record found for %s", next_hop_ip)
            return None

        rec      = ip_recs[0]
        obj_type = str(getattr(rec, "assigned_object_type", "") or "")
        obj_id   = getattr(rec, "assigned_object_id", None)

        if not obj_id:
            log.debug("NetBox: IP %s exists but is not assigned to any interface", next_hop_ip)
            return None

        # Step 2 — follow to the device that owns this interface.
        if "dcim.interface" not in obj_type:
            log.debug(
                "NetBox: IP %s is assigned to %r (not a device interface) — skipping",
                next_hop_ip, obj_type,
            )
            return None

        iface = nb.nb.dcim.interfaces.get(obj_id)
        if not iface or not iface.device:
            log.debug("NetBox: interface id=%s has no associated device", obj_id)
            return None

        device_id = int(iface.device.id)

        # Step 3 — get the device record and return its primary IPv4.
        devs = nb.get_devices({"id": device_id})
        if not devs:
            log.debug("NetBox: device id=%s not found", device_id)
            return None

        device     = devs[0]
        device_name = device.get("name", "unknown")
        primary_ip  = nb.get_device_mgmt_ip(device)

        if primary_ip:
            print(
                f"[L3]   NetBox: {next_hop_ip} → device '{device_name}' → primary IPv4 {primary_ip}"
            )
        else:
            log.debug("NetBox: device '%s' has no primary IPv4 set", device_name)

        return primary_ip

    except Exception as exc:
        log.debug("NetBox resolution for %s failed: %s", next_hop_ip, exc)
        return None




def resolve_neighbor_ip(neighbor_info: Dict[str, str]) -> Optional[str]:
    """Return the CDP/LLDP-reported IP for this neighbor.

    Uses only the IP advertised by the neighbor itself — no NetBox lookup.
    """
    ip = neighbor_info.get("neighbor_ip")
    log.debug(
        "Resolved neighbor %s -> %s (via CDP/LLDP)",
        neighbor_info.get("neighbor_id", "?"), ip or "None",
    )
    return ip


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 — prefix / gateway helpers
# ─────────────────────────────────────────────────────────────────────────────


def find_longest_prefix_match(ip: str, prefixes: List[str]) -> Optional[str]:
    """Return the most specific prefix (longest prefix-length) containing *ip*."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        log.error("Invalid IP address: %r", ip)
        return None

    best: Optional[ipaddress.IPv4Network | ipaddress.IPv6Network] = None
    for raw in prefixes:
        try:
            net = ipaddress.ip_network(raw, strict=False)
        except ValueError:
            log.debug("Skipping malformed prefix: %r", raw)
            continue
        if addr in net:
            if best is None or net.prefixlen > best.prefixlen:
                best = net

    if best:
        log.debug("Longest prefix match for %s: %s", ip, best)
        return str(best)
    log.debug("No prefix match found for %s", ip)
    return None


def calculate_first_usable_ip(prefix: str) -> Optional[str]:
    """Return the first usable host address in *prefix*.

    /32 or /128 → the address itself; /31 → network address; all others → net+1.
    """
    try:
        net = ipaddress.ip_network(prefix, strict=False)
    except ValueError:
        log.error("Invalid prefix: %r", prefix)
        return None
    if net.num_addresses == 1:
        return str(net.network_address)
    if net.prefixlen >= 31:
        return str(net.network_address)
    return str(net.network_address + 1)


# ─────────────────────────────────────────────────────────────────────────────
# SSH connection helpers
# ─────────────────────────────────────────────────────────────────────────────


def _open_device_client(
    ip: str,
    os_type: str,
    credentials: Dict[str, str],
) -> CiscoDeviceClient:
    """Create a CiscoDeviceClient and open its CLI connection.

    The caller is responsible for calling ``client._cli_disconnect()`` when done.
    Raises :exc:`GatewayConnectionError` on any connection failure.
    """
    try:
        client = CiscoDeviceClient(
            host          = ip,
            username      = credentials.get("username", ""),
            password      = credentials.get("password", ""),
            os_type       = os_type,
            enable_secret = credentials.get("secret") or None,
            timeout       = int(credentials.get("timeout", 30)),
        )
        client._cli_connect()
        return client
    except DeviceAuthError as exc:
        raise GatewayConnectionError(f"authentication failed for {ip}: {exc}") from exc
    except DeviceTransportError as exc:
        raise GatewayConnectionError(f"connection failed for {ip}: {exc}") from exc
    except Exception as exc:
        raise GatewayConnectionError(f"SSH error for {ip}: {exc}") from exc


def _send_cmd(client: CiscoDeviceClient, cmd: str) -> str:
    """Send a CLI command via *client* and return the raw text output."""
    try:
        raw, _, _ = client._cli_run_command(cmd, parse=False)
        return raw
    except DeviceTransportError as exc:
        raise GatewayConnectionError(f"Command {cmd!r} failed: {exc}") from exc


def connect_to_device(
    ip: str,
    credentials: Dict[str, str],
    device_type: str = "ios",
) -> str:
    """Open an SSH session to *ip*, retrieve the hostname prompt, then disconnect.

    Returns the hostname string (falls back to *ip*).
    Raises :exc:`GatewayConnectionError` on any failure.
    """
    client = _open_device_client(ip, device_type, credentials)
    try:
        prompt   = client._cli_connection.find_prompt()
        hostname = prompt.rstrip("#>").strip()
        log.debug("Connected to %s — prompt: %r", ip, prompt)
        return hostname or ip
    except Exception as exc:
        raise GatewayConnectionError(f"prompt detection failed for {ip}: {exc}") from exc
    finally:
        client._cli_disconnect()


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — MAC address normalization
# ─────────────────────────────────────────────────────────────────────────────


def normalize_mac(raw: str) -> Optional[str]:
    """Normalize any common MAC format to xx:xx:xx:xx:xx:xx (lowercase).

    Accepts colon, dash, Cisco-dot, or no-delimiter inputs.
    Returns None when the input is not a valid 48-bit MAC.
    """
    digits = re.sub(r"[:\-\.]", "", raw.strip()).lower()
    if len(digits) != 12 or not re.fullmatch(r"[0-9a-f]{12}", digits):
        return None
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


def mac_to_cisco_fmt(mac: str) -> str:
    """Convert a normalized xx:xx:xx:xx:xx:xx MAC to Cisco xxxx.xxxx.xxxx notation."""
    digits = mac.replace(":", "")
    return f"{digits[0:4]}.{digits[4:8]}.{digits[8:12]}"


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — ARP lookup
# ─────────────────────────────────────────────────────────────────────────────


def arp_lookup(
    client: CiscoDeviceClient,
    device_type: str,  # noqa: ARG001 — reserved for future platform-specific ARP variants
    target_ip: str,
) -> Optional[str]:
    """Run ``show ip arp <target_ip>`` and return the normalized MAC, or None."""
    cmd = f"show ip arp {target_ip}"
    try:
        output = _send_cmd(client, cmd)
    except Exception as exc:
        log.error("ARP command failed (%s): %s", cmd, exc)
        return None

    log.debug("ARP output for %s:\n%s", target_ip, output)

    # Cisco dotted: xxxx.xxxx.xxxx
    m = re.search(r"([0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4})", output)
    if m:
        return normalize_mac(m.group(1))

    # Colon-separated: xx:xx:xx:xx:xx:xx
    m = re.search(
        r"([0-9a-fA-F]{2}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}"
        r":[0-9a-fA-F]{2}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2})",
        output,
    )
    if m:
        return normalize_mac(m.group(1))

    log.debug("No MAC found in ARP output for %s", target_ip)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — gateway SVI / routed-interface lookup
# ─────────────────────────────────────────────────────────────────────────────


def _parse_all_routes(output: str) -> List[Dict[str, Optional[str]]]:
    """Parse every ECMP route entry from ``show ip route <ip>`` output.

    Returns a list — one dict per next-hop — so callers get all ECMP paths.
    Each dict has keys: prefix, next_hop, exit_interface, route_source,
    route_tag, route_age.

    Age / uptime handling:
      - IOS-XE descriptor: "10.0.0.2, from X, 2w2d ago, via Gi1/0/1"
        → age extracted per-entry from the "<age> ago" token on that line.
      - NX-OS *via:        "*via 10.0.0.2, Eth1/1, [110/2], 00:01:02, …"
        → age is the token after [metric].
      - IOS-XE brief:      "via 10.0.0.2, 00:01:02, GigabitEthernet1/0/1"
        → age is the second comma-separated token.
      - Fallback:          "Last update from X on Gi1/0/1, 2w2d ago"
        → age applied to all entries that lack a per-entry age.

    Tag handling:
      - IOS-XE route-level:   "Tag 91, type extern 2, …"  → applied to all entries
      - IOS-XE per-descriptor: "Route tag 91"              → applied to that ECMP entry
      - NX-OS inline:          "*via …, tag 91"            → applied to that *via line

    Patterns tried in priority order (first group that yields ≥1 result wins):

      1. IOS-XE routing descriptor block – line-by-line parse to associate
         per-entry "Route tag X" and "<age> ago" with the correct ECMP entry.

      2. NX-OS *via lines:
           *via 10.0.0.2, Eth1/1, [110/2], 00:01:02, ospf-1, intra, tag 91

      3. IOS-XE brief with age+interface:
           O 192.168.0.0/24 [110/2] via 10.0.0.2, 00:01:02, GigabitEthernet1/0/1

      4. IOS-XE brief C/L directly connected:
           C 192.168.0.0/24 is directly connected, Vlan200

      5. Fallback: any "via <ip>" (interface unknown)
    """
    routes: List[Dict[str, Optional[str]]] = []

    # ── Common fields ─────────────────────────────────────────────────────────
    prefix:     Optional[str] = None
    source:     Optional[str] = None
    global_tag: Optional[str] = None
    global_age: Optional[str] = None

    m = re.search(r"Routing entry for\s+(\S+)", output, re.IGNORECASE)
    if m:
        prefix = m.group(1)
    if not prefix:
        m = re.search(r"^(\d+\.\d+\.\d+\.\d+/\d+),\s+ubest", output, re.MULTILINE)
        if m:
            prefix = m.group(1)

    m = re.search(r'Known via\s+"([^"]+)"', output, re.IGNORECASE)
    if m:
        source = m.group(1)

    # IOS-XE route-level tag: "  Tag 91, type extern 2, forward metric 1"
    m = re.search(r"^\s+Tag\s+(\d+)", output, re.IGNORECASE | re.MULTILINE)
    if m:
        global_tag = m.group(1)

    # Global age fallback from Last update line.
    # Handles both:
    #   OSPF/Static: "Last update from X on TwentyFiveGigE1/5/0/3, 2w2d ago"
    #   BGP:         "Last update from 198.18.255.93 2w4d ago"   (no "on Interface")
    m = re.search(
        r"Last update from\s+\S+(?:\s+on\s+\S+,)?\s+(\S+)\s+ago",
        output, re.IGNORECASE,
    )
    if m:
        global_age = m.group(1)

    def _entry(
        nh: str,
        iface: Optional[str],
        src:  Optional[str] = None,
        tag:  Optional[str] = None,
        age:  Optional[str] = None,
    ) -> Dict[str, Optional[str]]:
        return {
            "prefix":        prefix,
            "next_hop":      nh,
            "exit_interface": iface,
            "route_source":  src or source,
            "route_tag":     tag if tag is not None else global_tag,
            "route_age":     age if age is not None else global_age,
        }

    # ── Pattern 0: NX-OS ubest/mbest format ──────────────────────────────────
    #
    # NX-OS *via lines (best route marked with *, non-best without):
    #   "    *via 172.18.0.252, Po12, [110/4], 3w2d, ospf-1, intra"
    #   "    via 172.18.0.253, Po13, [110/4], 3w2d, ospf-1, intra"
    # NX-OS directly connected:
    #   "    *via 0.0.0.0, Vlan128, [0/0], 3w2d, direct"
    #   "    *via 10.0.0.1, Vlan128, [0/0], 3w2d, local"
    #
    # Field order: nexthop-IP, interface, [AD/metric], age, protocol[, type][, tag N]
    # This block runs before Pattern 1 to prevent NX-OS output from being
    # misrouted into IOS-style descriptor parsing.
    if re.search(r"ubest/mbest:", output, re.IGNORECASE):
        _NXOS_VIA = re.compile(
            r"^\s+\*?via\s+(\d+\.\d+\.\d+\.\d+),\s+([^\s,]+)"
            r"(?:,\s+\[[^\]]+\],\s+([^,]+),\s+([^,\s]+))?"  # [AD/metric], age, protocol
            r"(?:.*?\btag\s+(\d+))?",
            re.IGNORECASE | re.MULTILINE,
        )
        for m in _NXOS_VIA.finditer(output):
            protocol = (m.group(4) or "").lower()
            nh       = "directly connected" if protocol in ("direct", "local") else m.group(1)
            tag      = m.group(5) or global_tag
            age      = m.group(3).strip() if m.group(3) else None
            routes.append(_entry(nh, m.group(2), tag=tag, age=age))
        if routes:
            return routes

    # ── Pattern 1: IOS-XE routing descriptor block (line-by-line) ────────────
    #
    # OSPF / Static descriptor (interface present):
    #   "  * 10.0.0.2, from 198.18.x.x, 2w2d ago, via GigabitEthernet1/0/1"
    #   "    10.0.0.6, from 198.18.x.x, 2w2d ago, via GigabitEthernet1/0/2"
    #
    # BGP internal descriptor (NO interface — recursive next-hop):
    #   "  * 198.18.255.93, from 198.18.255.93, 2w4d ago"
    #
    # Directly connected:
    #   "  * directly connected, via Vlan128"
    #
    # _DESC_IP captures group(1)=next-hop IP, group(2)=interface or None (BGP).
    # _RAGE_PER uses \b...\b so it matches "2w4d" even at end-of-line.
    #
    _DESC_IP  = re.compile(
        r"^\s+\*?\s*(\d+\.\d+\.\d+\.\d+),(?:.*?via\s+(\S+))?",
        re.IGNORECASE,
    )
    _DESC_DC  = re.compile(r"^\s+\*?\s*directly connected,\s+via\s+(\S+)",  re.IGNORECASE)
    _RTAG_PER = re.compile(r"^\s+Route tag\s+(\d+)",                        re.IGNORECASE)
    _RAGE_PER = re.compile(r"\b(\S+)\s+ago\b",                              re.IGNORECASE)

    pending: Optional[Dict[str, Optional[str]]] = None

    for line in output.splitlines():
        m = _DESC_IP.search(line)
        if m:
            if pending is not None:
                routes.append(pending)
            age_m = _RAGE_PER.search(line)
            iface = m.group(2).rstrip(",") if m.group(2) else None
            pending = _entry(
                m.group(1),
                iface,
                age=age_m.group(1) if age_m else None,
            )
            continue

        m = _DESC_DC.search(line)
        if m:
            if pending is not None:
                routes.append(pending)
            pending = _entry("directly connected", m.group(1).rstrip(","), "connected")
            continue

        m = _RTAG_PER.search(line)
        if m and pending is not None:
            pending["route_tag"] = m.group(1)

    if pending is not None:
        routes.append(pending)

    if routes:
        # Interface fallback for entries that have a next-hop but no interface.
        # Handles OSPF: "Last update from X on TwentyFiveGigE1/5/0/3, 2w2d ago"
        fb = re.search(r"Last update from\s+\S+\s+on\s+(\S+),", output, re.IGNORECASE)
        for r in routes:
            if not r["exit_interface"] and fb:
                r["exit_interface"] = fb.group(1).rstrip(",")
        return routes

    # ── Pattern 2: NX-OS *via lines (age is 4th field, after [metric]) ───────
    # "*via 10.0.0.2, Eth1/1, [110/2], 00:01:02, ospf-1, intra, tag 91"
    for m in re.finditer(
        r"^\s*\*via\s+(\d+\.\d+\.\d+\.\d+),\s+([^\s,]+)"
        r"(?:,\s+\[[^\]]+\],\s+([^,]+))?"     # optional: [metric], age
        r"(?:.*?\btag\s+(\d+))?",
        output, re.IGNORECASE | re.MULTILINE,
    ):
        tag = m.group(4) or global_tag
        age = m.group(3).strip() if m.group(3) else None
        routes.append(_entry(m.group(1), m.group(2), tag=tag, age=age))

    if routes:
        return routes

    # ── Pattern 3: IOS-XE brief with age+interface ───────────────────────────
    # "O  192.168.0.0/24 [110/2] via 10.0.0.2, 00:01:02, GigabitEthernet1/0/1"
    # "O E2 10.10.218.0/23 [110/1] via 10.254.80.6, 2w2d, TwentyFiveGigE1/1/0/47"
    # Age can be HH:MM:SS *or* Cisco duration format (2w2d, 1d12h, 3d23h, etc.).
    for m in re.finditer(
        r"\bvia\s+(\d+\.\d+\.\d+\.\d+),\s+([^,\s]+),\s+(\S+)",
        output, re.IGNORECASE,
    ):
        routes.append(_entry(m.group(1), m.group(3).rstrip(","), age=m.group(2)))

    if routes:
        return routes

    # ── Pattern 4: IOS-XE brief directly connected ───────────────────────────
    for m in re.finditer(r"is directly connected,\s+(\S+)", output, re.IGNORECASE):
        routes.append(_entry("directly connected", m.group(1).rstrip(","), "connected"))

    if routes:
        return routes

    # ── Pattern 5: any "via <ip>" (no interface) ─────────────────────────────
    for m in re.finditer(r"\bvia\s+(\d+\.\d+\.\d+\.\d+)", output, re.IGNORECASE):
        routes.append(_entry(m.group(1), None))

    return routes


def get_routes_for_ip(client: CiscoDeviceClient, ip: str) -> List[Dict[str, Optional[str]]]:
    """Run ``show ip route <ip>`` and return all matching ECMP routes."""
    try:
        output = _send_cmd(client, f"show ip route {ip}")
    except Exception as exc:
        log.error("Route lookup for %s failed: %s", ip, exc)
        return []
    log.debug("show ip route %s:\n%s", ip, output)
    routes = _parse_all_routes(output)
    for r in routes:
        r["exit_interface"] = _clean_iface(r.get("exit_interface"))
    return routes


def get_gateway_interface(client: CiscoDeviceClient, gateway_ip: str) -> Optional[str]:
    """Return the interface on the gateway that carries *gateway_ip*.

    Delegates to ``get_routes_for_ip`` and returns the exit_interface of the
    first matching route (the gateway's own IP is always directly connected).
    """
    for route in get_routes_for_ip(client, gateway_ip):
        if route.get("exit_interface"):
            log.debug("Gateway %s is on interface %s", gateway_ip, route["exit_interface"])
            return route["exit_interface"]
    log.debug("Could not resolve gateway interface for %s", gateway_ip)
    return None


def get_route_for_destination(
    client: CiscoDeviceClient,
    dst_ip: str,
) -> List[Dict[str, Optional[str]]]:
    """Return all ECMP routes for *dst_ip* from ``show ip route <dst_ip>``.

    Returns a list (one entry per ECMP path).  Each entry has:
      prefix, next_hop, exit_interface, route_source.
    """
    routes = get_routes_for_ip(client, dst_ip)
    log.debug("Routes for %s: %s", dst_ip, routes)
    return routes


def _resolve_ingress_interfaces(
    client: CiscoDeviceClient,
    lookup_ip: str,
) -> List[str]:
    """Return the physical ingress interface(s) by resolving one BGP recursion level.

    For IGP/static/connected routes, ``show ip route <lookup_ip>`` returns an
    exit_interface directly.

    For BGP routes the descriptor block has a next-hop IP (the BGP peer/route
    reflector address) but *no* exit_interface, because BGP uses a recursive
    next-hop resolved via IGP.  In that case a second ``show ip route <nh>``
    on the BGP next-hop IP produces the physical interface via IGP.

    Example (from user trace)::

        show ip route 10.254.29.194
          Known via "bgp 64646" ...
          * 198.18.255.12, from 198.18.255.12, 2w3d ago   ← no interface

        show ip route 198.18.255.12
          Known via "ospf 500" ...
          * 198.18.0.10, via TenGigabitEthernet0/0/2      ← physical interface ✓
    """
    ifaces:        List[str] = []
    recursive_ips: List[str] = []

    for r in get_routes_for_ip(client, lookup_ip):
        iface = r.get("exit_interface")
        if iface:
            if iface not in ifaces:
                ifaces.append(iface)
        else:
            nh = r.get("next_hop")
            if nh and nh != "directly connected" and nh not in recursive_ips:
                recursive_ips.append(nh)

    if not ifaces and recursive_ips:
        for nh_ip in recursive_ips:
            log.debug("BGP recursive ingress resolution: show ip route %s", nh_ip)
            print(f"[L3]   BGP recursive: show ip route {nh_ip} → ingress interface")
            for r in get_routes_for_ip(client, nh_ip):
                iface = r.get("exit_interface")
                if iface and iface not in ifaces:
                    ifaces.append(iface)

    return ifaces


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — MAC table lookup
# ─────────────────────────────────────────────────────────────────────────────

# Matches the start of any Cisco/NX-OS interface abbreviation.
_IFACE_RE = re.compile(
    r"^(Gi|Fa|Te|Fo|Hu|Twe|Po|Eth|GigabitEthernet|FastEthernet"
    r"|TenGigabitEthernet|Port-channel|port-channel|ae|bundle)",
    re.IGNORECASE,
)

# Validates a string as a real Cisco/NX-OS interface name.
# Rejects garbage values like "[1/0]", "1/0", empty strings, etc.
_VALID_IFACE_RE = re.compile(
    r"^(?:GigabitEthernet|TenGigabitEthernet|TwentyFiveGigE|HundredGigE"
    r"|FortyGigabitEthernet|FastEthernet|Ethernet"
    r"|Port-channel|port-channel|Bundle-Ether|bundle-ether"
    r"|Vlan|Loopback|Tunnel|Serial|Management|mgmt"
    r"|Gi|Te|Twe|Hu|Fo|Fa|Eth|Po|Lo|Tu|Se|Mg|ae|BE)\d",
    re.IGNORECASE,
)


def _clean_iface(iface: Optional[str]) -> Optional[str]:
    """Return *iface* unchanged if it looks like a valid Cisco interface, else None.

    Rejects values like ``[1/0]``, ``1/0``, ``—``, or empty strings that
    occasionally appear when route parsing produces a non-interface token.
    """
    if not iface:
        return None
    return iface if _VALID_IFACE_RE.match(iface) else None


def _parse_mac_table_output(output: str, mac: str) -> Optional[Dict[str, str]]:
    """Parse ``show mac address-table`` output and return {vlan, interface, mac}."""
    search_mac = mac_to_cisco_fmt(mac).lower()

    for line in output.splitlines():
        if search_mac not in line.lower():
            continue

        # Strip NX-OS leading flag characters (* G R C ~ +) and whitespace.
        clean = re.sub(r"^\s*[*GRC~+\s]+", "", line).strip()
        parts = clean.split()
        if len(parts) < 3:
            continue

        vlan      : Optional[str] = None
        interface : Optional[str] = None

        # VLAN — first token that is purely numeric.
        if parts[0].isdigit():
            vlan = parts[0]

        # Interface — rightmost token that looks like a network interface.
        for token in reversed(parts):
            if _IFACE_RE.match(token):
                interface = token
                break

        if vlan and interface:
            log.debug("MAC table: VLAN=%s, interface=%s", vlan, interface)
            return {"vlan": vlan, "interface": interface, "mac": mac}

    log.debug("Could not parse MAC table output:\n%s", output)
    return None


def mac_table_lookup(
    client: CiscoDeviceClient,
    device_type: str,  # noqa: ARG001 — reserved for future platform-specific MAC table commands
    mac: str,
) -> Optional[Dict[str, str]]:
    """Look up *mac* in the forwarding table and return {vlan, interface, mac}."""
    cisco_mac = mac_to_cisco_fmt(mac)
    cmd = f"show mac address-table address {cisco_mac}"
    try:
        output = _send_cmd(client, cmd)
    except Exception as exc:
        log.error("MAC table command failed (%s): %s", cmd, exc)
        return None
    log.debug("MAC table output:\n%s", output)
    return _parse_mac_table_output(output, mac)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — port-channel member lookup
# ─────────────────────────────────────────────────────────────────────────────


def is_portchannel(interface: str) -> bool:
    """Return True when *interface* is a LAG/port-channel logical interface."""
    return bool(
        re.match(r"^(port-?channel|Po|ae|bundle-?ether)\d+", interface, re.IGNORECASE)
    )


def _parse_ios_portchannel_members(output: str, po_num: str) -> List[str]:
    """Extract physical members of Port-channel *po_num* from IOS/IOS-XE etherchannel summary.

    Typical line format:
        1    Po1(SU)    LACP    Gi1/0/47(P) Gi2/0/47(P)
    """
    members: List[str] = []
    capturing = False

    for line in output.splitlines():
        if re.search(rf"\bPo{po_num}\b", line, re.IGNORECASE):
            capturing = True
        elif capturing and re.search(r"\bPo\d+\b", line):
            break  # start of next group

        if not capturing:
            continue

        for raw in re.findall(r"((?:Gi|Fa|Te|Fo|Hu|Twe)\d[\d/\.]*)", line):
            clean = re.sub(r"\([^)]*\)", "", raw).strip()
            if clean and clean not in members:
                members.append(clean)

    return members


def _parse_nxos_portchannel_members(output: str, po_num: str) -> List[str]:
    """Extract physical members of Po *po_num* from NX-OS port-channel summary.

    Typical line format:
        1    Po1(SU)    Eth    LACP    Eth1/1(P) Eth1/2(P)
    """
    members: List[str] = []
    capturing = False

    for line in output.splitlines():
        if re.search(rf"\bPo{po_num}\b", line, re.IGNORECASE):
            capturing = True
        elif capturing and re.search(r"\bPo\d+\b", line):
            break

        if not capturing:
            continue

        for raw in re.findall(r"(Eth\d+/\d+(?:/\d+)?)", line):
            clean = re.sub(r"\([^)]*\)", "", raw).strip()
            if clean and clean not in members:
                members.append(clean)

    return members


def get_portchannel_members(
    client: CiscoDeviceClient,
    device_type: str,
    interface: str,
) -> List[str]:
    """Return the physical member interfaces of the given port-channel.

    Uses platform-appropriate commands:
      IOS/IOS-XE: ``show etherchannel summary``
      NX-OS:      ``show port-channel summary``
    """
    m = re.search(r"\d+", interface)
    if not m:
        log.debug("Cannot extract port-channel number from %r", interface)
        return []

    po_num = m.group(0)
    try:
        if "nxos" in device_type:
            output  = _send_cmd(client, "show port-channel summary")
            members = _parse_nxos_portchannel_members(output, po_num)
        else:
            output  = _send_cmd(client, "show etherchannel summary")
            members = _parse_ios_portchannel_members(output, po_num)
            if not members:
                # Fallback: device_type may be "ios" for a Nexus that was reached
                # via direct SSH without NetBox platform detection.
                try:
                    output  = _send_cmd(client, "show port-channel summary")
                    members = _parse_nxos_portchannel_members(output, po_num)
                except Exception:
                    pass
    except Exception as exc:
        log.error("Port-channel member lookup failed: %s", exc)
        return []

    log.debug("Port-channel %s members: %s", interface, members)
    return members


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — interface detail (show interface <name>)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_interface_detail_nxos(output: str) -> Dict:
    """Parse NX-OS ``show interface <name>`` output into a flat detail dict.

    Returned keys (all may be ``None`` when not found in the output):
      state, description, speed, duplex,
      rx_runts, rx_giants, rx_crc, rx_input_error, rx_input_discard,
      tx_output_error, tx_output_discard
    """
    def _int(m: Optional[re.Match]) -> Optional[int]:
        return int(m.group(1)) if m else None

    result: Dict = {
        "state":            None,
        "description":      None,
        "speed":            None,
        "duplex":           None,
        "rx_runts":         None,
        "rx_giants":        None,
        "rx_crc":           None,
        "rx_input_error":   None,
        "rx_input_discard": None,
        "tx_output_error":  None,
        "tx_output_discard": None,
    }
    if not output:
        return result

    # ── State ─────────────────────────────────────────────────────────────────
    # "port-channel31 is up" / "Ethernet1/1 is down" / "... admin state is down"
    m = re.search(
        r"^\S+\s+is\s+(up|down|administratively\s+down)",
        output, re.IGNORECASE | re.MULTILINE,
    )
    if m:
        result["state"] = m.group(1).lower().replace("  ", " ")

    # ── Description ───────────────────────────────────────────────────────────
    m = re.search(r"Description:\s*(.+)", output, re.IGNORECASE)
    if m:
        result["description"] = m.group(1).strip()

    # ── Duplex ────────────────────────────────────────────────────────────────
    m = re.search(r"\b(full|half)[\s-]?duplex\b", output, re.IGNORECASE)
    if m:
        raw = m.group(0).lower().strip()
        result["duplex"] = re.sub(r"\s+", "-", raw) if " " in raw else raw

    # ── Speed ─────────────────────────────────────────────────────────────────
    # e.g. "100 Gb/s", "10 Gb/s", "1000 Mb/s"
    m = re.search(r"\b(\d+(?:\.\d+)?\s*[GMK]b/s)\b", output, re.IGNORECASE)
    if m:
        result["speed"] = m.group(1).strip()

    # ── RX / TX sections ─────────────────────────────────────────────────────
    # NX-OS uses "  RX\n" and "  TX\n" as section headers.
    rx_m = re.search(r"^\s+RX\s*$", output, re.MULTILINE)
    tx_m = re.search(r"^\s+TX\s*$", output, re.MULTILINE)

    if rx_m and tx_m:
        rx_text = output[rx_m.end(): tx_m.start()]
        tx_text = output[tx_m.end():]
    elif rx_m:
        rx_text = output[rx_m.end():]
        tx_text = ""
    else:
        rx_text = output
        tx_text = output

    result["rx_runts"]         = _int(re.search(r"(\d+)\s+runts\b",         rx_text, re.I))
    result["rx_giants"]        = _int(re.search(r"(\d+)\s+giants\b",        rx_text, re.I))
    result["rx_crc"]           = _int(re.search(r"(\d+)\s+CRC\b",           rx_text, re.I))
    result["rx_input_error"]   = _int(re.search(r"(\d+)\s+input\s+error\b", rx_text, re.I))
    result["rx_input_discard"] = _int(re.search(r"(\d+)\s+input\s+discard\b", rx_text, re.I))

    if tx_text:
        result["tx_output_error"]   = _int(re.search(r"(\d+)\s+output\s+error\b",   tx_text, re.I))
        result["tx_output_discard"] = _int(re.search(r"(\d+)\s+output\s+discard\b", tx_text, re.I))

    return result


def _parse_interface_detail_ios(output: str) -> Dict:
    """Parse IOS/IOS-XE ``show interface <name>`` output into a flat detail dict.

    Returned keys (all may be ``None`` when not found in the output):
      state, description, speed, duplex,
      runts, giants, crc, input_error,
      total_output_drops, output_error, output_discard,
      unknown_protocol_drops
    """
    def _int(m: Optional[re.Match]) -> Optional[int]:
        return int(m.group(1)) if m else None

    result: Dict = {
        "state":                 None,
        "description":           None,
        "speed":                 None,
        "duplex":                None,
        "runts":                 None,
        "giants":                None,
        "crc":                   None,
        "input_error":           None,
        "total_output_drops":    None,
        "output_error":          None,
        "output_discard":        None,
        "unknown_protocol_drops": None,
    }
    if not output:
        return result

    # ── State ─────────────────────────────────────────────────────────────────
    # "Port-channel100 is up, line protocol is up (connected)"
    # Prefer the "line protocol" state since it reflects actual forwarding.
    m = re.search(r"line\s+protocol\s+is\s+(up|down)", output, re.IGNORECASE)
    if m:
        result["state"] = m.group(1).lower()
    else:
        m = re.search(
            r"^\S+\s+is\s+(up|down|administratively\s+down)",
            output, re.IGNORECASE | re.MULTILINE,
        )
        if m:
            result["state"] = m.group(1).lower().replace("  ", " ")

    # ── Description ───────────────────────────────────────────────────────────
    m = re.search(r"Description:\s*(.+)", output, re.IGNORECASE)
    if m:
        result["description"] = m.group(1).strip()

    # ── Duplex + Speed ────────────────────────────────────────────────────────
    # IOS always puts duplex and speed on the SAME line:
    #   "Full-duplex, 1000Mb/s, link type is auto, media type is ..."
    #   "Half-duplex, 10Mb/s ..."
    # Searching the whole output risks matching the BW line
    # ("BW 1000000 Kbit/sec") before the actual speed value.
    # Instead, find the duplex line first and extract speed from it only.
    for _line in output.splitlines():
        if not re.search(r"\b(?:full|half|auto)[- ]?duplex\b", _line, re.IGNORECASE):
            continue
        dm = re.search(r"\b((?:full|half|auto)[- ]?duplex)\b", _line, re.IGNORECASE)
        if dm:
            raw = dm.group(1).lower()
            result["duplex"] = re.sub(r"\s+", "-", raw) if " " in raw else raw
        sm = re.search(r"\b(\d+(?:\.\d+)?\s*[GMK]b(?:/s|ps)?)\b", _line, re.IGNORECASE)
        if sm:
            result["speed"] = sm.group(1).strip()
        break  # duplex line found — stop scanning

    # Fallback: if no duplex line contained a speed, scan the whole output
    if not result["speed"]:
        m = re.search(r"\b(\d+(?:\.\d+)?\s*[GMK]b(?:/s|ps)?)\b", output, re.IGNORECASE)
        if m:
            result["speed"] = m.group(1).strip()

    # ── Total output drops ────────────────────────────────────────────────────
    # "Input queue: 0/2000/0/0 ...; Total output drops: 0"
    result["total_output_drops"] = _int(
        re.search(r"Total\s+output\s+drops:\s*(\d+)", output, re.I)
    )

    # ── RX counters ──────────────────────────────────────────────────────────
    # "0 runts, 0 giants, 0 throttles"
    result["runts"]  = _int(re.search(r"\b(\d+)\s+runts\b",  output, re.I))
    result["giants"] = _int(re.search(r"\b(\d+)\s+giants\b", output, re.I))

    # "0 input errors, 0 CRC, 0 frame, 0 overrun, 0 ignored"
    result["crc"]         = _int(re.search(r"\b(\d+)\s+CRC\b",          output, re.I))
    result["input_error"] = _int(re.search(r"\b(\d+)\s+input\s+errors?\b", output, re.I))

    # ── TX counters ──────────────────────────────────────────────────────────
    # "0 output errors, 0 collisions, 0 interface resets"
    result["output_error"] = _int(re.search(r"\b(\d+)\s+output\s+errors?\b", output, re.I))

    # "0 unknown protocol drops"
    result["unknown_protocol_drops"] = _int(
        re.search(r"\b(\d+)\s+unknown\s+protocol\s+drops\b", output, re.I)
    )

    # output_discard — not always present (optional field)
    result["output_discard"] = _int(
        re.search(r"\b(\d+)\s+output\s+discards?\b", output, re.I)
    )

    return result


def get_interface_detail(
    client: CiscoDeviceClient,
    device_type: str,
    interface_name: str,
) -> Dict:
    """Run ``show interface <interface_name>`` and return parsed health fields.

    Automatically selects NX-OS or IOS parsing based on the output content
    (``admin state is`` → NX-OS; ``line protocol is`` → IOS), falling back to
    the *device_type* hint when neither marker appears.

    On any error (command failure, parse failure) logs at DEBUG level and
    returns a dict of ``None`` values so callers always receive a consistent
    shape regardless of device errors or unexpected output formats.

    Works for all interface types already tracked by the tracer:
      - Physical ports (GigabitEthernetX/Y, EthernetX/Y, etc.)
      - Port-channels  (Port-channelN)
      - Subinterfaces  (Port-channelN.M, GigabitEthernetX/Y.Z)
    """
    _NXOS_NULL: Dict = {
        "state": None, "description": None, "speed": None, "duplex": None,
        "rx_runts": None, "rx_giants": None, "rx_crc": None,
        "rx_input_error": None, "rx_input_discard": None,
        "tx_output_error": None, "tx_output_discard": None,
    }
    _IOS_NULL: Dict = {
        "state": None, "description": None, "speed": None, "duplex": None,
        "runts": None, "giants": None, "crc": None, "input_error": None,
        "total_output_drops": None, "output_error": None,
        "output_discard": None, "unknown_protocol_drops": None,
    }

    if not interface_name:
        return {}

    try:
        output = _send_cmd(client, f"show interface {interface_name}")
    except Exception as exc:
        log.debug("get_interface_detail(%r): command failed: %s", interface_name, exc)
        return _NXOS_NULL if "nxos" in device_type.lower() else _IOS_NULL

    if not output or not output.strip():
        log.debug("get_interface_detail(%r): empty output", interface_name)
        return _NXOS_NULL if "nxos" in device_type.lower() else _IOS_NULL

    # ── Detect platform from output content ──────────────────────────────────
    if (re.search(r"\badmin\s+state\s+is\b", output, re.I)
            or "nxos" in device_type.lower()):
        result = _parse_interface_detail_nxos(output)
    else:
        result = _parse_interface_detail_ios(output)

    # Always attach the full raw output so the frontend can show it verbatim.
    result["raw_output"] = output.strip()
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — show version helper
# ─────────────────────────────────────────────────────────────────────────────


def _parse_stack_members(output: str) -> List[Dict]:
    """Parse per-member info from a Cisco IOS StackWise ``show version`` output.

    Handles the common IOS/IOS-XE format where each stack member is introduced
    by a ``Switch <N>`` line followed by a separator (dashes or blank line).

    Example section::

        Switch 01
        ---------
        Switch uptime                 : 28 weeks, 1 day, 14 hours, 30 minutes
        Model number                  : WS-C3750X-48PF-S
        System serial number          : FOC1534Y1Z6
        Current Software state        : ACTIVE
        Image ver                     : 12.2(55)SE9

    Returns an empty list when no stack sections are detected.
    """
    members: List[Dict] = []

    # Locate every "Switch N" header (followed by optional spaces/dashes on same
    # line, or a separator line immediately below).
    header_re = re.compile(
        r"^[ \t]*Switch\s+(\d+)[ \t]*(?:[-]+)?[ \t]*$",
        re.IGNORECASE | re.MULTILINE,
    )
    matches = list(header_re.finditer(output))
    if not matches:
        return []

    for idx, match in enumerate(matches):
        num   = int(match.group(1))
        start = match.start()
        end   = matches[idx + 1].start() if idx + 1 < len(matches) else len(output)
        sec   = output[start:end]

        member: Dict = {"switch_num": num}

        # Uptime for this member
        m = re.search(r"Switch uptime\s*[:=]\s*(.+)", sec, re.IGNORECASE)
        if not m:
            m = re.search(r"Uptime in current state\s*[:=]\s*(.+)", sec, re.IGNORECASE)
        if m:
            member["uptime"] = m.group(1).strip().rstrip(".")

        # Model number
        m = re.search(r"Model number\s*[:=]\s*(\S+)", sec, re.IGNORECASE)
        if m:
            member["model"] = m.group(1).strip().rstrip(",")

        # System serial number
        m = re.search(r"System serial number\s*[:=]\s*(\S+)", sec, re.IGNORECASE)
        if m:
            member["serial"] = m.group(1).strip().rstrip(",")

        # Software version for this member
        m = re.search(r"Image ver(?:sion)?\s*[:=]\s*(\S+)", sec, re.IGNORECASE)
        if not m:
            m = re.search(r"Software version\s*[:=]\s*(\S+)", sec, re.IGNORECASE)
        if m:
            member["os_version"] = m.group(1).strip().rstrip(",")

        # Role (ACTIVE / MEMBER / STANDBY)
        m = re.search(
            r"Current Software state\s*[:=]\s*(.+?)(?:\r?\n|$)",
            sec, re.IGNORECASE,
        )
        if m:
            member["role"] = m.group(1).strip()

        # Base MAC
        m = re.search(r"Base ethernet MAC Address\s*[:=]\s*(\S+)", sec, re.IGNORECASE)
        if m:
            member["mac"] = m.group(1).strip().rstrip(",")

        members.append(member)

    return members


def get_device_version(
    client:      CiscoDeviceClient,
    device_type: str,
) -> Dict:
    """Run ``show version`` and return OS version, uptime, and stack-member info.

    Returned keys:
      os_version    — overall version string, e.g. ``"15.2(4)E9"``
      uptime        — system uptime from the first/active member
      stack_members — list of per-member dicts (empty when not a stack)
                      Each member dict has: switch_num, uptime, model, serial,
                      os_version, role, mac  (all optional)
    """
    result: Dict = {"os_version": None, "uptime": None, "stack_members": []}

    try:
        output = _send_cmd(client, "show version")
    except Exception as exc:
        log.debug("get_device_version: command failed: %s", exc)
        return result

    if not output:
        return result

    # ── Uptime ────────────────────────────────────────────────────────────────
    # IOS/IOS-XE:  "<hostname> uptime is 28 weeks, 1 day, ..."
    # NX-OS:       "Kernel uptime is 0 day(s), 3 hour(s), ..."
    m = re.search(r"uptime is (.+)", output, re.IGNORECASE)
    if m:
        result["uptime"] = m.group(1).strip().rstrip(".")

    # ── OS version ────────────────────────────────────────────────────────────
    for pattern in (
        r"(?:Cisco IOS(?: XE)? Software[^,]*,\s*Version)\s+(\S+)",
        r"NXOS:\s*version\s+(\S+)",
        r"system:\s+version\s+(\S+)",
        r"Software\s+\((\S+)\),\s+Version\s+(\S+)",
    ):
        m = re.search(pattern, output, re.IGNORECASE)
        if m:
            groups = [g for g in m.groups() if g]
            result["os_version"] = groups[-1].rstrip(",") if groups else None
            break

    # ── Stack members (Cisco StackWise / 3750 / 9300 stacks) ─────────────────
    stack_members = _parse_stack_members(output)
    if stack_members:
        result["stack_members"] = stack_members
        # If a member-level version was found, prefer it as the global version
        for mb in stack_members:
            if mb.get("os_version"):
                result["os_version"] = result["os_version"] or mb["os_version"]
                break
        # Use the first/active member's uptime as global uptime when available
        for mb in stack_members:
            if mb.get("uptime"):
                result["uptime"] = result["uptime"] or mb["uptime"]
                break

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — CDP / LLDP neighbor lookup
# ─────────────────────────────────────────────────────────────────────────────


def _get_cdp_neighbor(
    client: CiscoDeviceClient,
    device_type: str,
    interface: str,
) -> Optional[Dict[str, str]]:
    """Return a CDP neighbor detail dict for *interface*, or None."""
    if "nxos" in device_type:
        cmd = f"show cdp neighbors interface {interface} detail"
    else:
        cmd = f"show cdp neighbors {interface} detail"

    try:
        output = _send_cmd(client, cmd)
    except Exception as exc:
        log.debug("CDP command failed on %s: %s", interface, exc)
        return None

    if not output or "device id" not in output.lower():
        return None

    info: Dict[str, str] = {"protocol": "CDP"}

    m = re.search(r"Device ID:\s*(\S+)", output, re.IGNORECASE)
    if m:
        info["neighbor_id"] = m.group(1)

    m = re.search(r"Platform:\s*([^,\r\n]+)", output, re.IGNORECASE)
    if m:
        info["platform"] = m.group(1).strip()

    m = re.search(r"IP [Aa]ddress:\s*(\S+)", output, re.IGNORECASE)
    if m:
        info["neighbor_ip"] = m.group(1)

    m = re.search(r"Port ID \(outgoing port\):\s*(\S+)", output, re.IGNORECASE)
    if m:
        info["remote_port"] = m.group(1)

    return info if "neighbor_id" in info else None


def _get_lldp_neighbor(
    client: CiscoDeviceClient,
    device_type: str,
    interface: str,
) -> Optional[Dict[str, str]]:
    """Return an LLDP neighbor detail dict for *interface*, or None."""
    if "nxos" in device_type:
        cmd = f"show lldp neighbors interface {interface} detail"
    else:
        cmd = f"show lldp neighbors {interface} detail"

    try:
        output = _send_cmd(client, cmd)
    except Exception as exc:
        log.debug("LLDP command failed on %s: %s", interface, exc)
        return None

    if not output or "chassis id" not in output.lower():
        return None

    info: Dict[str, str] = {"protocol": "LLDP"}

    m = re.search(r"System Name:\s*(\S+)", output, re.IGNORECASE)
    if m:
        info["neighbor_id"] = m.group(1)

    m = re.search(r"System Description:\s*(.+)", output, re.IGNORECASE)
    if m:
        info["platform"] = m.group(1).strip()[:80]

    m = re.search(r"Management Address:\s*(\S+)", output, re.IGNORECASE)
    if m:
        info["neighbor_ip"] = m.group(1)

    m = re.search(r"Port ID:\s*(\S+)", output, re.IGNORECASE)
    if m:
        info["remote_port"] = m.group(1)

    return info if "neighbor_id" in info else None


def get_neighbor_info(
    client: CiscoDeviceClient,
    device_type: str,
    interface: str,
) -> Optional[Dict[str, str]]:
    """Return the FIRST CDP or LLDP neighbor for *interface*, preferring CDP."""
    cdp = _get_cdp_neighbor(client, device_type, interface)
    if cdp:
        return cdp
    return _get_lldp_neighbor(client, device_type, interface)


def _parse_all_cdp_neighbors(output: str) -> List[Dict[str, str]]:
    """Parse ALL neighbor entries from ``show cdp neighbors <iface> detail`` output.

    The output may contain multiple sections separated by lines of dashes.
    Each section describes one neighbor.  Returns a list of neighbor dicts with
    the same keys as ``_get_cdp_neighbor``: neighbor_id, neighbor_ip,
    remote_port, platform, protocol.
    """
    neighbors: List[Dict[str, str]] = []
    # Split on any line that is purely dashes (IOS uses "---…", NX-OS uses "----…")
    sections = re.split(r'^-{3,}\s*$', output, flags=re.MULTILINE)
    for sec in sections:
        if "Device ID" not in sec and "device id" not in sec.lower():
            continue
        info: Dict[str, str] = {"protocol": "CDP"}
        m = re.search(r"Device ID:\s*(\S+)",                  sec, re.IGNORECASE)
        if m:
            info["neighbor_id"] = m.group(1)
        else:
            continue
        m = re.search(r"IP [Aa]ddress:\s*(\S+)",              sec, re.IGNORECASE)
        if m:
            info["neighbor_ip"] = m.group(1)
        m = re.search(r"Management [Aa]ddress.*?IP [Aa]ddress:\s*(\S+)", sec, re.IGNORECASE | re.DOTALL)
        if m and "neighbor_ip" not in info:
            info["neighbor_ip"] = m.group(1)
        m = re.search(r"Port ID \(outgoing port\):\s*(\S+)",  sec, re.IGNORECASE)
        if m:
            info["remote_port"] = m.group(1)
        m = re.search(r"Platform:\s*([^,\r\n]+)",             sec, re.IGNORECASE)
        if m:
            info["platform"] = m.group(1).strip()
        neighbors.append(info)
    return neighbors


def get_all_cdp_neighbors(
    client:      CiscoDeviceClient,
    device_type: str,
    interface:   str,
) -> List[Dict[str, str]]:
    """Return ALL CDP neighbors on *interface* as a list.

    Used when a port is a trunk/uplink and multiple devices appear in CDP.
    Falls back to an empty list on any failure.
    """
    if "nxos" in device_type.lower():
        cmd = f"show cdp neighbors interface {interface} detail"
    else:
        cmd = f"show cdp neighbors {interface} detail"
    try:
        output = _send_cmd(client, cmd)
    except Exception as exc:
        log.debug("get_all_cdp_neighbors(%s): command failed: %s", interface, exc)
        return []
    if not output or "device id" not in output.lower():
        return []
    return _parse_all_cdp_neighbors(output)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — stop-condition evaluation and result output
# ─────────────────────────────────────────────────────────────────────────────


def should_stop_trace(
    neighbor_info: Optional[Dict[str, str]],
) -> Tuple[bool, str]:
    """Return (stop, reason) based on CDP/LLDP neighbor information.

    Returns (True, reason) when the trace should end at the current switchport:
      - No CDP/LLDP neighbor found (endpoint-facing port).
      - Neighbor is a VMware host (ESXi/vSwitch).
      - Neighbor is an access point.

    Returns (False, "") when the neighbor is a routable network device
    (switch or router) and the trace should continue to that device.
    """
    if not neighbor_info:
        return True, "No CDP/LLDP neighbor on this port — closest switchport"

    combined = (
        neighbor_info.get("neighbor_id", "") + " " + neighbor_info.get("platform", "")
    ).lower()

    if any(kw in combined for kw in _VMWARE_KEYWORDS):
        return True, f"Neighbor is VMware ({neighbor_info.get('neighbor_id', 'unknown')})"

    if any(kw in combined for kw in _AP_ROLE_KEYWORDS):
        return True, f"Neighbor is an AP ({neighbor_info.get('neighbor_id', 'unknown')})"

    return False, ""



def _log_intermediate_hop(
    hop_num: int,
    hostname: str,
    switch_ip: str,
    vlan: str,
    interface: str,
    portchannel_members: List[str],
    neighbor_info: Dict[str, str],
    neighbor_ip: str,
) -> None:
    """Print a single [HOP] line for an intermediate switch during the trace."""
    po_detail   = f" (members: {', '.join(portchannel_members)})" if portchannel_members else ""
    neighbor_id = neighbor_info.get("neighbor_id", "unknown")
    protocol    = neighbor_info.get("protocol", "CDP")
    print(
        f"[HOP {hop_num:>2}] {hostname} ({switch_ip})  "
        f"VLAN={vlan}  iface={interface}{po_detail}  "
        f"--{protocol}-->  {neighbor_id} ({neighbor_ip})"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — path dict assembly and summary output
# ─────────────────────────────────────────────────────────────────────────────


def build_path_dict(
    target_ip: str,
    mac: Optional[str],
    gateway_ip: str,
    downstream_hops: List[Dict],
    gateway_interface: Optional[str] = None,
    dst_route: Optional[List[Dict]] = None,
    dst_ip: str = "",
    stop_reason: str = "",
) -> Dict:
    """Reverse the downstream hop list to produce an upstream (device→gateway) path dict.

    The downstream trace visits switches in gateway→device order.  Each hop
    record contains:
      local_interface   – the interface on *that* switch pointing toward the device
                          (the MAC table result; egress in downstream, ingress in upstream)
      portchannel_members – physical members of local_interface if it is a port-channel
      remote_port       – "Port ID (outgoing port)" from CDP, i.e. the port on the
                          *neighbor* switch that connects back to us.  In upstream terms
                          this is the *egress* of the upstream hop whose d_idx is one
                          lower.

    Reversal formula (j = upstream hop index, d_idx = n-1-j):
      ingress_interface = downstream_hops[d_idx].local_interface
      egress_interface  = downstream_hops[d_idx-1].remote_port  (None when d_idx == 0)

    For the last upstream hop (the gateway, d_idx == 0), egress_interface is set
    to *gateway_interface* — the SVI or routed interface that carries *gateway_ip*.
    """
    n = len(downstream_hops)
    upstream_path: List[Dict] = []

    for j in range(n):
        d_idx = n - 1 - j
        d_hop = downstream_hops[d_idx]

        ingress         = d_hop.get("local_interface")
        ingress_members = d_hop.get("portchannel_members", [])

        if d_idx == 0:
            # This is the gateway — egress is the SVI / routed port with the gateway IP.
            egress = gateway_interface
        else:
            egress = downstream_hops[d_idx - 1].get("remote_port")

        upstream_path.append({
            "hop":                         j + 1,
            "hostname":                    d_hop.get("hostname"),
            "switch_ip":                   d_hop.get("switch_ip"),
            "vlan":                        d_hop.get("vlan"),
            "ingress_interface":           ingress,
            "ingress_portchannel_members": ingress_members,
            "egress_interface":            egress,
            "is_gateway":                  d_idx == 0,
            "interface_detail":            d_hop.get("interface_detail"),
        })

    return {
        "target_ip":   target_ip,
        "mac":         mac_to_cisco_fmt(mac) if mac else None,
        "gateway_ip":  gateway_ip,
        "total_hops":  n,
        "path":        upstream_path,
        "dst_route":   dst_route or [],
        "dst_ip":      dst_ip,
        "stop_reason": stop_reason,
    }


def print_path_summary(path_dict: Dict) -> None:
    """Print the complete device→gateway path to the console."""
    SEP = "=" * 64
    print()
    print(SEP)
    print("  PATH SUMMARY  (device --> gateway)")
    print(SEP)
    print(f"  Target IP  : {path_dict['target_ip']}")
    print(f"  ARP MAC    : {path_dict['mac'] or '—'}")
    print(f"  Gateway    : {path_dict['gateway_ip']}")
    print(f"  Total hops : {path_dict['total_hops']}")
    print(SEP)

    for hop in path_dict["path"]:
        hostname   = hop["hostname"] or hop["switch_ip"]
        sw_ip      = hop["switch_ip"]
        vlan       = hop["vlan"] or "—"
        ingress    = hop["ingress_interface"] or "—"
        i_mbrs     = hop.get("ingress_portchannel_members", [])
        is_gateway = hop.get("is_gateway", False)

        if is_gateway:
            gw_iface = hop["egress_interface"]
            egress   = f"{gw_iface}  ({path_dict['gateway_ip']})" if gw_iface else "(gateway — interface not resolved)"
        else:
            egress = hop["egress_interface"] or "—"

        print(f"\n  Hop {hop['hop']}: {hostname}  ({sw_ip})")
        print(f"    VLAN    : {vlan}")
        print(f"    Ingress : {ingress}", end="")
        if i_mbrs:
            print(f"  [members: {', '.join(i_mbrs)}]", end="")
        print()
        print(f"    Egress  : {egress}")

    dst_routes = path_dict.get("dst_route", [])
    if dst_routes:
        queried = path_dict.get("dst_ip", "")
        print(f"\n  Destination Routes  ({queried})")
        for idx, r in enumerate(dst_routes, 1):
            prefix = r.get("prefix") or queried
            nh     = r.get("next_hop") or "?"
            iface  = r.get("exit_interface", "")
            src    = r.get("route_source", "")
            tag    = r.get("route_tag", "")
            age    = r.get("route_age", "")
            line   = f"    {idx}. {prefix}  via {nh}"
            if iface:
                line += f"  [{iface}]"
            if src:
                line += f"  [{src}]"
            if tag:
                line += f"  tag:{tag}"
            if age:
                line += f"  age:{age}"
            print(line)

    print()
    print(SEP)
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3 — L3 path trace (gateway → destination, all ECMP paths)
# ─────────────────────────────────────────────────────────────────────────────


def _run_l2_at_final_hop(
    client: CiscoDeviceClient,
    device_type: str,
    dst_ip: str,
    topology_only: bool = False,
) -> Dict:
    """Run a Layer 2 trace for *dst_ip* on the device that has its subnet directly connected.

    Executes the same three-step flow used in the initial L2 trace:
      1. ``show ip arp <dst_ip>``                    → resolve to MAC address
      2. ``show mac address-table address <mac>``     → VLAN + switchport
      3. ``show cdp neighbors <port> detail``         → next-hop device (if any)

    Returns a dict with keys:
      dst_ip, mac, vlan, port, portchannel_members, cdp_neighbor, error
    """
    result: Dict = {
        "dst_ip":              dst_ip,
        "mac":                 None,
        "vlan":                None,
        "port":                None,
        "portchannel_members": [],
        "interface_detail":    None,
        "cdp_neighbor":        None,
        "error":               None,
    }

    # Step 1 — ARP lookup
    print(f"[L2]   show ip arp {dst_ip}")
    mac = arp_lookup(client, device_type, dst_ip)
    if not mac:
        result["error"] = f"No ARP entry for {dst_ip}"
        return result

    result["mac"] = mac_to_cisco_fmt(mac)
    print(f"[L2]   ARP: {dst_ip} → {mac_to_cisco_fmt(mac)}")

    # Step 2 — MAC address-table lookup
    print(f"[L2]   show mac address-table address {mac_to_cisco_fmt(mac)}")
    mac_entry = mac_table_lookup(client, device_type, mac)
    if not mac_entry:
        result["error"] = f"MAC {mac_to_cisco_fmt(mac)} not in address table"
        return result

    result["vlan"] = mac_entry["vlan"]
    result["port"] = mac_entry["interface"]
    print(f"[L2]   MAC table: VLAN={mac_entry['vlan']}  Port={mac_entry['interface']}")

    # Step 2a — interface health / counter detail (skipped in topology_only mode)
    result["interface_detail"] = (
        {} if topology_only
        else get_interface_detail(client, device_type, mac_entry["interface"])
    )

    # Step 2b — port-channel expansion (if applicable)
    check_iface = mac_entry["interface"]
    if is_portchannel(mac_entry["interface"]):
        members = get_portchannel_members(client, device_type, mac_entry["interface"])
        result["portchannel_members"] = members
        if members:
            print(f"[L2]   Port-channel members: {', '.join(members)}")
            check_iface = members[0]

    # Step 3 — CDP/LLDP neighbor on the resolved physical port
    print(f"[L2]   show cdp neighbors {check_iface} detail")
    neighbor = get_neighbor_info(client, device_type, check_iface)
    if neighbor:
        result["cdp_neighbor"] = {
            "hostname": neighbor.get("neighbor_id"),
            "ip":       neighbor.get("neighbor_ip"),
            "platform": neighbor.get("platform"),
            "port":     neighbor.get("remote_port"),
            "protocol": neighbor.get("protocol", "CDP"),
        }
        nid = neighbor.get("neighbor_id", "unknown")
        nip = neighbor.get("neighbor_ip", "")
        print(f"[L2]   CDP neighbor: {nid}" + (f" ({nip})" if nip else ""))
    else:
        print(f"[L2]   No CDP/LLDP neighbor on {check_iface} — endpoint port")

    return result


def get_bgp_route_detail(
    client: CiscoDeviceClient,
    prefix: str,
) -> Dict:
    """Run ``show ip bgp <prefix>`` and parse key BGP attributes.

    Returns a dict with keys (all may be None):
      bgp_as_path    — full AS-path string, e.g. ``"65001 65002 65003"``
      bgp_community  — community string, e.g. ``"65001:100 65002:200"``
      bgp_local_pref — local preference (int)
      bgp_origin     — origin code: ``"igp"`` / ``"egp"`` / ``"incomplete"``
      bgp_med        — multi-exit discriminator (int)
      bgp_weight     — weight (IOS-specific, int)
    """
    result: Dict = {
        "bgp_as_path":    None,
        "bgp_community":  None,
        "bgp_local_pref": None,
        "bgp_origin":     None,
        "bgp_med":        None,
        "bgp_weight":     None,
    }
    if not prefix:
        return result
    try:
        output = _send_cmd(client, f"show ip bgp {prefix}")
    except Exception as exc:
        log.debug("get_bgp_route_detail(%r): %s", prefix, exc)
        return result
    if not output or "network not in table" in output.lower():
        return result

    # AS-path — one or more lines of space-separated AS numbers, right after the
    # next-hop IP line.  The best-path line is marked with '*>'.
    # "    65001 65002 65003" or "    65454" or "    (empty for iBGP)"
    m = re.search(
        r"^\s+\*?[>i\s]*(\d[\d\s]*\d|\d+)\s*$",
        output, re.MULTILINE,
    )
    if m:
        result["bgp_as_path"] = m.group(1).strip()

    # Local preference
    m = re.search(r"\blocalpref\s+(\d+)\b", output, re.IGNORECASE)
    if m:
        result["bgp_local_pref"] = int(m.group(1))

    # MED
    m = re.search(r"\bmetric\s+(\d+)\b", output, re.IGNORECASE)
    if m:
        result["bgp_med"] = int(m.group(1))

    # Weight (IOS)
    m = re.search(r"\bweight\s+(\d+)\b", output, re.IGNORECASE)
    if m:
        result["bgp_weight"] = int(m.group(1))

    # Origin
    m = re.search(r"\bOrigin\s+(igp|egp|incomplete)\b", output, re.IGNORECASE)
    if m:
        result["bgp_origin"] = m.group(1).lower()

    # Community — IOS: "Community: 65001:100 65002:200"
    m = re.search(r"Community:\s*(.+?)(?:\r?\n|$)", output, re.IGNORECASE)
    if m:
        result["bgp_community"] = m.group(1).strip()

    return result


def _lookup_neighbor_for_ip(
    nb_url:     str,
    nb_token:   str,
    target_ip:  str,
    verify_ssl: bool = True,
) -> Tuple[Optional[str], Optional[str]]:
    """
    NetBox lookup: given a router interface IP, find the switch that is directly
    connected to that router port (i.e. the device on the other end of the cable).

    This is the key to resolving multiple CDP neighbors: when EJ-WAN-DIS-02 sees
    9 devices on Twe1/0/24, we look up the target IP (198.18.1.91) in NetBox,
    find it on UMC-WAN-RTR-02 Te0/0/2, follow the cable to UMC-WAN-DIS-02,
    and match that name (stripped + lowercased) against the CDP list.

    Returns (neighbor_device_name_lower, neighbor_primary_ip) or (None, None).
    """
    try:
        nb = _get_nb_client(nb_url, nb_token, verify_ssl)

        # Step 1 — find the IP record
        ip_recs: list = []
        for addr in (f"{target_ip}/32", target_ip):
            ip_recs = list(nb.nb.ipam.ip_addresses.filter(address=addr))
            if ip_recs:
                break
        if not ip_recs:
            log.debug("NetBox: IP %s not found", target_ip)
            return None, None

        rec      = ip_recs[0]
        obj_type = str(getattr(rec, "assigned_object_type", "") or "")
        obj_id   = getattr(rec, "assigned_object_id", None)
        if not obj_id or "dcim.interface" not in obj_type:
            return None, None

        # Step 2 — get the interface record
        iface = nb.nb.dcim.interfaces.get(obj_id)
        if not iface:
            return None, None

        # Step 3 — follow the cable to the connected endpoint
        connected = None
        for attr in ("connected_endpoint", "link_peers", "connected_endpoints"):
            val = getattr(iface, attr, None)
            if not val:
                continue
            connected = val[0] if isinstance(val, list) else val
            break

        if not connected:
            # No cable data — return the device that owns the IP itself
            dev = getattr(iface, "device", None)
            if dev:
                return str(dev.name).lower(), None
            return None, None

        # Step 4 — get the name of the connected device
        connected_dev = getattr(connected, "device", None)
        if not connected_dev:
            return None, None

        devs = nb.get_devices({"id": int(connected_dev.id)})
        if not devs:
            return None, None

        device    = devs[0]
        dev_name  = device.get("name", "").lower()
        mgmt_ip   = nb.get_device_mgmt_ip(device)
        print(
            f"[L3]   NetBox: {target_ip} is on "
            f"{getattr(iface.device, 'name', '?')}/{iface} "
            f"→ connected to {dev_name}"
        )
        return dev_name, mgmt_ip

    except Exception as exc:
        log.debug("_lookup_neighbor_for_ip(%s): %s", target_ip, exc)
        return None, None


def _select_neighbor_for_target(
    neighbors:  List[Dict[str, str]],
    target_ip:  str,
    nb_url:     str,
    nb_token:   str,
    verify_ssl: bool = True,
) -> Optional[Dict[str, str]]:
    """
    Choose the correct CDP neighbor from a list of multiple candidates.

    Priority:
      1. A neighbor whose management IP equals target_ip directly.
      2. A neighbor whose device name (stripped of domain, lowercased) matches
         the switch that NetBox says is connected to the target_ip interface.
      3. Fall back to the first neighbor.
    """
    # Direct IP match
    for n in neighbors:
        if n.get("neighbor_ip") == target_ip:
            print(f"[L3]   Neighbor {n.get('neighbor_id')} matches target IP {target_ip} directly")
            return n

    # NetBox resolution
    if nb_url and nb_token:
        target_dev_name, _ = _lookup_neighbor_for_ip(nb_url, nb_token, target_ip, verify_ssl)
        if target_dev_name:
            for n in neighbors:
                cdp_name = n.get("neighbor_id", "").lower().split(".")[0]
                if cdp_name == target_dev_name:
                    print(
                        f"[L3]   NetBox matched {n.get('neighbor_id')} "
                        f"({n.get('neighbor_ip')}) for target {target_ip}"
                    )
                    return n
            print(
                f"[L3]   WARNING: NetBox says {target_dev_name!r} is the neighbor "
                f"but it was not found in CDP list "
                f"({', '.join(n.get('neighbor_id','?') for n in neighbors)})"
            )

    # Fallback to first
    return neighbors[0] if neighbors else None


def _cdp_l2_walk_between_l3_hops(
    client:         CiscoDeviceClient,
    device_type:    str,
    exit_interface: str,
    next_hop_ip:    str,
    creds:          Dict[str, str],
    max_l2_hops:    int = 8,
    nb_url:         str = "",
    nb_token:       str = "",
    verify_ssl:     bool = True,
) -> List[Dict]:
    """Discover intermediate L2 switches between two L3 hops for non-BGP routes.

    When the routing protocol is OSPF / EIGRP / static (anything except BGP)
    the exit_interface on the current L3 router is a physical port.  That port
    may be connected to one or more L2 switches before reaching the next-hop
    L3 router.  This function walks those switches using CDP, ARP, and MAC
    tables, collecting interface details along the way.

    Algorithm
    ---------
    1. show cdp neighbors <exit_interface> detail  (on the already-open client)
       - No neighbor               → no L2 switches; return []
       - Neighbor IS next_hop_ip   → direct L3-to-L3 link; return []
       - Neighbor is another device → intermediate switch; continue

    2. For each intermediate switch (up to max_l2_hops):
       a. SSH in and get its hostname.
       b. show interface <incoming_port>   → health counters (incoming side)
       c. show ip arp <next_hop_ip>        → resolve next-hop MAC
       d. show mac address-table <mac>     → find egress port
       e. show interface <egress_port>     → health counters (egress side)
       f. show cdp neighbors <egress_port> detail
          - Neighbor IS next_hop_ip → done
          - Neighbor is another device → continue walk

    Returns
    -------
    list of dicts, one per intermediate L2 switch:
      {
        "device":           str  (hostname),
        "switch_ip":        str  (SSH IP),
        "interface":        str  (incoming port facing upstream router),
        "egress_interface": str | None  (outgoing port facing next-hop router),
        "interface_detail": dict (show interface on incoming port),
        "egress_detail":    dict (show interface on egress port),
        "note":             str  (error or informational note, may be empty),
      }
    """
    l2_hops:  List[Dict] = []
    visited:  set        = set()

    # ── Step 1: all CDP neighbors on the current L3 router's egress interface ──
    print(f"[L3]   L2 walk: show cdp neighbors {exit_interface} detail")
    neighbors = get_all_cdp_neighbors(client, device_type, exit_interface)

    if not neighbors:
        return []   # No CDP neighbour — direct physical link, nothing to add

    # Pick the correct neighbour (direct IP match → NetBox resolution → first)
    neighbor    = _select_neighbor_for_target(neighbors, next_hop_ip, nb_url, nb_token, verify_ssl)
    neighbor_ip = neighbor.get("neighbor_ip") if neighbor else None
    neighbor_id = (neighbor.get("neighbor_id") or "unknown") if neighbor else "unknown"

    if neighbor_ip == next_hop_ip:
        print(f"[L3]   CDP on {exit_interface}: direct L3 neighbor {neighbor_id} ({neighbor_ip})")
        return []

    print(f"[L3]   CDP on {exit_interface}: intermediate switch {neighbor_id} ({neighbor_ip})")

    current_ip    = neighbor_ip
    incoming_port = neighbor.get("remote_port") if neighbor else None

    # ── Step 2: Obtain the MAC address for next_hop_ip from the STARTING router ──
    # ARP must be run HERE (on the L3 router we're already connected to) because
    # intermediate L2 switches do not have ARP entries for remote routed IPs.
    # This MAC is then carried through every switch via MAC-table lookups only.
    print(f"[L3]   show ip arp {next_hop_ip} → get MAC for L2 tracking")
    target_mac: Optional[str] = arp_lookup(client, device_type, next_hop_ip)
    if target_mac:
        print(f"[L3]   ARP: {next_hop_ip} → {mac_to_cisco_fmt(target_mac)}")
    else:
        print(f"[L3]   Warning: no ARP for {next_hop_ip} on this device — MAC-table walk may fail")

    # ── Step 3: Walk through intermediate L2 switches ────────────────────────────
    for _ in range(max_l2_hops):
        if not current_ip or current_ip in visited:
            break
        visited.add(current_ip)

        print(f"[L3]   Connecting to intermediate device {current_ip}...")

        try:
            sw = _open_device_client(current_ip, "ios", creds)
        except GatewayConnectionError as exc:
            print(f"[L3]   L2 walk: cannot connect to {current_ip}: {exc}")
            l2_hops.append({
                "device":           current_ip,
                "switch_ip":        current_ip,
                "interface":        incoming_port or "—",
                "egress_interface": None,
                "interface_detail": {},
                "egress_detail":    {},
                "note":             f"Cannot connect: {exc}",
            })
            break

        sw_hostname    = current_ip
        incoming_det:  Dict                    = {}
        outgoing_port: Optional[str]           = None
        outgoing_det:  Dict                    = {}
        next_neighbor: Optional[Dict[str,str]] = None
        note:          str                     = ""
        found_target:  bool                    = False

        try:
            try:
                sw_hostname = (
                    sw._cli_connection.find_prompt().rstrip("#>").strip()
                    or current_ip
                )
            except Exception:
                pass

            print(f"[L3]   Device: {sw_hostname} ({current_ip})")

            # Interface counters for the incoming port (toward upstream L3 router)
            if incoming_port:
                print(f"[L3]   show interface {incoming_port}")
                incoming_det = get_interface_detail(sw, "ios", incoming_port)

            # ── CHECK 1: is next_hop_ip on this device's own interfaces? ─────
            # If yes, we've reached the destination router (the BGP peer).
            # L2 switches have no routed IPs so this will only match on a router.
            try:
                ip_brief = _send_cmd(sw, "show ip int brief")
                if next_hop_ip in ip_brief:
                    print(
                        f"[L3]   Found {next_hop_ip} on {sw_hostname} — "
                        f"reached the BGP peer device"
                    )
                    found_target = True
            except Exception:
                pass

            if not found_target:
                # ── CHECK 2: MAC-table lookup using the MAC obtained earlier ──
                # Do NOT ARP here — L2 switches won't have the ARP entry.
                if target_mac:
                    cisco_mac = mac_to_cisco_fmt(target_mac)
                    print(f"[L3]   show mac address-table address {cisco_mac}")
                    mac_entry = mac_table_lookup(sw, "ios", target_mac)
                    if mac_entry:
                        outgoing_port = mac_entry["interface"]
                        print(f"[L3]   Egress port: {outgoing_port}")
                        outgoing_det = get_interface_detail(sw, "ios", outgoing_port)

                        # CDP on egress port — may return multiple neighbours
                        check = outgoing_port
                        if is_portchannel(outgoing_port):
                            mbrs = get_portchannel_members(sw, "ios", outgoing_port)
                            if mbrs:
                                check = mbrs[0]

                        print(f"[L3]   show cdp neighbors {check} detail")
                        next_neighbors = get_all_cdp_neighbors(sw, "ios", check)
                        if next_neighbors:
                            next_neighbor = _select_neighbor_for_target(
                                next_neighbors, next_hop_ip,
                                nb_url, nb_token, verify_ssl,
                            )
                    else:
                        note = f"MAC {cisco_mac} not in address-table on {sw_hostname}"
                        print(f"[L3]   {note}")
                else:
                    # MAC not available — try ARP as last resort (some routed switches may respond)
                    fallback_mac = arp_lookup(sw, "ios", next_hop_ip)
                    if fallback_mac:
                        target_mac = fallback_mac
                        print(f"[L3]   Fallback ARP succeeded: {next_hop_ip} → {mac_to_cisco_fmt(target_mac)}")
                        mac_entry = mac_table_lookup(sw, "ios", target_mac)
                        if mac_entry:
                            outgoing_port = mac_entry["interface"]
                            outgoing_det  = get_interface_detail(sw, "ios", outgoing_port)
                            check = outgoing_port
                            next_neighbors = get_all_cdp_neighbors(sw, "ios", check)
                            if next_neighbors:
                                next_neighbor = _select_neighbor_for_target(
                                    next_neighbors, next_hop_ip,
                                    nb_url, nb_token, verify_ssl,
                                )
                    else:
                        note = f"No MAC and no ARP for {next_hop_ip} on {sw_hostname}"
                        print(f"[L3]   {note}")

        finally:
            try:
                sw._cli_disconnect()
            except Exception:
                pass

        l2_hops.append({
            "device":           sw_hostname,
            "switch_ip":        current_ip,
            "interface":        incoming_port or "—",
            "egress_interface": outgoing_port,
            "interface_detail": incoming_det,
            "egress_detail":    outgoing_det,
            "note":             note,
        })

        # If we found the target IP on this device it is the BGP peer router — stop.
        if found_target:
            break

        if next_neighbor:
            next_ip = next_neighbor.get("neighbor_ip")
            if next_ip == next_hop_ip:
                print(f"[L3]   Reached next-hop router {next_hop_ip} via {sw_hostname}")
                break
            if next_ip and next_ip not in visited:
                incoming_port = next_neighbor.get("remote_port")
                current_ip    = next_ip
                continue

        break  # no more CDP neighbours — end of walk

    return l2_hops


def run_l3_path_trace(
    gateway_ip: str,
    gw_hostname: str,
    gw_ingress_interface: Optional[str],
    dst_ip: str,
    initial_routes: List[Dict],
    creds: Dict[str, str],
    src_ip: str = "",
    nb_url: str = "",
    nb_token: str = "",
    verify_ssl: bool = True,
    max_hops: int = 15,
    topology_only: bool = False,
) -> List[List[Dict]]:
    """BFS traversal of all ECMP L3 paths from *gateway_ip* toward *dst_ip*.

    At every hop:
      - ``show ip route <src_ip>``   → ingress interface (reverse-path to source)
      - ``show ip route <dst_ip>``   → egress routes (all ECMP next-hops onward)

    Each unique next-hop spawns a new path branch.  Returns a list of
    complete paths, each path being an ordered list of hop dicts.

    Stop conditions per branch:
      - Destination is directly connected on the current device.
      - No route found for dst_ip.
      - Cannot connect to next-hop.
      - Loop detected (IP already visited on this branch).
      - max_hops reached.
    """
    from collections import deque  # noqa: PLC0415

    complete_paths: List[List[Dict]] = []

    gw_hop: Dict = {
        "hostname":           gw_hostname,
        "ip":                 gateway_ip,
        "ingress_interfaces": [gw_ingress_interface] if gw_ingress_interface else [],
        "egress_routes":      initial_routes,
        "note":               "",
    }

    # If the destination is already directly reachable from the gateway, done.
    if any(r.get("next_hop") == "directly connected" for r in initial_routes):
        return [[gw_hop]]

    # Seed the BFS queue: (path_so_far, next_hop_to_visit, visited_ips_on_this_branch)
    # Each branch gets its own copy of gw_hop stamped with the specific route it follows
    # so print_l3_paths can label the path and show the exact egress interface.
    queue: deque = deque()
    for route in initial_routes:
        nh = route.get("next_hop")
        if nh and nh != "directly connected":
            branch_gw_hop = dict(gw_hop)
            branch_gw_hop["selected_route"] = route   # the one route this branch follows
            queue.append(([branch_gw_hop], nh, {gateway_ip}))

    if not queue:
        return [[gw_hop]]

    while queue:
        path_so_far, current_ip, visited = queue.popleft()

        if current_ip in visited:
            complete_paths.append(
                path_so_far + [{"hostname": current_ip, "ip": current_ip,
                                "note": f"Loop detected at {current_ip}",
                                "ingress_interfaces": [], "egress_routes": []}]
            )
            continue

        if len(path_so_far) >= max_hops:
            complete_paths.append(
                path_so_far + [{"hostname": current_ip, "ip": current_ip,
                                "note": f"Max hops ({max_hops}) reached",
                                "ingress_interfaces": [], "egress_routes": []}]
            )
            continue

        print(f"[L3] Hop {len(path_so_far) + 1}: connecting to {current_ip}...")

        # ── Connection with NetBox primary-IP fallback ────────────────────────
        connect_ip = current_ip
        client     = None
        connect_err: str = ""

        # ── Try direct SSH first ──────────────────────────────────────────────
        try:
            client = _open_device_client(connect_ip, "ios", creds)
        except GatewayConnectionError as exc:
            connect_err = str(exc)
            log.debug("Direct connect to %s failed: %s", connect_ip, exc)

            # ── Fallback: resolve the IP to a Cisco device in NetBox ──────────
            if nb_url and nb_token:
                print(f"[L3]   Cannot reach {current_ip} — querying NetBox to resolve device...")
                primary_ip = _resolve_mgmt_ip_from_netbox(
                    nb_url, nb_token, current_ip, verify_ssl
                )
                if primary_ip and primary_ip != current_ip:
                    print(f"[L3]   Connecting to device primary IPv4: {primary_ip}...")
                    connect_ip = primary_ip
                    try:
                        client = _open_device_client(connect_ip, "ios", creds)
                        connect_err = ""
                        print(f"[L3]   Connected via primary IP {connect_ip}")
                    except GatewayConnectionError as exc2:
                        connect_err = str(exc2)
                        log.debug(
                            "Primary IP connect to %s also failed: %s", connect_ip, exc2
                        )
                elif primary_ip == current_ip:
                    log.debug(
                        "NetBox primary IP for %s is the same address — no fallback available",
                        current_ip,
                    )

        if client is None:
            note = f"Cannot connect to {current_ip}"
            if connect_ip != current_ip:
                note += f"; also tried NetBox primary {connect_ip}"
            if connect_err:
                note += f": {connect_err}"

            unreachable_hop: Dict = {
                "hostname":           current_ip,
                "ip":                 current_ip,
                "connect_ip":         connect_ip,
                "note":               note,
                "ingress_interfaces": [],
                "egress_routes":      [],
                "l2_trace":           None,
                "l2_trace_device":    None,
            }

            # ── L2 fallback ───────────────────────────────────────────────────
            # When the next-hop is unreachable, reconnect to the LAST
            # successfully-reached device and run an ARP→MAC→CDP trace for
            # dst_ip.  This recovers the egress L2 port even when the
            # upstream L3 next-hop cannot be SSH'd into.
            if path_so_far:
                prev_hop         = path_so_far[-1]
                prev_connect_ip  = prev_hop.get("connect_ip") or prev_hop.get("ip")
                prev_hostname    = prev_hop.get("hostname") or prev_connect_ip
                if prev_connect_ip:
                    print(
                        f"[L3]   Cannot reach {current_ip} — "
                        f"attempting L2 trace for {dst_ip} on {prev_hostname} ({prev_connect_ip})..."
                    )
                    try:
                        prev_client = _open_device_client(prev_connect_ip, "ios", creds)
                        try:
                            l2_result = _run_l2_at_final_hop(prev_client, "ios", dst_ip)
                            unreachable_hop["l2_trace"]        = l2_result
                            unreachable_hop["l2_trace_device"] = prev_hostname
                            if l2_result.get("error"):
                                print(f"[L3]   L2 fallback: {l2_result['error']}")
                            else:
                                port = l2_result.get("port", "?")
                                mac  = l2_result.get("mac", "?")
                                print(
                                    f"[L3]   L2 fallback succeeded on {prev_hostname}: "
                                    f"MAC={mac}  port={port}"
                                )
                        finally:
                            try:
                                prev_client._cli_disconnect()
                            except Exception:
                                pass
                    except GatewayConnectionError as exc:
                        log.debug(
                            "L2 fallback: cannot reconnect to %s: %s",
                            prev_connect_ip, exc,
                        )

            complete_paths.append(path_so_far + [unreachable_hop])
            continue

        hostname        = current_ip
        ingress_ifaces: List[str]  = []
        egress_routes:  List[Dict] = []
        l2_trace:       Optional[Dict] = None

        try:
            try:
                hostname = client._cli_connection.find_prompt().rstrip("#>").strip() or current_ip
            except Exception:
                pass

            # Reverse-path lookup: show ip route <src_ip> reveals which
            # interface traffic ARRIVES ON.  For BGP recursive routes a second
            # lookup on the BGP next-hop is performed automatically.
            ingress_lookup_ip = src_ip or path_so_far[-1]["ip"]
            print(f"[L3]   show ip route {ingress_lookup_ip} → ingress interface")
            ingress_ifaces = _resolve_ingress_interfaces(client, ingress_lookup_ip)

            print(f"[L3]   show ip route {dst_ip} → egress routes")
            egress_routes = get_routes_for_ip(client, dst_ip)

            # ── Enrich BGP routes with community / AS-path / local-pref ─────
            # Done here (while session is open) for both topology and full mode
            # because BGP attributes are routing topology, not interface counters.
            _seen_bgp_prefixes: set = set()
            for _r in egress_routes:
                _rsrc = (_r.get("route_source") or "").lower()
                _pfx  = _r.get("prefix")
                if "bgp" in _rsrc and _pfx and _pfx not in _seen_bgp_prefixes:
                    _seen_bgp_prefixes.add(_pfx)
                    print(f"[L3]   show ip bgp {_pfx} → BGP attributes")
                    _bgp = get_bgp_route_detail(client, _pfx)
                    _r.update({k: v for k, v in _bgp.items() if v is not None})

            # ── Collect interface detail for ingress + egress interfaces ─────
            # Skipped in topology_only mode — enrichment phase collects these.
            _seen_ifaces: set = set()
            iface_details: Dict[str, Dict] = {}
            if not topology_only:
                for _iface in ingress_ifaces:
                    if _iface and _iface not in _seen_ifaces:
                        iface_details[_iface] = get_interface_detail(client, "ios", _iface)
                        _seen_ifaces.add(_iface)
                for _r in egress_routes:
                    _ei = _r.get("exit_interface")
                    if _ei and _ei not in _seen_ifaces:
                        iface_details[_ei] = get_interface_detail(client, "ios", _ei)
                        _seen_ifaces.add(_ei)

            # ── L2 intermediate walk — non-BGP AND BGP routes ────────────────
            # Non-BGP (OSPF/EIGRP/static): the exit_interface is a physical
            #   port → walk CDP directly from that interface.
            # BGP: the next-hop is a loopback/VIP.  First resolve it to its
            #   physical IGP path ("show ip route <bgp_nh>"), then walk CDP
            #   from the physical interface to capture the L2 switches that
            #   sit between this router and the BGP peer.
            l2_intermediate: List[Dict] = []
            if not any(r.get("next_hop") == "directly connected" for r in egress_routes):
                for _r in egress_routes:
                    _rsrc = (_r.get("route_source") or "").lower()
                    _ei   = _r.get("exit_interface")
                    _nh   = _r.get("next_hop")
                    if not _nh or _nh == "directly connected":
                        continue

                    if "bgp" not in _rsrc and _ei:
                        # ── Non-BGP: walk from the route's physical exit interface ──
                        print(
                            f"[L3]   Non-BGP ({_rsrc}) via {_ei} "
                            f"→ L2 walk toward {_nh}"
                        )
                        _walked = _cdp_l2_walk_between_l3_hops(
                            client, "ios", _ei, _nh, creds,
                            nb_url=nb_url, nb_token=nb_token, verify_ssl=verify_ssl,
                        )
                        if _walked:
                            l2_intermediate.extend(_walked)
                            print(f"[L3]   Found {len(_walked)} L2 switch(es) toward {_nh}")
                        break

                    elif "bgp" in _rsrc:
                        # ── BGP: resolve next-hop to physical path first ──────────
                        print(
                            f"[L3]   BGP route — resolving next-hop {_nh} "
                            f"to physical path before L2 walk..."
                        )
                        bgp_nh_routes = get_routes_for_ip(client, _nh)
                        for _bgp_r in bgp_nh_routes:
                            _bgp_rsrc = (_bgp_r.get("route_source") or "").lower()
                            _bgp_ei   = _bgp_r.get("exit_interface")
                            _bgp_nh   = _bgp_r.get("next_hop")
                            if (
                                "bgp" not in _bgp_rsrc
                                and _bgp_ei
                                and _bgp_nh
                                and _bgp_nh != "directly connected"
                            ):
                                print(
                                    f"[L3]   BGP next-hop {_nh} resolves via "
                                    f"{_bgp_rsrc}: physical {_bgp_nh} on {_bgp_ei}"
                                )
                                _walked = _cdp_l2_walk_between_l3_hops(
                                    client, "ios", _bgp_ei, _bgp_nh, creds,
                                    nb_url=nb_url, nb_token=nb_token, verify_ssl=verify_ssl,
                                )
                                if _walked:
                                    l2_intermediate.extend(_walked)
                                    print(
                                        f"[L3]   Found {len(_walked)} L2 switch(es) "
                                        f"on path to BGP peer {_nh}"
                                    )
                                break
                        break  # one walk per L3 hop

            # If this device has the destination subnet directly connected,
            # run the full L2 trace (ARP → MAC table → CDP) while still connected.
            if any(r.get("next_hop") == "directly connected" for r in egress_routes):
                print(f"[L3]   {dst_ip} subnet is directly connected — running L2 trace...")
                l2_trace = _run_l2_at_final_hop(client, "ios", dst_ip, topology_only=topology_only)

        finally:
            try:
                client._cli_disconnect()
            except Exception:
                pass

        current_hop: Dict = {
            "hostname":           hostname,
            "ip":                 current_ip,
            "connect_ip":         connect_ip,
            "ingress_interfaces": ingress_ifaces,
            "egress_routes":      egress_routes,
            "interface_details":  iface_details,
            "l2_trace":           l2_trace,
            "l2_intermediate":    l2_intermediate,
            "note":               "",
        }
        new_path    = path_so_far + [current_hop]
        new_visited = visited | {current_ip, connect_ip}  # prevent re-visiting either IP

        if not egress_routes:
            current_hop["note"] = "No route to destination"
            complete_paths.append(new_path)
            continue

        if any(r.get("next_hop") == "directly connected" for r in egress_routes):
            complete_paths.append(new_path)
            continue

        # Expand ECMP — each unique next-hop spawns a new branch.
        next_hops: List[str] = []
        for r in egress_routes:
            nh = r.get("next_hop")
            if nh and nh != "directly connected" and nh not in next_hops:
                next_hops.append(nh)

        if not next_hops:
            current_hop["note"] = "No reachable next-hop"
            complete_paths.append(new_path)
            continue

        for nh in next_hops:
            queue.append((new_path, nh, new_visited))

    return complete_paths


def print_l3_paths(paths: List[List[Dict]], dst_ip: str) -> None:
    """Print all L3 ECMP paths (gateway → destination) to the console."""
    if not paths:
        return

    SEP = "=" * 64
    print()
    print(SEP)
    print(f"  L3 PATH TRACE  (gateway --> {dst_ip})")
    print(SEP)

    for path_num, path in enumerate(paths, 1):
        # Label the path with the specific route (next-hop + egress interface) it follows.
        first_hop = path[0] if path else {}
        sel = first_hop.get("selected_route", {})
        path_nh    = sel.get("next_hop", "")
        path_iface = sel.get("exit_interface", "")
        path_label = f"  via {path_nh}" + (f"  [{path_iface}]" if path_iface else "")
        print(f"\n  Path {path_num}:{path_label}")

        for hop_num, hop in enumerate(path, 1):
            hostname = hop.get("hostname") or hop.get("ip", "unknown")
            ip       = hop.get("ip", "")
            note     = hop.get("note", "")

            connect_ip = hop.get("connect_ip", ip)
            ip_label   = f"({ip})" if connect_ip == ip else f"({ip})  [connected via {connect_ip}]"
            print(f"\n    Hop {hop_num}: {hostname}  {ip_label}")

            if note:
                print(f"      [{note}]")
                l2_fb = hop.get("l2_trace")
                if l2_fb:
                    l2_dev = hop.get("l2_trace_device") or "prev-device"
                    print(f"\n      Layer 2 Fallback Trace ({l2_dev} → {l2_fb.get('dst_ip', dst_ip)})")
                    if l2_fb.get("error"):
                        print(f"        Error       : {l2_fb['error']}")
                    else:
                        if l2_fb.get("mac"):
                            print(f"        ARP MAC     : {l2_fb['mac']}")
                        if l2_fb.get("vlan"):
                            print(f"        VLAN        : {l2_fb['vlan']}")
                        if l2_fb.get("port"):
                            print(f"        Port        : {l2_fb['port']}")
                        if l2_fb.get("portchannel_members"):
                            print(f"        Po members  : {', '.join(l2_fb['portchannel_members'])}")
                        cdp = l2_fb.get("cdp_neighbor")
                        if cdp:
                            nid   = cdp.get("hostname", "unknown")
                            nip   = cdp.get("ip", "")
                            nport = cdp.get("port", "")
                            plat  = cdp.get("platform", "")
                            proto = cdp.get("protocol", "CDP")
                            print(
                                f"        {proto} Neighbor: {nid}"
                                + (f"  ({nip})" if nip else "")
                            )
                            if nport:
                                print(f"        Remote Port : {nport}")
                            if plat:
                                print(f"        Platform    : {plat}")
                        else:
                            print(f"        CDP         : no neighbor on port (endpoint)")
                continue

            ifaces = hop.get("ingress_interfaces", [])
            if ifaces:
                print(f"      Ingress : {', '.join(ifaces)}")

            # For the gateway hop: show the specific egress interface this path uses.
            sel_route = hop.get("selected_route")
            if sel_route:
                sel_nh    = sel_route.get("next_hop", "?")
                sel_iface = sel_route.get("exit_interface", "")
                sel_age   = sel_route.get("route_age", "")
                sel_tag   = sel_route.get("route_tag", "")
                egress_line = f"      Egress  : {sel_iface or '—'}  (→ {sel_nh})"
                if sel_age:
                    egress_line += f"  age:{sel_age}"
                if sel_tag:
                    egress_line += f"  tag:{sel_tag}"
                print(egress_line)

            reached = False
            for r in hop.get("egress_routes", []):
                prefix = r.get("prefix") or dst_ip
                nh     = r.get("next_hop") or "?"
                iface  = r.get("exit_interface", "")
                src    = r.get("route_source", "")
                tag    = r.get("route_tag", "")
                age    = r.get("route_age", "")
                line   = f"      Route   : {prefix}  via {nh}"
                if iface:
                    line += f"  [{iface}]"
                if src:
                    line += f"  [{src}]"
                if tag:
                    line += f"  tag:{tag}"
                if age:
                    line += f"  age:{age}"
                print(line)
                if nh == "directly connected":
                    reached = True

            if reached:
                print(f"      [DESTINATION REACHED]")
                l2 = hop.get("l2_trace")
                if l2:
                    print(f"\n      Layer 2 Trace  ({l2.get('dst_ip', '')})")
                    if l2.get("error"):
                        print(f"        Error       : {l2['error']}")
                    else:
                        if l2.get("mac"):
                            print(f"        ARP MAC     : {l2['mac']}")
                        if l2.get("vlan"):
                            print(f"        VLAN        : {l2['vlan']}")
                        if l2.get("port"):
                            print(f"        Port        : {l2['port']}")
                        if l2.get("portchannel_members"):
                            print(f"        Po members  : {', '.join(l2['portchannel_members'])}")
                        cdp = l2.get("cdp_neighbor")
                        if cdp:
                            name  = cdp.get("hostname", "unknown")
                            nip   = cdp.get("ip", "")
                            nport = cdp.get("port", "")
                            plat  = cdp.get("platform", "")
                            proto = cdp.get("protocol", "CDP")
                            print(
                                f"        {proto} Neighbor: {name}"
                                + (f"  ({nip})" if nip else "")
                            )
                            if nport:
                                print(f"        Remote Port : {nport}")
                            if plat:
                                print(f"        Platform    : {plat}")
                        else:
                            print(f"        CDP         : no neighbor on port (endpoint)")

    print()
    print(SEP)
    print()


def print_trace_summary(result: Dict) -> None:
    """Print the complete L2 + L3 trace summary from the combined output dict.

    Called once at the very end of the trace so all output is consolidated.

    *result* is the dict returned by ``run_l2_trace``::

        {
          "src_ip":     str,
          "dst_ip":     str,
          "gateway_ip": str,
          "layer2":     dict | None,          # from build_path_dict
          "layer3":     list[list[dict]] | None,  # from run_l3_path_trace
        }
    """
    SEP = "=" * 70
    print()
    print(SEP)
    print("  NETWORK TRACE SUMMARY")
    print(SEP)
    print(f"  Source      : {result.get('src_ip', '—')}")
    print(f"  Destination : {result.get('dst_ip', '—')}")
    print(f"  Gateway     : {result.get('gateway_ip', '—')}")
    print(SEP)

    layer2 = result.get("layer2")
    if layer2:
        print(f"\n  ── Layer 2 Path  (device → gateway) ──────────────────────────────")
        stop = layer2.get("stop_reason", "")
        if stop:
            print(f"  L2 stop reason : {stop}")
        print_path_summary(layer2)
    else:
        print("\n  [Layer 2 trace produced no path data]")

    layer3 = result.get("layer3")
    if layer3:
        print(f"\n  ── Layer 3 Paths  (gateway → destination) ─────────────────────────")
        print_l3_paths(layer3, result.get("dst_ip", ""))

    print()
    print(SEP)
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — L2 trace orchestration
# ─────────────────────────────────────────────────────────────────────────────


def run_l2_trace(
    target_ip: str,
    dst_ip: str,
    gateway_ip: str,
    creds: Dict[str, str],
    source_is_ap: bool,
    device_type: Optional[str] = None,
    max_hops: int = 30,
    nb_url: str = "",
    nb_token: str = "",
    verify_ssl: bool = True,
    topology_only: bool = False,
) -> Optional[Dict]:
    """Run the hop-by-hop Layer 2 trace for *target_ip* starting at *gateway_ip*.

    Phase A — ARP (gateway only, once):
        Resolves *target_ip* to a MAC address.

    Phase B — hop loop (up to *max_hops* switches):
        On each switch:
          1. MAC table lookup   → VLAN + interface
          2. Port-channel expansion (if applicable)
          3. CDP/LLDP on the resolved physical interface
          4a. Neighbor is AP, VMware, or absent → record final hop and stop.
          4b. Neighbor is a switch/router → record intermediate hop, resolve
              its management IP, and connect to continue the trace.

    After the loop the collected hops are reversed to produce a
    device→gateway path dict that is printed as a summary table.

    Pass *device_type* to skip the NetBox platform lookup for the gateway
    (saves one API call when the caller already fetched it for Phase 1).
    """
    # ── Phase A: ARP lookup on the gateway (done exactly once) ───────────────
    if device_type is None:
        device_type = "ios"  # direct SSH — no NetBox platform lookup

    log.info(
        "L2 trace start: target=%s  gateway=%s  device_type=%s",
        target_ip, gateway_ip, device_type,
    )

    try:
        client = _open_device_client(gateway_ip, device_type, creds)
    except GatewayConnectionError as exc:
        print(f"[ERROR] L2 trace — cannot connect to gateway {gateway_ip}: {exc}")
        return None

    mac: Optional[str]          = None
    gw_hostname: str            = gateway_ip
    gw_interface: Optional[str] = None
    dst_routes: List[Dict]      = []

    try:
        try:
            gw_hostname = client._cli_connection.find_prompt().rstrip("#>").strip() or gateway_ip
        except Exception:
            pass

        print(f"[INFO] ARP lookup for {target_ip} on {gw_hostname} ({gateway_ip})...")
        mac = arp_lookup(client, device_type, target_ip)

        gw_interface = get_gateway_interface(client, gateway_ip)
        if gw_interface:
            print(f"[INFO] Gateway IP {gateway_ip} is on interface {gw_interface}")

        print(f"[INFO] Route lookup for destination {dst_ip} on {gw_hostname}...")
        dst_routes = get_route_for_destination(client, dst_ip)
        if dst_routes:
            for r in dst_routes:
                nh    = r.get("next_hop", "?")
                iface = r.get("exit_interface", "")
                print(
                    f"[INFO] Route to {dst_ip}: "
                    f"{r.get('prefix') or dst_ip}  via {nh}"
                    + (f"  [{iface}]" if iface else "")
                )
        else:
            print(f"[WARN] No route found for {dst_ip} on {gw_hostname}")
    finally:
        try:
            client._cli_disconnect()
        except Exception:
            pass

    if not mac:
        print(f"[WARN] No ARP entry for {target_ip} on {gw_hostname}")
        return {
            "src_ip":     target_ip,
            "dst_ip":     dst_ip,
            "gateway_ip": gateway_ip,
            "layer2":     {"stop_reason": "ARP entry not found", "path": [], "mac": None},
            "layer3":     None,
        }

    print(f"[INFO] ARP resolved: {target_ip} -> {mac_to_cisco_fmt(mac)}")
    if source_is_ap:
        print(f"[INFO] Source {target_ip} is an AP — will stop at the access switchport")

    # ── Phase B: hop-by-hop MAC trace ─────────────────────────────────────────
    # path_hops accumulates records in *downstream* order (gateway → device).
    # Each record stores what is needed to later reconstruct the upstream path.
    #
    #   local_interface   – the MAC-table result on this switch (egress toward device)
    #   portchannel_members – physical members of local_interface if it is a Po
    #   remote_port       – CDP "Port ID (outgoing port)" = the port on the *next*
    #                        switch (toward device) that connects back to us.
    #                        This becomes the upstream egress of the *previous* hop.
    #
    path_hops: List[Dict] = []

    current_ip          = gateway_ip
    current_device_type = device_type
    visited: set        = {gateway_ip}

    final_stop_reason: str = f"Max hops ({max_hops}) reached"

    for hop_num in range(1, max_hops + 1):
        try:
            client = _open_device_client(current_ip, current_device_type, creds)
        except GatewayConnectionError as exc:
            final_stop_reason = f"Cannot connect to {current_ip}: {exc}"
            break

        hostname        : str            = current_ip
        mac_entry       : Optional[Dict] = None
        portchannel_mbrs: List[str]      = []
        neighbor_info   : Optional[Dict] = None

        try:
            try:
                hostname = client._cli_connection.find_prompt().rstrip("#>").strip() or current_ip
            except Exception:
                pass

            # Step 1 — MAC table lookup
            print(f"[INFO] MAC table lookup on {hostname} ({current_ip})...")
            mac_entry = mac_table_lookup(client, current_device_type, mac)
            if not mac_entry:
                final_stop_reason = (
                    f"MAC {mac_to_cisco_fmt(mac)} not found in table on {hostname}"
                )
                break

            vlan      = mac_entry["vlan"]
            interface = mac_entry["interface"]
            print(f"[INFO] MAC table: VLAN={vlan}  Interface={interface}")

            # Step 2 — port-channel expansion (skipped in topology_only mode)
            if is_portchannel(interface) and not topology_only:
                print(f"[INFO] {interface} is a port-channel — resolving members...")
                portchannel_mbrs = get_portchannel_members(client, current_device_type, interface)
                if portchannel_mbrs:
                    print(f"[INFO] Port-channel members: {', '.join(portchannel_mbrs)}")
                else:
                    print(f"[WARN] No members resolved for {interface}")

            # Step 2b — interface health / counter detail (skipped in topology_only mode)
            iface_detail = (
                {} if topology_only
                else get_interface_detail(client, current_device_type, interface)
            )

            # Step 3 — CDP/LLDP on the resolved physical interface
            check_iface = portchannel_mbrs[0] if portchannel_mbrs else interface
            print(f"[INFO] Checking CDP/LLDP on {check_iface}...")
            neighbor_info = get_neighbor_info(client, current_device_type, check_iface)

        finally:
            try:
                client._cli_disconnect()
            except Exception:
                pass

        # Always record this hop (downstream order) before deciding to stop/continue.
        path_hops.append({
            "hostname":           hostname,
            "switch_ip":          current_ip,
            "vlan":               mac_entry["vlan"],
            "local_interface":    mac_entry["interface"],
            "portchannel_members": portchannel_mbrs,
            "interface_detail":   iface_detail,
            # remote_port = CDP "outgoing port" on the next-hop switch (toward device).
            # Left as None when this is a stop hop (endpoint / AP / VMware port).
            "remote_port":        neighbor_info.get("remote_port") if neighbor_info else None,
        })

        # Step 4 — stop-condition evaluation
        should_stop, reason = should_stop_trace(neighbor_info)
        if should_stop:
            final_stop_reason = reason or "Closest switchport found"
            break

        # Neighbor is a network device — resolve its management IP and continue.
        neighbor_ip = resolve_neighbor_ip(neighbor_info)
        if not neighbor_ip:
            final_stop_reason = (
                f"Cannot resolve management IP for "
                f"{neighbor_info.get('neighbor_id', 'unknown')} — stopping"
            )
            # The remote_port is not useful when we cannot reach the neighbor.
            path_hops[-1]["remote_port"] = None
            break

        if neighbor_ip in visited:
            final_stop_reason = f"Loop detected — already visited {neighbor_ip}"
            path_hops[-1]["remote_port"] = None
            break

        # Log this switch as an intermediate hop and advance.
        _log_intermediate_hop(
            hop_num, hostname, current_ip,
            mac_entry["vlan"], mac_entry["interface"],
            portchannel_mbrs, neighbor_info, neighbor_ip,
        )

        visited.add(neighbor_ip)
        current_device_type = "ios"  # direct SSH — no NetBox platform lookup
        current_ip          = neighbor_ip

    # ── Build L2 path dict ────────────────────────────────────────────────────
    layer2_dict: Optional[Dict] = None
    if path_hops:
        layer2_dict = build_path_dict(
            target_ip       = target_ip,
            mac             = mac,
            gateway_ip      = gateway_ip,
            downstream_hops = path_hops,
            gateway_interface = gw_interface,
            dst_route       = dst_routes,
            dst_ip          = dst_ip,
            stop_reason     = final_stop_reason,
        )

    # ── Phase 3: L3 path trace (gateway → destination, all ECMP paths) ────────
    layer3_paths: List[List[Dict]] = []
    if dst_routes:
        layer3_paths = run_l3_path_trace(
            gateway_ip           = gateway_ip,
            gw_hostname          = gw_hostname,
            gw_ingress_interface = gw_interface,
            dst_ip               = dst_ip,
            initial_routes       = dst_routes,
            creds                = creds,
            src_ip               = target_ip,
            nb_url               = nb_url,
            nb_token             = nb_token,
            verify_ssl           = verify_ssl,
            max_hops             = max_hops,
            topology_only        = topology_only,
        )

    # ── Return combined output dict (caller prints at the end) ────────────────
    return {
        "src_ip":     target_ip,
        "dst_ip":     dst_ip,
        "gateway_ip": gateway_ip,
        "layer2":     layer2_dict,
        "layer3":     layer3_paths or None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4 — interface enrichment (run after topology is drawn)
# ─────────────────────────────────────────────────────────────────────────────


def iter_interface_enrichment(
    flat_paths:  List[Dict],
    creds:       Dict[str, str],
    max_workers: int = 10,
) -> "Iterator[Dict]":
    """Generator that enriches all devices in *flat_paths* **in parallel**.

    Each device gets its own SSH session in a ThreadPoolExecutor worker.
    Results are yielded as soon as any device finishes — fastest devices appear
    first in the SSE stream so the frontend fills in quickly.

    Yields
    ------
    ``{"type": "device_update",     "device": str, "data": {...}}``
    ``{"type": "interface_update",  "device": str, "interface": str, "data": {...}}``
    ``{"type": "portchannel_update","device": str, "interface": str, "members": [...]}``
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from typing import Iterator  # local import

    # ── Build device_ip → (device_name, [interfaces]) ─────────────────────────
    device_map: Dict[str, Tuple[str, List[str]]] = {}
    for path in flat_paths:
        for hop in (path.get("path") or []):
            d_ip  = hop.get("device_ip")
            d_name = hop.get("device", "unknown")
            iface = hop.get("interface", "")
            if not d_ip or not iface or iface in ("—", ""):
                continue
            if d_ip not in device_map:
                device_map[d_ip] = (d_name, [])
            _, ifaces = device_map[d_ip]
            if iface not in ifaces:
                ifaces.append(iface)
            eg = (hop.get("details") or {}).get("egress_interface")
            if eg and eg not in ifaces:
                ifaces.append(eg)

    if not device_map:
        return

    # ── Worker: SSH to one device, collect everything, return events list ─────
    def _enrich_device(d_ip: str, d_name: str, interfaces: List[str]) -> List[Dict]:
        events: List[Dict] = []
        print(f"[ENRICH] {d_name} ({d_ip}) — version + {len(interfaces)} interface(s)")
        try:
            client = _open_device_client(d_ip, "ios", creds)
        except GatewayConnectionError as exc:
            print(f"[ENRICH] Cannot connect to {d_name} ({d_ip}): {exc}")
            return events

        try:
            # show version
            ver = get_device_version(client, "ios")
            if ver.get("os_version") or ver.get("uptime") or ver.get("stack_members"):
                print(
                    f"[ENRICH] {d_name} — "
                    f"version={ver.get('os_version','?')}  "
                    f"uptime={ver.get('uptime','?')}"
                )
                events.append({"type": "device_update", "device": d_name, "data": ver})

            # show interface + port-channel members
            for iface in interfaces:
                detail = get_interface_detail(client, "ios", iface)
                print(
                    f"[ENRICH] {d_name} {iface} — "
                    f"state={detail.get('state','?')}  "
                    f"crc={detail.get('crc') or detail.get('rx_crc', 0)}"
                )
                events.append({
                    "type":      "interface_update",
                    "device":    d_name,
                    "interface": iface,
                    "data":      detail,
                })
                if is_portchannel(iface):
                    members = get_portchannel_members(client, "ios", iface)
                    if members:
                        print(f"[ENRICH] {d_name} {iface} — members: {', '.join(members)}")
                        events.append({
                            "type":      "portchannel_update",
                            "device":    d_name,
                            "interface": iface,
                            "members":   members,
                        })
        finally:
            try:
                client._cli_disconnect()
            except Exception:
                pass

        return events

    # ── Fan out: all devices in parallel; yield as each one finishes ──────────
    n_workers = min(max_workers, len(device_map))
    with ThreadPoolExecutor(max_workers=n_workers, thread_name_prefix="enrich-") as pool:
        future_map = {
            pool.submit(_enrich_device, d_ip, d_name, ifaces): (d_ip, d_name)
            for d_ip, (d_name, ifaces) in device_map.items()
        }
        for future in as_completed(future_map):
            d_ip, d_name = future_map[future]
            try:
                for event in future.result():
                    yield event
            except Exception as exc:
                print(f"[ENRICH] Unexpected error from {d_name} ({d_ip}): {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="network_tracer.py",
        description=(
            "Reconstruct the network path between two IPs using NetBox, "
            "ARP/NDP, MAC tables, CDP/LLDP, VRFs, FHRP, and routing tables. "
            "Supports IPv4, IPv6, Vault credentials, and parallel ECMP tracing."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Direct credentials
  python network_tracer.py 10.1.1.100 10.2.2.200 \\
      --netbox-url https://netbox.example.com --netbox-token abc123 \\
      --username admin --password secret

  # HashiCorp Vault credentials
  python network_tracer.py 10.1.1.100 10.2.2.200 \\
      --VAULT_ADDR https://vault.example.com \\
      --VAULT_ROLE_ID <role> --VAULT_SECRET_ID <secret>

  # Reverse trace + ECMP parallel + IPv6
  python network_tracer.py 2001:db8::1 2001:db8::2 --reverse --ecmp

  # Via environment variables
  export NETBOX_URL=https://netbox.example.com NETBOX_TOKEN=abc123
  export DEVICE_USER=admin DEVICE_PASS=secret
  python network_tracer.py 10.1.1.100 10.2.2.200
        """,
    )

    p.add_argument("src_ip", help="Source IP address (IPv4 or IPv6)")
    p.add_argument("dst_ip", help="Destination IP address (IPv4 or IPv6)")

    nb = p.add_argument_group("NetBox (ignored when Vault is configured)")
    nb.add_argument("--netbox-url",   default=None, help="NetBox base URL (env: NETBOX_URL)")
    nb.add_argument("--netbox-token", default=None, help="NetBox API token (env: NETBOX_TOKEN)")
    nb.add_argument("--no-ssl-verify", action="store_true", help="Disable TLS verification for NetBox")

    dev = p.add_argument_group("Device credentials (ignored when Vault is configured)")
    dev.add_argument("--username", default=None, help="SSH username (env: DEVICE_USER)")
    dev.add_argument("--password", default=None, help="SSH password (env: DEVICE_PASS)")
    dev.add_argument(
        "--secret",
        default=os.environ.get("DEVICE_SECRET", ""),
        help="Enable secret (env: DEVICE_SECRET)",
    )
    dev.add_argument("--timeout", type=int, default=30, help="SSH timeout in seconds (default: 30)")

    if _VAULT_AVAILABLE:
        vault_grp = p.add_argument_group(
            "Vault authentication (optional — overrides --username/--password/--netbox-*)"
        )
        add_vault_parser_args(vault_grp)

    tr = p.add_argument_group("Trace options")
    tr.add_argument("--reverse",  action="store_true", help="Also run reverse trace (dst -> src)")
    tr.add_argument("--ecmp",     action="store_true", help="Trace all ECMP paths in parallel")
    tr.add_argument("--max-hops", type=int, default=30, help="Max hops before stopping (default: 30)")
    tr.add_argument("--out-dir",  default=".",         help="Output directory for JSON/CSV (default: current dir)")
    tr.add_argument("--verbose",  action="store_true", help="Enable DEBUG logging")

    out = p.add_argument_group("Output format")
    out.add_argument(
        "--json",
        nargs="?",
        const="trace_result.json",
        default=None,
        metavar="FILE",
        help=(
            "Write the complete L2+L3 trace as a pretty-printed JSON object to FILE. "
            "When FILE is omitted the output goes to trace_result.json. "
            "All console progress messages are suppressed."
        ),
    )

    return p


# ─────────────────────────────────────────────────────────────────────────────
# JSON flat-path assembly
# ─────────────────────────────────────────────────────────────────────────────


def _flat_l2_hops(layer2: Dict) -> List[Dict]:
    """Convert the L2 upstream path to flat hop dicts, including the gateway's
    physical ingress port as the final L2 entry.

    For every switch in the L2 path the ``ingress_interface`` is the port facing
    the source device and ``egress_interface`` is the uplink toward the gateway.
    The gateway itself is special — its ``ingress_interface`` is the *physical*
    downlink port (e.g. ``Twe1/1/0/43``) while its ``egress_interface`` is the
    SVI (e.g. ``Vlan128``).  The physical downlink belongs to the L2 segment;
    the SVI belongs to the L3 segment and is emitted by ``_flat_l3_hops``.

    This information comes directly from the downstream MAC/CDP trace — no
    additional commands are needed.
    """
    hops: List[Dict] = []
    for hop in (layer2.get("path") or []):
        device  = hop.get("hostname") or hop.get("switch_ip", "unknown")
        details: Dict = {}
        if hop.get("vlan"):
            try:
                details["vlan"] = int(hop["vlan"])
            except (ValueError, TypeError):
                details["vlan"] = hop["vlan"]

        # Merge interface health/counter fields into details (no key conflicts).
        iface_det = hop.get("interface_detail") or {}
        if iface_det:
            details.update(iface_det)

        if hop.get("is_gateway"):
            physical_ingress = hop.get("ingress_interface")
            if physical_ingress:
                hops.append({
                    "layer":     "L2",
                    "device":    device,
                    "device_ip": hop.get("switch_ip"),
                    "interface": physical_ingress,
                    "details":   details,
                })
            continue

        egress = hop.get("egress_interface")
        if egress:
            details["egress_interface"] = egress
        mbrs = hop.get("ingress_portchannel_members")
        if mbrs:
            details["portchannel_members"] = mbrs
        hops.append({
            "layer":     "L2",
            "device":    device,
            "device_ip": hop.get("switch_ip"),
            "interface": hop.get("ingress_interface") or "—",
            "details":   details,
        })
    return hops


def _flat_l3_hops(l3_path: List[Dict]) -> List[Dict]:
    """Convert one L3 path (list of hop dicts) to flat hop dicts.

    The first hop in *l3_path* is always the gateway (it has ``selected_route``).
    Subsequent hops are intermediate L3 devices, ending with the hop that has
    the destination subnet directly connected.  That final hop also carries the
    ``l2_trace`` result which expands into additional L2 entries.
    """
    hops: List[Dict] = []

    for hop in l3_path:
        note          = hop.get("note", "")
        hostname      = hop.get("hostname") or hop.get("ip", "unknown")
        ingress       = _clean_iface((hop.get("ingress_interfaces") or [None])[0])
        iface_details = hop.get("interface_details") or {}
        ingress_det   = iface_details.get(ingress, {}) if ingress else {}

        if note:
            hops.append({
                "layer":     "L3",
                "device":    hostname,
                "device_ip": hop.get("connect_ip") or hop.get("ip"),
                "interface": ingress or "—",
                "details":   {"note": note},
            })
            # L2 fallback trace stored when the next-hop was unreachable —
            # emit as L2 hops so the JSON path still shows the egress port.
            l2t = hop.get("l2_trace")
            l2_dev    = hop.get("l2_trace_device") or hostname
            # The L2-fallback device is the PREVIOUS successfully-reached hop,
            # so its SSH IP is that hop's connect_ip (passed from prev_hop).
            l2_dev_ip = hop.get("l2_trace_device_ip") or hop.get("connect_ip") or hop.get("ip")
            if l2t and not l2t.get("error"):
                vlan_raw = l2t.get("vlan")
                l2_det: Dict = {}
                if l2t.get("mac"):
                    l2_det["mac"] = l2t["mac"]
                if vlan_raw is not None:
                    try:
                        l2_det["vlan"] = int(vlan_raw)
                    except (ValueError, TypeError):
                        l2_det["vlan"] = vlan_raw
                mbrs = l2t.get("portchannel_members")
                if mbrs:
                    l2_det["portchannel_members"] = mbrs
                _fb_iface_det = l2t.get("interface_detail") or {}
                if _fb_iface_det:
                    l2_det.update(_fb_iface_det)
                hops.append({
                    "layer":     "L2",
                    "device":    l2_dev,
                    "device_ip": l2_dev_ip,
                    "interface": l2t.get("port") or "—",
                    "details":   l2_det,
                })
                cdp = l2t.get("cdp_neighbor")
                if cdp and cdp.get("hostname"):
                    cdp_det = {k: v for k, v in {
                        "protocol": cdp.get("protocol", "CDP"),
                        "ip":       cdp.get("ip"),
                        "platform": cdp.get("platform"),
                    }.items() if v is not None}
                    hops.append({
                        "layer":     "L2",
                        "device":    cdp["hostname"],
                        "device_ip": cdp.get("ip"),   # CDP management IP
                        "interface": cdp.get("port") or "—",
                        "details":   cdp_det,
                    })
            continue

        sel = hop.get("selected_route")  # set only on the gateway hop

        if sel:
            # Gateway hop — show which ECMP route this path follows.
            iface   = ingress or "—"
            details = {k: v for k, v in {
                "gateway_ip":     hop.get("ip"),
                "next_hop_ip":    sel.get("next_hop"),
                "egress_iface":   sel.get("exit_interface"),
                "prefix":         sel.get("prefix"),
                "route_source":   sel.get("route_source"),
                "route_tag":      sel.get("route_tag"),
                "route_age":      sel.get("route_age"),
                "bgp_as_path":    sel.get("bgp_as_path"),
                "bgp_community":  sel.get("bgp_community"),
                "bgp_local_pref": sel.get("bgp_local_pref"),
                "bgp_origin":     sel.get("bgp_origin"),
                "bgp_med":        sel.get("bgp_med"),
                "bgp_weight":     sel.get("bgp_weight"),
            }.items() if v is not None}
        else:
            # Intermediate or final L3 hop.
            iface    = ingress or "—"
            egresses = hop.get("egress_routes") or []
            dc       = next(
                (r for r in egresses if r.get("next_hop") == "directly connected"), None
            )
            if dc:
                details = {k: v for k, v in {
                    "prefix":              dc.get("prefix"),
                    "connected_interface": dc.get("exit_interface"),
                    "route_source":        dc.get("route_source"),
                }.items() if v is not None}
            elif egresses:
                r = egresses[0]
                details = {k: v for k, v in {
                    "next_hop_ip":    r.get("next_hop"),
                    "prefix":         r.get("prefix"),
                    "egress_iface":   r.get("exit_interface"),
                    "route_source":   r.get("route_source"),
                    "route_tag":      r.get("route_tag"),
                    "route_age":      r.get("route_age"),
                    "bgp_as_path":    r.get("bgp_as_path"),
                    "bgp_community":  r.get("bgp_community"),
                    "bgp_local_pref": r.get("bgp_local_pref"),
                    "bgp_origin":     r.get("bgp_origin"),
                    "bgp_med":        r.get("bgp_med"),
                    "bgp_weight":     r.get("bgp_weight"),
                }.items() if v is not None}
            else:
                details = {}

        # Merge ingress interface health/counter fields into the L3 hop details.
        if ingress_det:
            details.update(ingress_det)

        hops.append({
            "layer":     "L3",
            "device":    hostname,
            "device_ip": hop.get("connect_ip") or hop.get("ip"),
            "interface": iface,
            "details":   details,
        })

        # ── Intermediate L2 switches between this L3 hop and the next ─────────
        # Discovered by _cdp_l2_walk_between_l3_hops for non-BGP routes.
        # Each switch is emitted as TWO L2 hops so the graph shows both
        # the incoming port (facing us) and the egress port (facing next-hop):
        #   hop A: interface = incoming port + its counters
        #   hop B: interface = egress  port + its counters  (omitted if unknown)
        for _sw in hop.get("l2_intermediate") or []:
            _sw_dev  = _sw.get("device") or _sw.get("switch_ip", "unknown")
            _in_det: Dict = dict(_sw.get("interface_detail") or {})
            _note = _sw.get("note", "")
            if _note:
                _in_det["note"] = _note

            # Hop A — incoming interface (edge: upstream L3 router → this switch)
            hops.append({
                "layer":     "L2",
                "device":    _sw_dev,
                "device_ip": _sw.get("switch_ip"),
                "interface": _sw.get("interface") or "—",
                "details":   _in_det,
            })

            # Hop B — egress interface (edge: this switch → next-hop device)
            _eg_iface = _sw.get("egress_interface")
            if _eg_iface:
                _eg_det: Dict = dict(_sw.get("egress_detail") or {})
                _eg_det["egress_interface"] = _eg_iface
                hops.append({
                    "layer":     "L2",
                    "device":    _sw_dev,
                    "device_ip": _sw.get("switch_ip"),
                    "interface": _eg_iface,
                    "details":   _eg_det,
                })

        # L2 trace at the final hop (destination subnet is directly connected).
        l2t = hop.get("l2_trace")
        if l2t and not l2t.get("error"):
            vlan_raw = l2t.get("vlan")
            l2_det: Dict = {}
            if l2t.get("mac"):
                l2_det["mac"] = l2t["mac"]
            if vlan_raw is not None:
                try:
                    l2_det["vlan"] = int(vlan_raw)
                except (ValueError, TypeError):
                    l2_det["vlan"] = vlan_raw
            mbrs = l2t.get("portchannel_members")
            if mbrs:
                l2_det["portchannel_members"] = mbrs
            # Merge port interface health/counter detail from the L2 trace.
            l2_iface_det = l2t.get("interface_detail") or {}
            if l2_iface_det:
                l2_det.update(l2_iface_det)

            hops.append({
                "layer":     "L2",
                "device":    hostname,
                "device_ip": hop.get("connect_ip") or hop.get("ip"),
                "interface": l2t.get("port") or "—",
                "details":   l2_det,
            })

            cdp = l2t.get("cdp_neighbor")
            if cdp and cdp.get("hostname"):
                cdp_det = {k: v for k, v in {
                    "protocol": cdp.get("protocol", "CDP"),
                    "ip":       cdp.get("ip"),
                    "platform": cdp.get("platform"),
                }.items() if v is not None}
                hops.append({
                    "layer":     "L2",
                    "device":    cdp["hostname"],
                    "device_ip": cdp.get("ip"),   # CDP management IP
                    "interface": cdp.get("port") or "—",
                    "details":   cdp_det,
                })

    return hops


def build_flat_paths(result: Dict) -> List[Dict]:
    """Transform the combined trace result into a JSON array of flat path objects.

    Each entry in the returned list is ONE complete path from source to destination.
    When multiple ECMP L3 routes exist the L2 segment is duplicated so every
    L3 route appears as an independent, self-contained path object.

    Path structure per object:
      src_ip, dst_ip, gateway_ip, path: [
        {layer: "L2"|"L3", device: str, interface: str, details: {…}}, …
      ]
    """
    src_ip     = result.get("src_ip", "")
    dst_ip     = result.get("dst_ip", "")
    gateway_ip = result.get("gateway_ip", "")
    layer2     = result.get("layer2") or {}
    layer3     = result.get("layer3") or []

    # Shared L2 prefix: switches between the source device and the gateway.
    l2_prefix = _flat_l2_hops(layer2)

    if not layer3:
        # L2-only result or trace that never reached L3 routing.
        return [{
            "src_ip":     src_ip,
            "dst_ip":     dst_ip,
            "gateway_ip": gateway_ip,
            "path":       l2_prefix,
        }]

    # One flat path object per ECMP route.
    paths: List[Dict] = []
    for l3_path in layer3:
        if not l3_path:
            continue
        paths.append({
            "src_ip":     src_ip,
            "dst_ip":     dst_ip,
            "gateway_ip": gateway_ip,
            "path":       l2_prefix + _flat_l3_hops(l3_path),
        })

    return paths or [{
        "src_ip":     src_ip,
        "dst_ip":     dst_ip,
        "gateway_ip": gateway_ip,
        "path":       l2_prefix,
    }]


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = build_parser()
    args   = parser.parse_args()
    _configure_logging(verbose=args.verbose)

    # args.json is None (flag absent), or a filename string (flag present).
    json_file    = args.json               # e.g. "trace_result.json" or custom name
    json_mode    = json_file is not None
    _orig_stdout = sys.stdout
    _trace_result: Optional[Dict] = None   # set inside try; read by finally

    # In JSON mode redirect stdout so no progress messages appear.
    # The finally block always runs (even after early return 1) and
    # writes the JSON file — or an error object when the trace fails.
    if json_mode:
        sys.stdout = io.StringIO()

    try:
        # ── Credential resolution ─────────────────────────────────────────────
        if _VAULT_AVAILABLE and is_vault_configured(args):
            try:
                addr, role_id, secret_id = resolve_vault_auth(args)
                vc = VaultClient(
                    addr, role_id, secret_id,
                    mount=getattr(args, "vault_mount", "secret"),
                    path=getattr(args, "vault_path",  "network/device"),
                )
                secrets = vc.get_secrets()
            except VaultError as exc:
                log.error("Vault error: %s", exc)
                return 1
            username     = secrets["user"]
            password     = secrets["password"]
            netbox_url   = secrets["netbox_url"]
            netbox_token = secrets["netbox_token"]
            log.info("Credentials loaded from Vault")
        else:
            username     = args.username     or os.environ.get("DEVICE_USER",  "")
            password     = args.password     or os.environ.get("DEVICE_PASS",  "")
            netbox_url   = args.netbox_url   or os.environ.get("NETBOX_URL",   "")
            netbox_token = args.netbox_token or os.environ.get("NETBOX_TOKEN", "")

        # ── Validate required credentials ─────────────────────────────────────
        errors: List[str] = []
        if not netbox_url:
            errors.append("NetBox URL required (--netbox-url, NETBOX_URL, or Vault)")
        if not netbox_token:
            errors.append("NetBox token required (--netbox-token, NETBOX_TOKEN, or Vault)")
        if not username:
            errors.append("SSH username required (--username, DEVICE_USER, or Vault)")
        if not password:
            errors.append("SSH password required (--password, DEVICE_PASS, or Vault)")
        if errors:
            for e in errors:
                print(f"ERROR: {e}", file=sys.stderr)
            parser.print_usage(sys.stderr)
            return 1

        verify_ssl = not args.no_ssl_verify
        src_ip     = args.src_ip

        creds: Dict[str, str] = {
            "username": username,
            "password": password,
            "secret":   args.secret,
            "timeout":  str(args.timeout),
        }

        # ── Phase 1: locate the gateway and verify SSH connectivity ───────────
        print(f"[INFO] Source IP: {src_ip}")

        prefixes = get_prefixes_from_netbox(netbox_url, netbox_token, verify_ssl, contains=src_ip)
        if not prefixes:
            print(f"[ERROR] No matching subnet found for {src_ip} in NetBox")
            log.error("No NetBox prefix contains %s", src_ip)
            return 1

        matched = find_longest_prefix_match(src_ip, prefixes)
        if not matched:
            print(f"[ERROR] No matching subnet found for {src_ip} in NetBox")
            log.error("Longest-prefix match failed for %s among %d candidates", src_ip, len(prefixes))
            return 1

        print(f"[INFO] Matched subnet: {matched}")

        gateway = calculate_first_usable_ip(matched)
        if not gateway:
            print(f"[ERROR] Could not determine gateway for subnet {matched}")
            log.error("calculate_first_usable_ip(%r) returned None", matched)
            return 1

        print(f"[INFO] Gateway IP (first usable): {gateway}")
        print("[INFO] Attempting connection to gateway...")
        try:
            hostname = connect_to_device(gateway, creds)
            print(f"[SUCCESS] Connected to {hostname}")
        except GatewayConnectionError as exc:
            print(f"[ERROR] Failed to connect: {exc}")
            log.error("Gateway connection failed: %s", exc)
            return 1

        # ── Phase 2: L2 + L3 trace → collect all data, print once at the end ─
        result = run_l2_trace(
            target_ip    = src_ip,
            dst_ip       = args.dst_ip,
            gateway_ip   = gateway,
            creds        = creds,
            source_is_ap = False,
            max_hops     = args.max_hops,
            nb_url       = netbox_url,
            nb_token     = netbox_token,
            verify_ssl   = verify_ssl,
        )

        _trace_result = result

        if result and not json_mode:
            print_trace_summary(result)

        return 0

    finally:
        if json_mode:
            sys.stdout = _orig_stdout

            if _trace_result is not None:
                flat   = build_flat_paths(_trace_result)
                output = json.dumps(flat, indent=2, default=str)
            else:
                output = json.dumps([{
                    "error":   "Trace did not complete — see log file for details",
                    "src_ip":  getattr(args, "src_ip",  ""),
                    "dst_ip":  getattr(args, "dst_ip",  ""),
                }], indent=2)

            try:
                with open(json_file, "w", encoding="utf-8") as fh:
                    fh.write(output)
                    fh.write("\n")
                print(f"[JSON] Trace written to {json_file}", file=sys.stderr)
            except OSError as exc:
                print(
                    f"[ERROR] Cannot write JSON to {json_file!r}: {exc}",
                    file=sys.stderr,
                )


if __name__ == "__main__":
    sys.exit(main())
