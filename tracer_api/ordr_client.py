"""
tracer_api/ordr_client.py
=========================
ORDR device intelligence API client.

Credentials are resolved in this priority order:
  1. Vault KV v2 — path is settings.ordr_vault_path when set, otherwise
     the same path as the network credentials (settings.vault_path).
     Expected keys in the secret: ORDR_URL, ORDR_TENANTGUID, ORDR_USER, ORDR_PASSWORD
  2. Env vars / .env — TRACER_ORDR_URL, TRACER_ORDR_TENANT_GUID, etc.

Credentials are cached in-process for 10 minutes.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning  # type: ignore

requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)  # type: ignore

log = logging.getLogger("tracer_api.ordr")


# ---------------------------------------------------------------------------
# Debug helpers
# ---------------------------------------------------------------------------

def _mask(value: str, show: int = 4) -> str:
    """Show the first *show* chars of a secret; mask the rest."""
    if not value:
        return "(empty)"
    if len(value) <= show:
        return value
    return value[:show] + "*" * (len(value) - show)


def _dbg(msg: str) -> None:
    """Print a debug line that shows up in the API server log."""
    print(f"[ORDR] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Credential cache (10-minute TTL)
# ---------------------------------------------------------------------------

_cred_cache:  Optional[Dict[str, str]] = None
_cred_expiry: float                    = 0.0
_cred_lock:   threading.Lock           = threading.Lock()
_CRED_TTL:    float                    = 600.0


def _load_credentials_from_vault() -> Optional[Dict[str, str]]:
    """Fetch ORDR credentials from Vault KV v2 and print every step."""
    _dbg("─── Vault credential load started ───────────────────────────────")

    try:
        import hvac
        import hvac.exceptions
        _dbg("  hvac imported OK")
    except ImportError:
        _dbg("  ERROR: hvac is not installed — run: pip install hvac")
        return None

    from .config import settings

    _dbg(f"  TRACER_VAULT_ADDR    = {settings.vault_addr or '(not set)'}")
    _dbg(f"  TRACER_VAULT_MOUNT   = {settings.vault_mount or '(not set)'}")
    _dbg(f"  TRACER_VAULT_PATH    = {settings.vault_path or '(not set)'}")
    _dbg(f"  TRACER_ORDR_VAULT_PATH = {settings.ordr_vault_path or '(not set — will use VAULT_PATH)'}")
    _dbg(f"  TRACER_VAULT_ROLE_ID = {_mask(settings.vault_role_id)}")
    _dbg(f"  TRACER_VAULT_SECRET_ID = {_mask(settings.vault_secret_id)}")

    if not settings.vault_addr:
        _dbg("  SKIP: TRACER_VAULT_ADDR is not set — cannot use Vault")
        return None

    vault_path = settings.ordr_vault_path.strip() or settings.vault_path
    _dbg(f"  Effective vault path = {settings.vault_mount}/{vault_path}")

    try:
        _dbg(f"  Connecting to Vault at {settings.vault_addr} …")
        client = hvac.Client(url=settings.vault_addr)

        _dbg("  Authenticating with AppRole …")
        login_resp = client.auth.approle.login(
            role_id    = settings.vault_role_id,
            secret_id  = settings.vault_secret_id,
        )
        _dbg(f"  Auth OK — client.is_authenticated() = {client.is_authenticated()}")
        _dbg(f"  Token lease duration = {login_resp.get('auth', {}).get('lease_duration', '?')}s")

        _dbg(f"  Reading secret: mount={settings.vault_mount!r} path={vault_path!r} …")
        resp = client.secrets.kv.v2.read_secret_version(
            mount_point              = settings.vault_mount,
            path                     = vault_path,
            raise_on_deleted_version = True,
        )
        raw: dict = resp.get("data", {}).get("data", {})

        all_keys = sorted(raw.keys())
        _dbg(f"  Secret contains {len(all_keys)} key(s): {', '.join(all_keys)}")

        # Check which ORDR keys are present
        for expected in ("ORDR_URL", "ORDR_TENANTGUID", "ORDR_USER", "ORDR_PASSWORD"):
            present = expected in raw or expected.lower() in raw
            _dbg(f"  Key {expected!r}: {'FOUND' if present else 'MISSING'}")

        creds = {
            "url":         str(raw.get("ORDR_URL")        or raw.get("ordr_url",         "") or ""),
            "tenant_guid": str(raw.get("ORDR_TENANTGUID") or raw.get("ordr_tenantguid",  "") or ""),
            "user":        str(raw.get("ORDR_USER")        or raw.get("ordr_user",         "") or ""),
            "password":    str(raw.get("ORDR_PASSWORD")    or raw.get("ordr_password",     "") or ""),
        }

        _dbg(f"  Resolved ORDR_URL         = {creds['url'] or '(empty)'}")
        _dbg(f"  Resolved ORDR_TENANTGUID  = {creds['tenant_guid'] or '(empty)'}")
        _dbg(f"  Resolved ORDR_USER        = {creds['user'] or '(empty)'}")
        _dbg(f"  Resolved ORDR_PASSWORD    = {_mask(creds['password'])}")

        if not creds["url"] or not creds["user"]:
            _dbg("  ERROR: ORDR_URL or ORDR_USER is empty after resolution — check the secret keys above")
            return None

        _dbg("  Vault load SUCCESS")
        return creds

    except hvac.exceptions.Forbidden as exc:
        _dbg(f"  ERROR: Vault permission denied — check the AppRole policy: {exc}")
        return None
    except hvac.exceptions.InvalidPath as exc:
        _dbg(f"  ERROR: Vault path not found ({settings.vault_mount}/{vault_path}): {exc}")
        return None
    except Exception as exc:
        _dbg(f"  ERROR: {type(exc).__name__}: {exc}")
        log.warning("ORDR Vault credential load failed: %s", exc)
        return None


def _resolve_credentials() -> Dict[str, str]:
    """Return ORDR credentials, using a TTL cache.  Prints what source is used."""
    global _cred_cache, _cred_expiry

    with _cred_lock:
        now = time.monotonic()
        if _cred_cache is not None and now < _cred_expiry:
            _dbg(f"Using cached credentials (expires in {_cred_expiry - now:.0f}s)")
            return _cred_cache

        _dbg("Cache miss — loading fresh credentials …")
        vault_creds = _load_credentials_from_vault()
        if vault_creds:
            _cred_cache  = vault_creds
            _cred_expiry = now + _CRED_TTL
            return _cred_cache

        _dbg("Vault load failed — falling back to env vars / settings")
        from .config import settings
        fallback = {
            "url":         settings.ordr_url,
            "tenant_guid": settings.ordr_tenant_guid,
            "user":        settings.ordr_user,
            "password":    settings.ordr_password,
        }
        _dbg(f"  Env TRACER_ORDR_URL         = {fallback['url'] or '(not set)'}")
        _dbg(f"  Env TRACER_ORDR_TENANT_GUID = {fallback['tenant_guid'] or '(not set)'}")
        _dbg(f"  Env TRACER_ORDR_USER        = {fallback['user'] or '(not set)'}")
        _dbg(f"  Env TRACER_ORDR_PASSWORD    = {_mask(fallback['password'])}")

        _cred_cache  = fallback
        _cred_expiry = now + _CRED_TTL
        return _cred_cache


def invalidate_credential_cache() -> None:
    """Force the next call to re-fetch credentials from Vault."""
    global _cred_cache, _cred_expiry
    with _cred_lock:
        _cred_cache  = None
        _cred_expiry = 0.0
    _dbg("Credential cache invalidated")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class OrdrNotConfiguredError(Exception):
    """Raised when ORDR URL or credentials are not available."""


class OrdrDeviceNotFoundError(Exception):
    """Raised when ORDR returns no data for the given IP."""


def query_by_ip(ip: str, timeout: int = 15) -> Dict[str, Any]:
    """Query ORDR for device intelligence by IP address."""
    _dbg(f"═══ query_by_ip({ip!r}) ══════════════════════════════════════════")

    creds = _resolve_credentials()

    _dbg(f"  URL         = {creds.get('url') or '(empty)'}")
    _dbg(f"  tenantGuid  = {creds.get('tenant_guid') or '(empty)'}")
    _dbg(f"  user        = {creds.get('user') or '(empty)'}")
    _dbg(f"  password    = {_mask(creds.get('password', ''))}")

    if not creds.get("url"):
        _dbg("  ABORT: ORDR URL is empty")
        raise OrdrNotConfiguredError(
            "ORDR URL is not configured.  Set TRACER_ORDR_URL in .env "
            "or add ORDR_URL to the Vault secret at TRACER_VAULT_PATH."
        )
    if not creds.get("user"):
        _dbg("  ABORT: ORDR user is empty")
        raise OrdrNotConfiguredError(
            "ORDR user is not configured.  Set TRACER_ORDR_USER in .env "
            "or add ORDR_USER to the Vault secret."
        )

    # Build the full endpoint URL: base URL from Vault + /Devices
    base_url     = creds["url"].rstrip("/")
    endpoint_url = f"{base_url}/Devices"

    params: list = [
        ("ip",         ip.strip()),
        ("tenantGuid", creds["tenant_guid"]),
    ]
    headers = {"Accept": "application/json"}

    full_url = requests.Request(
        "GET", endpoint_url, params=params
    ).prepare().url

    _dbg(f"  Base URL     = {base_url}")
    _dbg(f"  Endpoint URL = {endpoint_url}")
    _dbg(f"  Request URL  = {full_url}")
    _dbg(f"  Auth user    = {creds['user']}")
    _dbg(f"  verify SSL   = False")
    _dbg(f"  timeout      = {timeout}s")
    _dbg("  Sending request …")

    try:
        resp = requests.get(
            endpoint_url,
            params   = params,
            headers  = headers,
            auth     = (creds["user"], creds["password"]),
            verify   = False,
            timeout  = timeout,
        )
    except requests.exceptions.ConnectionError as exc:
        _dbg(f"  CONNECTION ERROR: {exc}")
        raise RuntimeError(f"Cannot connect to ORDR at {creds['url']}: {exc}") from exc
    except requests.exceptions.Timeout:
        _dbg(f"  TIMEOUT after {timeout}s")
        raise RuntimeError(f"ORDR request timed out after {timeout}s") from None
    except requests.exceptions.RequestException as exc:
        _dbg(f"  REQUEST ERROR: {type(exc).__name__}: {exc}")
        raise RuntimeError(f"ORDR request failed: {exc}") from exc

    _dbg(f"  HTTP {resp.status_code} {resp.reason}")
    _dbg(f"  Response headers: {dict(resp.headers)}")
    _dbg(f"  Response body ({len(resp.content)} bytes):")

    # Print up to 2000 chars of body for diagnostics
    body_preview = resp.text[:2000]
    for line in body_preview.splitlines():
        _dbg(f"    {line}")
    if len(resp.text) > 2000:
        _dbg(f"    … (truncated, {len(resp.text)} total chars)")

    if resp.status_code == 401:
        invalidate_credential_cache()
        raise RuntimeError("ORDR authentication failed (401) — credentials invalidated.")

    if resp.status_code == 404:
        raise OrdrDeviceNotFoundError(f"No ORDR record found for IP {ip}")

    if not resp.ok:
        raise RuntimeError(f"ORDR returned HTTP {resp.status_code}: {resp.text[:200]}")

    try:
        data = resp.json()
    except ValueError as exc:
        _dbg(f"  JSON parse error: {exc}")
        raise RuntimeError(f"ORDR response is not valid JSON: {exc}") from exc

    # Normalise three possible response shapes:
    #   1. {"MetaData": {...}, "Devices": [{...}]}   ← paginated list response
    #   2. [{...}]                                    ← bare list
    #   3. {...}                                      ← single device dict
    if isinstance(data, dict) and "Devices" in data:
        devices = data["Devices"]
        _dbg(f"  Response is paginated — Devices count: {len(devices)}")
        if not devices:
            raise OrdrDeviceNotFoundError(f"No ORDR record found for IP {ip}")
        data = devices[0]
    elif isinstance(data, list):
        _dbg(f"  Response is a list with {len(data)} item(s)")
        if not data:
            raise OrdrDeviceNotFoundError(f"No ORDR record found for IP {ip}")
        data = data[0]

    if not data:
        raise OrdrDeviceNotFoundError(f"No ORDR record found for IP {ip}")

    _dbg(f"  SUCCESS — device: {data.get('deviceName','?')} / {data.get('DeviceType','?')}")
    _dbg("═══════════════════════════════════════════════════════════════════")
    log.info("ORDR: found device %s (%s) for IP %s",
             data.get("deviceName", "?"), data.get("DeviceType", "?"), ip)
    return data
