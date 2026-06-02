"""
tracer_api/models.py
====================
Pydantic v2 request / response models used by the FastAPI routes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class TraceStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    ENRICHING = "enriching"   # topology ready, interface enrichment streaming
    COMPLETED = "completed"
    FAILED    = "failed"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class TraceRequest(BaseModel):
    """Body for POST /api/v1/traces."""

    src_ip: str = Field(..., description="Source IP address (IPv4)")
    dst_ip: str = Field(..., description="Destination IP address (IPv4)")
    max_hops: int = Field(30, ge=1, le=64, description="Maximum L3 hops")

    # Optional per-request credential overrides.
    # When omitted the server-side environment variables are used.
    netbox_url:    Optional[str] = Field(None, description="Override NetBox URL")
    netbox_token:  Optional[str] = Field(None, description="Override NetBox API token")
    username:      Optional[str] = Field(None, description="Override device SSH username")
    password:      Optional[str] = Field(None, description="Override device SSH password")

    @field_validator("src_ip", "dst_ip")
    @classmethod
    def validate_ipv4(cls, v: str) -> str:
        import ipaddress
        try:
            ipaddress.IPv4Address(v.split("/")[0])
        except ValueError:
            raise ValueError(f"{v!r} is not a valid IPv4 address")
        return v


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class TraceSummary(BaseModel):
    """Lightweight entry returned by GET /api/v1/traces (list)."""
    trace_id:   str
    status:     TraceStatus
    src_ip:     str
    dst_ip:     str
    created_at: str
    updated_at: str


class TraceResponse(BaseModel):
    """Full trace record returned by GET /api/v1/traces/{trace_id}."""
    trace_id:          str
    status:            TraceStatus
    src_ip:            str
    dst_ip:            str
    created_at:        str
    updated_at:        str
    progress:          List[str] = []
    result:            Optional[List[Dict[str, Any]]] = None  # flat paths
    error:             Optional[str] = None
    duration_seconds:  Optional[float] = None


# ---------------------------------------------------------------------------
# Graph models  (Cytoscape.js-compatible)
# ---------------------------------------------------------------------------

class GraphNodeData(BaseModel):
    id:          str
    label:       str
    node_type:   str   # "switch" | "router" | "gateway" | "src" | "dst" | "unknown"
    layer:       str   # "L2" | "L3" | "mixed"
    ip:          Optional[str] = None
    details:     Dict[str, Any] = {}


class GraphEdgeData(BaseModel):
    id:            str
    source:        str  # node id
    target:        str  # node id
    label:         str = ""
    layer:         str  # "L2" | "L3"
    src_interface: Optional[str] = None
    dst_interface: Optional[str] = None
    vlan:          Optional[int] = None
    details:       Dict[str, Any] = {}


class GraphElement(BaseModel):
    """Single Cytoscape.js element (node or edge)."""
    data: Dict[str, Any]


class PathInfo(BaseModel):
    path_id:      int
    edge_ids:     List[str]
    src_ip:       str
    dst_ip:       str
    gateway_ip:   str
    ecmp_variant: int = 0   # which ECMP branch (0-based)


class GraphResponse(BaseModel):
    """Cytoscape.js-compatible graph returned by GET /api/v1/traces/{id}/graph."""
    elements:  List[GraphElement]
    paths:     List[PathInfo]
    metadata:  Dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Health / info
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    status:         str = "ok"
    api_version:    str
    active_traces:  int
    queued_traces:  int
    cached_results: int


# ---------------------------------------------------------------------------
# Trace History
# ---------------------------------------------------------------------------

class HistorySummary(BaseModel):
    """Lightweight entry returned by GET /api/v1/history (list)."""
    id:         str
    src_ip:     str
    dst_ip:     str
    created_at: str
    status:     str
    duration_s: Optional[float] = None


class HistoryDetail(HistorySummary):
    """Full history entry including the rendered graph payload."""
    graph: Optional[Dict[str, Any]] = None


class HistoryListResponse(BaseModel):
    entries: List[HistorySummary]
    total:   int
    limit:   int
    offset:  int


# ---------------------------------------------------------------------------
# On-demand interface detail
# ---------------------------------------------------------------------------

class OrdrDeviceData(BaseModel):
    """Full device intelligence record from ORDR.  All fields are optional."""
    # ── Identity ────────────────────────────────────────────────────────────
    ip:                   Optional[str]   = Field(None, alias="IpAddress")
    mac:                  Optional[str]   = Field(None, alias="MacAddress")
    device_name:          Optional[str]   = Field(None, alias="deviceName")
    fqdn:                 Optional[str]   = Field(None, alias="fqdn")
    dhcp_hostname:        Optional[str]   = Field(None, alias="dhcpHostname")
    serial:               Optional[str]   = Field(None, alias="SerialNo")
    # ── Classification ──────────────────────────────────────────────────────
    device_type:          Optional[str]   = Field(None, alias="DeviceType")
    device_descr:         Optional[str]   = Field(None, alias="DeviceDescr")
    group:                Optional[str]   = Field(None, alias="Group")
    profile:              Optional[str]   = Field(None, alias="Profile")
    endpoint_type:        Optional[str]   = Field(None, alias="endpointType")
    classification_state: Optional[str]   = Field(None, alias="classificationState")
    criticality:          Optional[str]   = Field(None, alias="criticality")
    fda_class:            Optional[int]   = Field(None, alias="fdaClass")
    secondary_device:     Optional[bool]  = Field(None, alias="secondaryDevice")
    guest_device:         Optional[bool]  = Field(None, alias="guestDevice")
    ou:                   Optional[str]   = Field(None, alias="ou")
    # ── Hardware / Software ──────────────────────────────────────────────────
    manufacturer:         Optional[str]   = Field(None, alias="LongMfgName")
    mfg_name:             Optional[str]   = Field(None, alias="MfgName")
    model:                Optional[str]   = Field(None, alias="ModelNameNo")
    os_type:              Optional[str]   = Field(None, alias="OsType")
    os_version:           Optional[str]   = Field(None, alias="OsVersion")
    sw_version:           Optional[str]   = Field(None, alias="SwVersion")
    # ── Network ─────────────────────────────────────────────────────────────
    subnet:               Optional[str]   = Field(None, alias="Subnet")
    vlan:                 Optional[int]   = Field(None, alias="Vlan")
    vlan_name:            Optional[str]   = Field(None, alias="vlanName")
    access_type:          Optional[str]   = Field(None, alias="accessType")
    essid:                Optional[str]   = Field(None, alias="essid")
    dhcp_enabled:         Optional[bool]  = Field(None, alias="dhcpEnabled")
    # ── Risk & Security ──────────────────────────────────────────────────────
    risk_state:           Optional[str]   = Field(None, alias="RiskState")
    risk_score:           Optional[float] = Field(None, alias="riskScore")
    known_vuln_risk:      Optional[str]   = Field(None, alias="knownVulnRiskState")
    alarm_count:          Optional[int]   = Field(None, alias="alarmCount")
    has_phi:              Optional[bool]  = Field(None, alias="hasPhi")
    has_external_flows:   Optional[str]   = Field(None, alias="hasExternalFlows")
    is_blacklisted:       Optional[bool]  = Field(None, alias="isBlacklisted")
    proxied:              Optional[bool]  = Field(None, alias="proxied")
    # ── Status & Visibility ──────────────────────────────────────────────────
    conn_status:          Optional[str]   = Field(None, alias="connStatus")
    first_seen:           Optional[str]   = Field(None, alias="firstSeen")
    last_seen:            Optional[str]   = Field(None, alias="lastSeen")
    # ── Network equipment (where the device is connected) ────────────────────
    nw_equip_hostname:    Optional[str]   = Field(None, alias="nwEquipHostname")
    nw_equip_interface:   Optional[str]   = Field(None, alias="nwEquipInterface")
    # Accept both the real field name and the typo from the schema
    nw_equip_scrape_ip:   Optional[str]   = Field(None, alias="nwEquipScrapeIp")
    nw_equip_scrape_ip_typo: Optional[str] = Field(None, alias="nwEuipScrapeIp")
    # ── Location ────────────────────────────────────────────────────────────
    device_location:      Optional[str]   = Field(None, alias="deviceLocation")
    sensor_location:      Optional[str]   = Field(None, alias="sensorLocation")
    # ── Sensor ──────────────────────────────────────────────────────────────
    sensor_name:          Optional[str]   = Field(None, alias="sensorName")
    sensor_ip:            Optional[str]   = Field(None, alias="sensorIp")

    model_config = {"populate_by_name": True, "extra": "allow"}


class InterfaceDetailRequest(BaseModel):
    """Body for POST /api/v1/interfaces/detail."""
    device_ip: str  = Field(..., description="SSH management IP of the device")
    interface: str  = Field(..., description="Interface name, e.g. GigabitEthernet0/1")


class InterfaceDetailResponse(BaseModel):
    device_ip:  str
    interface:  str
    raw_output: str
    parsed:     Dict[str, Any]
