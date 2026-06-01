# Network Path Tracer

Interactive hop-by-hop L2/L3 network path tracer with a React frontend and FastAPI backend.

---

## Features

- **Interactive topology diagram** — React Flow with pan, zoom, minimap
- **L2 / L3 colour coding** — brown for Layer 2, blue for Layer 3
- **Hover tooltips** — device and interface details on hover
- **Click detail panel** — full counters and NetBox links on click
- **Dark / light mode** — persisted to localStorage, respects system preference
- **Export** — PNG (html2canvas) and SVG (programmatic, vector quality)
- **Trace history** — SQLite-backed, searchable, reloadable
- **ECMP-aware** — renders all parallel paths from ECMP traces
- **NetBox integration** — device and interface URLs injected into graph

---

## Quick Start (local development)

### 1. Backend

```bash
cd /path/to/web_path_tracer

# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r tracer_api/requirements.txt

# Configure environment
cp tracer_api/.env.example .env
# Edit .env — fill in TRACER_NETBOX_URL, TRACER_NETBOX_TOKEN,
# TRACER_DEVICE_USERNAME, TRACER_DEVICE_PASSWORD, etc.

# Run the API
python tracer_api/run_api.py
# API available at http://localhost:8000
# Docs at http://localhost:8000/api/docs
```

### 2. Frontend

```bash
cd tracer_frontend

# Install Node dependencies
npm install

# Start dev server (proxies /api to localhost:8000 automatically)
npm run dev
# App available at http://localhost:5173
```

---

## Production (Docker Compose)

### Prerequisites

- Docker 24+
- Docker Compose v2

### Deploy

```bash
# 1. Copy and configure the backend env file
cp tracer_api/.env.example .env
# Edit .env — fill in NetBox and device credentials

# 2. Build and start both services
docker compose up -d --build

# App available at http://localhost (port 80)
# API docs at http://localhost/api/docs
```

The SQLite history database is stored in a named Docker volume (`tracer-data`)
so it survives container restarts and rebuilds.

### Stop

```bash
docker compose down
```

### View logs

```bash
docker compose logs -f tracer-api
docker compose logs -f tracer-frontend
```

---

## Configuration

All backend settings use the `TRACER_` prefix and can be set in `.env` or as
environment variables.  See `tracer_api/.env.example` for the full list.

Key settings:

| Variable                        | Default              | Description                               |
|---------------------------------|----------------------|-------------------------------------------|
| `TRACER_NETBOX_URL`             | *(empty)*            | NetBox base URL — enables NetBox links    |
| `TRACER_NETBOX_TOKEN`           | *(empty)*            | NetBox API token                          |
| `TRACER_DEVICE_USERNAME`        | *(empty)*            | SSH username for network devices          |
| `TRACER_DEVICE_PASSWORD`        | *(empty)*            | SSH password                              |
| `TRACER_CORS_ORIGINS`           | `*`                  | Comma-separated allowed origins           |
| `TRACER_HISTORY_DB_PATH`        | `./trace_history.db` | SQLite history file path                  |
| `TRACER_PORT`                   | `8000`               | API listen port                           |

Frontend environment variable (`tracer_frontend/.env`):

| Variable               | Default | Description                                         |
|------------------------|---------|-----------------------------------------------------|
| `VITE_API_BASE_URL`    | *(empty)* | API base URL; empty = use relative paths (nginx proxy) |

---

## API Reference

| Method | Path                                | Description                        |
|--------|-------------------------------------|------------------------------------|
| POST   | `/api/v1/traces`                    | Start a new trace (async)          |
| GET    | `/api/v1/traces`                    | List recent in-memory traces       |
| GET    | `/api/v1/traces/{id}`               | Get trace status + result          |
| GET    | `/api/v1/traces/{id}/graph`         | Get Cytoscape.js-compatible graph  |
| GET    | `/api/v1/traces/{id}/stream`        | SSE live progress stream           |
| DELETE | `/api/v1/traces/{id}`               | Cancel / delete a trace            |
| GET    | `/api/v1/history`                   | List saved trace history           |
| GET    | `/api/v1/history/{id}`              | Full history entry with graph      |
| DELETE | `/api/v1/history/{id}`              | Delete history entry               |
| GET    | `/api/v1/health`                    | Health check                       |

Interactive docs: `http://localhost:8000/api/docs`

---

## Architecture

```
web_path_tracer/
├── tracer_api/               # FastAPI backend
│   ├── main.py               # Routes (traces + history + health)
│   ├── graph_builder.py      # Cytoscape.js graph builder + NetBox URLs
│   ├── history.py            # SQLite persistence for trace history
│   ├── models.py             # Pydantic request/response models
│   ├── config.py             # Settings (pydantic-settings)
│   ├── cache.py              # TTL caches
│   ├── task_store.py         # In-memory task registry
│   ├── tracer_runner.py      # Background SSH trace execution
│   ├── requirements.txt
│   ├── .env.example
│   └── Dockerfile
│
├── tracer_frontend/          # React + TypeScript + Vite frontend
│   ├── src/
│   │   ├── api/client.ts     # Axios API client
│   │   ├── types/trace.ts    # TypeScript interfaces
│   │   ├── store/            # Zustand stores (trace, history, theme)
│   │   ├── transform/        # Backend JSON → React Flow nodes/edges
│   │   ├── components/       # All React components
│   │   ├── utils/export.ts   # PNG + SVG export
│   │   ├── styles/           # CSS variables theme + globals
│   │   ├── App.tsx
│   │   └── main.tsx
│   ├── nginx.conf
│   ├── Dockerfile
│   └── package.json
│
├── docker-compose.yml        # Runs backend + frontend together
└── README.md
```

---

## Notes

- **History persistence**: SQLite, single file, no additional services required.
  WAL mode is enabled for concurrent read performance.
- **Trace polling**: The frontend polls `GET /api/v1/traces/{id}` every 2 seconds
  until the trace completes or fails (max 5 minutes / 150 polls).
- **NetBox URLs**: Added to graph elements when `TRACER_NETBOX_URL` is configured.
  The URLs are search queries (e.g. `/dcim/devices/?name=...`) so they work
  without knowing NetBox object IDs.
- **ECMP paths**: All parallel paths are rendered. Each ECMP variant is a separate
  flat path in the result; shared devices/links are deduplicated in the graph.
- **Export — PNG**: Uses `html2canvas` on the React Flow renderer element.
  Capture quality depends on browser rendering; scale=2 produces 2× resolution.
- **Export — SVG**: Programmatically constructed from node position data.
  Pure vector, suitable for inclusion in documents.
