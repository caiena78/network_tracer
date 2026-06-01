"""
tracer_api/ordr_client.py
=========================
ORDR (Order of the Dragon) device intelligence API client.

Credentials are resolved at call-time in this priority order:
  1. Vault KV v2  (path controlled by TRACER_ORDR_VAULT_PATH, default: ordr/credentials)
     Expected secret keys: ORDR_URL, ORDR_TENANTGUID, ORDR_USER, ORDR_PASSWORD
  2. Environment variables / .env  (TRACER_ORDR_URL, TRACER_ORDR_TENANT_GUID, etc.)

Vault credentials are cached per-process using a TTL cache (10 minutes).
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
# Credential cache (10-minute TTL — short enough to pick up Vault rotations)
# ---------------------------------------------------------------------------

_cred_cache:    Optional[Dict[str, str]] = None
_cred_expiry:   float                    = 0.0
_cred_lock:     threading.Lock           = threading.Lock()
_CRED_TTL:      float                    = 600.0   # seconds


def _load_credentials_from_vault() -> Optional[Dict[str, str]]:
    """Fetch ORDR credentials from Vault KV v2.  Returns None on any failure."""
    try:
        import hvac
        import hvac.exceptions
    except ImportError:
        log.debug("hvac not installed — cannot load ORDR creds from Vault")
        return None

    from .config import settings

    if not settings.vault_addr:
        return None

    log.debug(
        "Loading ORDR credentials from Vault %s / %s / %s",
        settings.vault_addr, settings.vault_mount, settings.ordr_vault_path,
    )

    try:
        client = hvac.Client(url=settings.vault_addr)
        client.auth.approle.login(
            role_id    = settings.vault_role_id,
            secret_id  = settings.vault_secret_id,
        )

        resp = client.secrets.kv.v2.read_secret_version(
            mount_point              = settings.vault_mount,
            path                     = settings.ordr_vault_path,
            raise_on_deleted_version = True,
        )
        raw: dict = resp.get("data", {}).get("data", {})

        creds = {
            "url":         str(raw.get("ORDR_URL",         "") or ""),
            "tenant_guid": str(raw.get("ORDR_TENANTGUID",  "") or ""),
            "user":        str(raw.get("ORDR_USER",         "") or ""),
            "password":    str(raw.get("ORDR_PASSWORD",     "") or ""),
        }

        if not creds["url"] or not creds["user"]:
            log.warning(
                "ORDR Vault secret at '%s/%s' is missing ORDR_URL or ORDR_USER",
                settings.vault_mount, settings.ordr_vault_path,
            )
            return None

        log.info("ORDR credentials loaded from Vault")
        return creds

    except Exception as exc:
        log.warning("ORDR Vault credential load failed: %s", exc)
        return None


def _resolve_credentials() -> Dict[str, str]:
    """Return ORDR credentials, using a 10-minute in-process TTL cache."""
    global _cred_cache, _cred_expiry

    with _cred_lock:
        if _cred_cache is not None and time.monotonic() < _cred_expiry:
            return _cred_cache

        vault_creds = _load_credentials_from_vault()
        if vault_creds:
            _cred_cache  = vault_creds
            _cred_expiry = time.monotonic() + _CRED_TTL
            return _cred_cache

        # Fall back to env vars / settings
        from .config import settings
        fallback = {
            "url":         settings.ordr_url,
            "tenant_guid": settings.ordr_tenant_guid,
            "user":        settings.ordr_user,
            "password":    settings.ordr_password,
        }
        _cred_cache  = fallback
        _cred_expiry = time.monotonic() + _CRED_TTL
        return _cred_cache


def invalidate_credential_cache() -> None:
    """Force the next call to re-fetch credentials from Vault."""
    global _cred_cache, _cred_expiry
    with _cred_lock:
        _cred_cache  = None
        _cred_expiry = 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class OrdrNotConfiguredError(Exception):
    """Raised when ORDR URL or credentials are not available."""


class OrdrDeviceNotFoundError(Exception):
    """Raised when ORDR returns no data for the given IP."""


def query_by_ip(ip: str, timeout: int = 15) -> Dict[str, Any]:
    """
    Query ORDR for device intelligence by IP address.

    Parameters
    ----------
    ip : str
        The device IP address to look up.
    timeout : int
        HTTP request timeout in seconds.

    Returns
    -------
    dict
        The ORDR device record (may be empty if the device is not in ORDR).

    Raises
    ------
    OrdrNotConfiguredError
        When ORDR URL or credentials are not available.
    OrdrDeviceNotFoundError
        When ORDR returns no record for the IP.
    RuntimeError
        On any other HTTP or connection error.
    """
    creds = _resolve_credentials()

    if not creds.get("url"):
        raise OrdrNotConfiguredError(
            "ORDR URL is not configured.  Set TRACER_ORDR_URL in .env "
            "or add ORDR_URL to the Vault secret at TRACER_ORDR_VAULT_PATH."
        )
    if not creds.get("user"):
        raise OrdrNotConfiguredError(
            "ORDR user is not configured.  Set TRACER_ORDR_USER in .env "
            "or add ORDR_USER to the Vault secret."
        )

    params: list = [
        ("ip",         ip.strip()),
        ("tenantGuid", creds["tenant_guid"]),
    ]

    headers = {"Accept": "application/json"}

    log.debug("ORDR query: GET %s ip=%s tenantGuid=%s", creds["url"], ip, creds["tenant_guid"])

    try:
        resp = requests.get(
            creds["url"],
            params   = params,
            headers  = headers,
            auth     = (creds["user"], creds["password"]),
            verify   = False,
            timeout  = timeout,
        )
    except requests.exceptions.ConnectionError as exc:
        raise RuntimeError(f"Cannot connect to ORDR at {creds['url']}: {exc}") from exc
    except requests.exceptions.Timeout:
        raise RuntimeError(f"ORDR request timed out after {timeout}s") from None
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"ORDR request failed: {exc}") from exc

    if resp.status_code == 404:
        raise OrdrDeviceNotFoundError(f"No ORDR record found for IP {ip}")

    if resp.status_code == 401:
        # Credentials may have been rotated — invalidate cache and raise
        invalidate_credential_cache()
        raise RuntimeError("ORDR authentication failed (401).  Credentials have been invalidated.")

    if not resp.ok:
        raise RuntimeError(
            f"ORDR returned HTTP {resp.status_code}: {resp.text[:200]}"
        )

    try:
        data = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"ORDR response is not valid JSON: {exc}") from exc

    # ORDR can return an empty dict or a list with one item
    if isinstance(data, list):
        if not data:
            raise OrdrDeviceNotFoundError(f"No ORDR record found for IP {ip}")
        data = data[0]

    if not data:
        raise OrdrDeviceNotFoundError(f"No ORDR record found for IP {ip}")

    log.info("ORDR: found device %s (%s) for IP %s",
             data.get("deviceName", "?"), data.get("DeviceType", "?"), ip)
    return data
