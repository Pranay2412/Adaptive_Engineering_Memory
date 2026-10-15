# Synapse Platform: Multi-Service Docker Deployment & Operations Guide

This guide details the complete containerized deployment architecture for the **Synapse Adaptive Engineering Memory Platform**. All platform services and features are containerized and orchestrated through Docker Compose.

---

## 1. Multi-Service Container Architecture

The Synapse container deployment provides an enterprise-ready, observable, and isolated runtime across 7 dedicated services connected via an internal bridge network (`synapse_network`):

```text
                  +--------------------------------------------------------+
                  |                      HOST NETWORK                      |
                  +--------------------------------------------------------+
                        |                 |                    |
     Port 8088          |  Port 8081      |  Port 3001         |  Port 9091
        v               |     v           |     v              |     v
+------------------+    |  +------------+ |  +------------+    |  +------------+
|   synapse-api    |    |  |synapse-mcp | |  |  grafana   |    |  | prometheus |
| (FastAPI + Web   |    |  | (MCP Server| |  | (Dashboard |    |  |  (Metrics  |
|   Dashboard)     |    |  |  SSE Mode) | |  |  Console)  |    |  |  Scraper)  |
+------------------+    |  +------------+ |  +------------+    |  +------------+
        |               |        |        |        ^           |        ^
        |               +--------+--------+        |           |        |
        |                        |                 +-----------+--------+
        v                        v                             |
+--------------------------------------------------------------+---------+
|                  synapse_network (Internal Bridge)                     |
+------------------------------------------------------------------------+
        |                                   |                    |
        v                                   v                    v
+------------------+               +------------------+ +----------------+
|  synapse-worker  |               |     postgres     | |     neo4j      |
| (Background Sync |               |  (Relational DB  | | (Entity Graph  |
|    & Indexing)   |               |   Team Memory)   | |   Database)    |
+------------------+               +------------------+ +----------------+
```

---

## 2. Service Inventory & Port Mappings

Host port mappings are designed to prevent conflicts with other services on the developer machine:

| Container Service | Internal Port | Host Port | Role & Capabilities |
|---|---|---|---|
| **`synapse-api`** | `8000` | `8088` | FastAPI REST API, Engineering Dashboard UI, `/metrics` endpoint |
| **`synapse-mcp`** | `8001` | `8081` | Model Context Protocol (MCP) server running SSE transport for IDEs |
| **`synapse-worker`**| N/A | N/A | Background worker daemon for incremental indexing and maintenance |
| **`postgres`** | `5432` | `5435` | PostgreSQL 16 relational database for team decisions and audit logs |
| **`neo4j`** | `7474`, `7687` | `7474`, `7687` | Neo4j graph database for deep code symbol and document cross-linking |
| **`prometheus`** | `9090` | `9091` | Prometheus scraper collecting ACO stage latencies and token metrics |
| **`grafana`** | `3000` | `3001` | Pre-provisioned Grafana dashboards for real-time observability |

---

## 3. Quickstart: Building & Launching All Services

### 3.1 Prerequisites
- Docker Engine 24.0+ or Docker Desktop (Windows / macOS / Linux)
- Docker Compose v2.20+

### 3.2 Initialize Environment Configuration
```bash
# Copy the environment template
cp .env.example .env
```

Review `.env` to verify your desired ports and database passwords:
```dotenv
SYNAPSE_PORT=8088
SYNAPSE_MCP_PORT=8081
POSTGRES_PORT=5435
PROMETHEUS_PORT=9091
GRAFANA_PORT=3001
SYNAPSE_SECRET_KEY=synapse-prod-secret-key-9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c
POSTGRES_PASSWORD=synapse_secure_db_pass_123
```

### 3.3 Build and Launch Containers
```bash
# Build multi-stage images
docker compose build

# Launch all 7 services in background
docker compose up -d
```

### 3.4 Verify Service Health
```bash
# Check container status and health probes
docker compose ps
```

Expected output:
```text
NAME                IMAGE                  STATUS                    PORTS
synapse-api         fyp-synapse-api        Up (healthy)              0.0.0.0:8088->8000/tcp
synapse-mcp         fyp-synapse-api        Up (healthy)              0.0.0.0:8081->8001/tcp
synapse-worker      fyp-synapse-api        Up                        
synapse-postgres    postgres:16-alpine     Up (healthy)              0.0.0.0:5435->5432/tcp
synapse-neo4j       neo4j:5.26-community   Up (healthy)              0.0.0.0:7474->7474/tcp, 0.0.0.0:7687->7687/tcp
synapse-prometheus  prom/prometheus:v2.54.1 Up                       0.0.0.0:9091->9090/tcp
synapse-grafana     grafana/grafana:11.2.0 Up                        0.0.0.0:3001->3000/tcp
```

