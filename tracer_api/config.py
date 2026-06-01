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
    max_concurrent_traces: int = 10
    # ThreadPoolExecutor workers used to explore parallel ECMP L3 branches.
    max_l3_branch_workers: int = 6

    # ── Cache TTLs (seconds, 0 = disabled) ─────────────────────────────────────
    # How long to cache NetBox prefix-list results (rarely change mid-trace).
    netbox_prefix_cache_ttl: int = 300
    # How long to cache a completed (src_ip, dst_ip) trace result.
    trace_result_cache_ttl: int = 600

    # ── Task retention ─────────────────────────────────────────────────────────
    # How long to keep completed/failed task records in memory before eviction.
    task_ttl_seconds: int = 3600

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
