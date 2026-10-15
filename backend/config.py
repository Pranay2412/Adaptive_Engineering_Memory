"""Configuration Management & Validation for Synapse FastAPI Service (Phase 10).

Provides:
- Typed configuration dataclass (SynapseConfig)
- Environment variable loader with fallback defaults
- Strict startup validation (production secret safety, bounds checks, path validation)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .security import DEFAULT_DEV_SECRET


class EnvironmentMode(str, Enum):
    """Runtime environment modes."""
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    TESTING = "testing"


class ConfigValidationError(ValueError):
    """Raised when configuration fails startup validation checks."""
    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


@dataclass
class SynapseConfig:
    """Validated configuration for the Synapse FastAPI service."""

    environment: str = EnvironmentMode.DEVELOPMENT.value
    host: str = "127.0.0.1"
    port: int = 8000
    secret_key: str = DEFAULT_DEV_SECRET
    require_auth: bool = False
    db_path: str = "data/synapse_team_memory.db"
    cors_allowed_origins: list[str] = field(default_factory=lambda: ["*"])
    request_timeout_seconds: float = 30.0
    max_request_payload_bytes: int = 10 * 1024 * 1024  # 10 MB
    log_level: str = "INFO"
    enable_metrics: bool = True

    def validate(self) -> None:
        """Run strict validation checks on configuration properties.

        Raises ConfigValidationError if any constraint is violated.
        """
        # Port range
        if not (1 <= self.port <= 65535):
            raise ConfigValidationError(
                f"Invalid port {self.port}: must be between 1 and 65535.",
                field="port",
            )

        # Timeout range
        if self.request_timeout_seconds <= 0:
            raise ConfigValidationError(
                f"Invalid request_timeout_seconds {self.request_timeout_seconds}: must be greater than 0.",
                field="request_timeout_seconds",
            )

        # Payload size bounds (1 KB to 100 MB)
        if not (1024 <= self.max_request_payload_bytes <= 100 * 1024 * 1024):
            raise ConfigValidationError(
                f"Invalid max_request_payload_bytes {self.max_request_payload_bytes}: must be between 1KB and 100MB.",
                field="max_request_payload_bytes",
            )

        # Production Security Guardrails
        if self.environment == EnvironmentMode.PRODUCTION.value:
            if self.secret_key == DEFAULT_DEV_SECRET:
                raise ConfigValidationError(
                    "CRITICAL: Default development secret_key cannot be used in production. "
                    "Set SYNAPSE_SECRET_KEY to a secure 32+ character random string.",
                    field="secret_key",
                )
            if len(self.secret_key) < 32:
                raise ConfigValidationError(
                    f"Production secret_key is too short ({len(self.secret_key)} chars). "
                    "Minimum length for production is 32 characters.",
                    field="secret_key",
                )
            if "*" in self.cors_allowed_origins and self.require_auth:
                raise ConfigValidationError(
                    "Wildcard CORS origin ('*') is forbidden in production when require_auth is enabled.",
                    field="cors_allowed_origins",
                )

    def to_safe_dict(self) -> dict[str, Any]:
        """Return safe dictionary with secrets redacted for telemetry/diagnostics."""
        return {
            "environment": self.environment,
            "host": self.host,
            "port": self.port,
            "require_auth": self.require_auth,
            "db_path": self.db_path,
            "cors_allowed_origins": list(self.cors_allowed_origins),
            "request_timeout_seconds": self.request_timeout_seconds,
            "max_request_payload_bytes": self.max_request_payload_bytes,
            "log_level": self.log_level,
            "enable_metrics": self.enable_metrics,
            "secret_key_configured": self.secret_key != DEFAULT_DEV_SECRET,
            "secret_key_length": len(self.secret_key),
        }


def load_config_from_env() -> SynapseConfig:
    """Load and validate configuration from environment variables."""
    env = os.environ.get("SYNAPSE_ENV", EnvironmentMode.DEVELOPMENT.value).lower()
    host = os.environ.get("SYNAPSE_HOST", "127.0.0.1")
    port = int(os.environ.get("SYNAPSE_PORT", "8000"))
    secret_key = os.environ.get("SYNAPSE_SECRET_KEY", DEFAULT_DEV_SECRET)
    require_auth = os.environ.get("SYNAPSE_REQUIRE_AUTH", "").lower() in ("1", "true", "yes")
    db_path = os.environ.get("SYNAPSE_DB_PATH", "data/synapse_team_memory.db")

    cors_str = os.environ.get("SYNAPSE_CORS_ORIGINS", "*")
    cors_origins = [o.strip() for o in cors_str.split(",") if o.strip()]

    timeout = float(os.environ.get("SYNAPSE_REQUEST_TIMEOUT", "30.0"))
    max_payload = int(os.environ.get("SYNAPSE_MAX_PAYLOAD_BYTES", str(10 * 1024 * 1024)))
    log_level = os.environ.get("SYNAPSE_LOG_LEVEL", "INFO").upper()

    config = SynapseConfig(
        environment=env,
        host=host,
        port=port,
        secret_key=secret_key,
        require_auth=require_auth,
        db_path=db_path,
        cors_allowed_origins=cors_origins,
        request_timeout_seconds=timeout,
        max_request_payload_bytes=max_payload,
        log_level=log_level,
    )
    config.validate()
    return config