---

## 4. Web Consoles & Endpoints

Once running, access the platform interfaces on your browser:

- **Engineering Memory Dashboard:** [http://localhost:8088/dashboard](http://localhost:8088/dashboard)  
  *Explore code symbols, architectural decisions, live query console, and token budget analytics.*
- **Interactive REST API Documentation (Swagger):** [http://localhost:8088/docs](http://localhost:8088/docs)
- **Liveness & Readiness Probes:**
  - Liveness: `curl http://localhost:8088/health`
  - Readiness: `curl http://localhost:8088/ready`
- **Prometheus Metrics Explorer:** [http://localhost:9091](http://localhost:9091)
- **Grafana Observability Dashboard:** [http://localhost:3001](http://localhost:3001)  
  *Default Credentials:* User: `admin` | Password: `synapse_grafana_pass`  
  *Pre-provisioned dashboard: "Synapse Engineering Memory Telemetry"*
- **Neo4j Graph Browser:** [http://localhost:7474](http://localhost:7474)

---

## 5. Connecting IDE Coding Assistants via MCP

The `synapse-mcp` service exposes a Model Context Protocol endpoint over Server-Sent Events (SSE) at `http://localhost:8081/sse`.

### 5.1 Cursor Configuration
Add to `~/.cursor/mcp.json` or project `.cursor/mcp.json`:
```json
{
  "mcpServers": {
    "synapse": {
      "url": "http://localhost:8081/sse"
    }
  }
}
```

### 5.2 Claude Desktop Configuration
Add to `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "synapse": {
      "url": "http://localhost:8081/sse"
    }
  }
}
```

### 5.3 Stdio Mode (Alternative for CLI)
To run in direct stdio mode inside the container:
```bash
docker compose run --rm synapse-api python -m backend.mcp_server --transport stdio
```

---

## 6. Containerized Operations & CLI Utilities

All platform maintenance scripts can be executed directly inside the container without local Python installations:

### 6.1 Run Deployment Smoke Test
```bash
docker compose run --rm synapse-api smoke-test
```
*Verifies configuration files, fresh database migrations, API liveness/readiness, ACO context queries, and cryptographic backup/restore.*

### 6.2 Run End-to-End Demo
```bash
docker compose run --rm synapse-api demo
```
*Executes the complete 8-stage verification pipeline with synthetic fixtures and Section 7 token accounting.*

### 6.3 Database Migrations
```bash
# Check migration status
docker compose run --rm synapse-api python scripts/migrate.py status

# Apply forward migrations
docker compose run --rm synapse-api python scripts/migrate.py up

# Verify schema integrity
docker compose run --rm synapse-api python scripts/migrate.py verify
```

### 6.4 Zero-Downtime Backup & Restore
```bash
# Create verified backup archive
docker compose run --rm synapse-api python scripts/backup_restore.py create --note "Nightly container backup"

# List existing backups
docker compose run --rm synapse-api python scripts/backup_restore.py list

# Verify backup checksums
docker compose run --rm synapse-api python scripts/backup_restore.py verify backups/<archive_name>.tar.gz
```

---

## 7. Persistent Data & Volume Management

Synapse persists all databases and index caches across container restarts using named Docker volumes:

| Volume Name | Container Path | Purpose |
|---|---|---|
| `synapse_data` | `/app/data` | SQLite database (`synapse_team_memory.db`) and local cache files |
| `synapse_graph` | `/app/graphify-out` | Graph artifacts, entity embeddings, and schema checkpoints |
| `synapse_backups` | `/app/backups` | Compressed `.tar.gz` backup archives and SHA-256 manifests |
| `synapse_postgres_data`| `/var/lib/postgresql/data`| PostgreSQL database tables and transaction write-ahead logs |
| `synapse_neo4j_data` | `/data` | Neo4j graph store, nodes, relationships, and indices |
| `synapse_prometheus_data`| `/prometheus` | Scraped time-series telemetry data |
| `synapse_grafana_data` | `/var/lib/grafana` | Custom Grafana user preferences and dashboard configurations |

To perform a clean reset of all state:
```bash
docker compose down -v
```
*(Warning: this purges all persisted data and restores the environment to pristine initial state).*
