"""Configuration Management for Synapse Python SDK (Phase 11).

Defines ClientConfig:
- Target endpoint URL and connection settings
- Authentication mechanisms (Bearer tokens, API keys, development headers)
- Execution timeouts and retry policies
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ClientConfig:
    """Configuration options for SynapseClient."""

    base_url: str = "http://localhost:8000"
    api_key: str | None = None
    bearer_token: str | None = None
    timeout: float = 30.0
    max_retries: int = 3
    backoff_factor: float = 0.5
    auto_idempotency_keys: bool = False
    custom_headers: dict[str, str] = field(default_factory=dict)

    # Local development simulation identities (ignored if auth credentials provided)
    default_principal_id: str | None = None
    default_tenant_id: str | None = None
    default_allowed_repositories: list[str] | None = None
    default_roles: list[str] | None = None

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        if self.timeout <= 0:
            raise ValueError(f"Invalid timeout {self.timeout}: must be greater than 0.")
        if self.max_retries < 0:
            raise ValueError(f"Invalid max_retries {self.max_retries}: cannot be negative.")
        if self.backoff_factor < 0:
            raise ValueError(f"Invalid backoff_factor {self.backoff_factor}: cannot be negative.")

    def get_auth_headers(self) -> dict[str, str]:
        """Construct authentication and identity headers based on configured credentials."""
        headers: dict[str, str] = {}

        # 1. Bearer Token
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token.strip()}"
            return headers

        # 2. API Key
        if self.api_key:
            headers["X-API-Key"] = self.api_key.strip()
            return headers

        # 3. Development simulation headers
        if self.default_principal_id:
            headers["X-Principal-ID"] = self.default_principal_id
        if self.default_tenant_id:
            headers["X-Tenant-ID"] = self.default_tenant_id
        if self.default_allowed_repositories:
            headers["X-Allowed-Repositories"] = ",".join(self.default_allowed_repositories)
        if self.default_roles:
            headers["X-Roles"] = ",".join(self.default_roles)

        return headers
