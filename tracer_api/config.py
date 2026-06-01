"""
tracer_api/config.py
====================
All configuration is read from environment variables (or a .env file).
Override any setting by exporting TRACER_<UPPER_NAME>=value.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TRACER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── NetBox ─────────────────────────────────────────────────────────────────
    netbox_url: str = ""
    netbox_token: str = ""
    netbox_verify_ssl: bool = True

    # ── Device SSH credentials ─────────────────────────────────────────────────
    device_username: str = ""
    device_password: str = ""
    device_enable_secret: str = ""
    device_timeout: int = 30

    # ── HashiCorp Vault (optional — overrides device/NetBox creds when set) ────
    vault_addr: str = ""
    vault_role_id: str = ""
    vault_secret_id: str = ""
    vault_mount: str = "secret"
    vault_path: str = "network/device"

    # ── API behaviour ──────────────────────────────────────────────────────────
    api_title: str = "Network Tracer API"
    api_version: str = "1.0.0"
    # Comma-separated allowed origins, e.g. "http://localhost:3000,https://noc.example.com"
    cors_origins: str = "*"
    # When non-empty, every request must carry  X-API-Key: <value>  header.
    api_key: str = ""
    # Uvicorn host / port (used by the startup script, not FastAPI itself)
    host: str = "0.0.0.0"
    port: int = 8000
    reload: bool = False

    # ── Tracer limits ──────────────────────────────────────────────────────────
    max_hops: int = 30
    # Hard wall-clock timeout for a single trace job (seconds).
    trace_timeout_seconds: int = 900
    # Maximum number of trace jobs that may run concurrently.
    # Each trace occupies one OS thread (SSH is blocking I/O).  Set this to the
    # number of simultaneous traces you expect; the thread pool grows to match.
    max_concurrent_traces: int = 50
    # ThreadPoolExecutor workers used to explore parallel ECMP L3 branches.
    max_l3_branch_workers: int = 12
    # ThreadPoolExecutor workers used for parallel interface enrichment.
    # Each worker holds one SSH session; set to the average number of devices
    # per trace path so all devices are enriched simultaneously.
    max_enrichment_workers: int = 10

    # ── Cache TTLs (seconds, 0 = disabled) ─────────────────────────────────────
    # How long to cache NetBox prefix-list results (rarely change mid-trace).
    netbox_prefix_cache_ttl: int = 300
    # How long to cache a completed (src_ip, dst_ip) trace result.
    trace_result_cache_ttl: int = 600

    # ── Task retention ─────────────────────────────────────────────────────────
    # How long to keep completed/failed task records in memory before eviction.
    task_ttl_seconds: int = 3600

    # ── ORDR device intelligence ───────────────────────────────────────────────
    # Credentials are loaded from Vault when vault_addr is configured.
    # The Vault secret at ordr_vault_path must contain:
    #   ORDR_URL, ORDR_TENANTGUID, ORDR_USER, ORDR_PASSWORD
    # Direct env-var overrides (used when Vault is not configured):
    # KV v2 path for ORDR secrets.
    # Leave blank (default) to read from the same Vault secret as the network
    # device credentials (TRACER_VAULT_PATH).  Set to a different path only when
    # ORDR credentials live in a separate Vault secret.
    ordr_vault_path: str = ""
    ordr_url:         str = ""
    ordr_tenant_guid: str = ""
    ordr_user:        str = ""
    ordr_password:    str = ""

    # ── History persistence ─────────────────────────────────────────────────────
    # Path to the SQLite database file for trace history.
    # Relative paths are resolved from the process working directory.
    history_db_path: str = "./trace_history.db"

    @property
    def cors_origins_list(self) -> List[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
