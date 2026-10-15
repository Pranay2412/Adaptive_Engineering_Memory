# Synthetic Demo Repository Fixture

> **NOTICE: SYNTHETIC DEMO FIXTURE - NO PRODUCTION DATA OR REAL SECRETS**
> This repository contains synthetic code, architecture specifications, and runbooks used exclusively
> for demonstrating and benchmarking the Synapse Adaptive Engineering Memory Platform.

## Architecture Overview

This fixture models a modular cloud service with:
- **`auth/`**: Authentication service and JWT token lifecycle management (`AuthService`, `TokenManager`).
- **`billing/`**: Billing computation engine (`BillingEngine`) to verify domain isolation during context retrieval.
- **`docs/architecture/`**: High-level system architecture and security specifications (`auth_overview.md`).
- **`docs/troubleshooting/`**: Operational runbooks for debugging authentication and token failures (`token_errors.md`).
