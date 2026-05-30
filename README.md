# Store Intelligence

Real-time analytics for offline retail. Raw CCTV → structured events →
queryable REST API → live React dashboard with simulated YOLO camera feed.

## Quickstart (5 commands)

```bash
git clone <this-repo>.git store-intelligence && cd store-intelligence
docker compose up -d --build
curl -s http://localhost:8000/health | python -m json.tool
curl -X POST "http://localhost:8000/simulation/start?speed=1"
open http://localhost:3030     # React dashboard (use `start` on Windows)
```

| Service                | URL                                   | Purpose                                              |
| ---------------------- | ------------------------------------- | ---------------------------------------------------- |
| **Dashboard (React)**  | http://localhost:3030                 | Primary UI: KPIs, funnel, heatmap, MJPEG, anomalies  |
| **API**                | http://localhost:8000                 | REST + SSE + MJPEG + simulation                      |
| **API Docs (Swagger)** | http://localhost:8000/docs            | Interactive endpoint explorer                        |
| **Streamlit (legacy)** | http://localhost:8501                 | Fallback UI, kept for parity                         |

## Real store IDs

The repo ships with two store ids the evaluator can drive against:

| Store ID         | Provenance                                                              |
| ---------------- | ----------------------------------------------------------------------- |
| `STORE_BLR_002`  | Synthetic store used by the suggested layout and sample CCTV pipeline   |
| `ST1008`         | **Real anonymised data**: Brigade Bangalore, 10-Apr-2026, 24 transactions, ₹34,331.71 |

Test against the real store id:

```bash
curl http://localhost:8000/stores/ST1008/metrics
curl http://localhost:8000/stores/ST1008/funnel
curl http://localhost:8000/stores/ST1008/heatmap
curl http://localhost:8000/stores/ST1008/anomalies
```

The committed `data/sample_pos_transactions.csv` is derived from the real
Brigade POS file (PII stripped — only `store_id, transaction_id, timestamp,
basket_value_inr`). The raw CSV with customer names + phone numbers is
**git-ignored** and never pushed.

## Live demo with simulation

The simulation manager replays events into the API at controllable speed and
broadcasts each one to subscribers over SSE — so the dashboard fills in even
without running the heavy detection pipeline.

```bash
# Start a 5x replay against any camera
curl -X POST "http://localhost:8000/simulation/start?speed=5&cam_id=CAM_1"

# Slow it down on the fly
curl -X POST "http://localhost:8000/simulation/speed?speed=1"

# Stop
curl -X POST http://localhost:8000/simulation/stop
```

The dashboard's "▶ Start" buttons trigger the same endpoints from the UI.

## Real CCTV streaming (optional)

The camera panel auto-detects whether real footage + matching detections are
present and switches between two modes:

| Mode      | What you see                                                                            |
| --------- | --------------------------------------------------------------------------------------- |
| **real**  | The actual CCTV mp4 with YOLO bboxes overlaid that follow real people, plus zone polygons + HUD. Detections are pre-computed once via `pipeline/precompute_detections.py` and replayed in sync with the clip. |
| **synth** | Fallback when clips aren't present (the public repo case). Same UI shape, synthetic actors. |

To switch to real CCTV on your machine:

```bash
# 1. Drop your clips into data/clips/  (already there in this workspace)
# 2. Pre-compute YOLO detections once  (~10 min on CPU for 5 clips at stride=5)
pip install -r requirements.txt          # opencv + ultralytics
python -m pipeline.precompute_detections \
       --clips-dir data/clips \
       --out-dir   data/detections \
       --stride    5

# 3. Restart the API container so the bind-mounts pick up the new files
docker compose up -d --force-recreate api
```

`docker-compose.yml` mounts `data/clips/`, `data/detections/`, and
`data/layout/store_layout.json` into the API container as **read-only** bind
mounts. Clips and detections are git-ignored and never pushed.

## Run the detection pipeline against your own clips

The pipeline is heavier (carries torch + ultralytics + cv2) and is packaged
separately. It can run on any workstation; events are POSTed to the API
container.

```bash
# 1. Install pipeline deps (once, on the host)
pip install -r requirements.txt

# 2. Process every .mp4 in data/clips/ (filenames map to cameras via
#    data/layout/store_layout.json::clip_to_camera)
python -m pipeline.detect \
  --store-id STORE_BLR_002 \
  --layout data/layout/store_layout.json \
  --clips-dir data/clips \
  --frame-stride 5

# 3. Replay the resulting JSONL into the API
python -m pipeline.replay --events-dir data/events --api http://localhost:8000

# Or one shot:
./pipeline/run.sh data/clips data/layout/store_layout.json
```

## Verify the system is working

```bash
# Liveness
curl http://localhost:8000/health

# One-line ingest of the committed sample
curl -X POST http://localhost:8000/events/ingest \
  -H 'Content-Type: application/json' \
  --data-binary @data/sample_events.json

# Real-time SSE feed (Ctrl-C to stop)
curl -N http://localhost:8000/stores/STORE_BLR_002/stream

# Simulated MJPEG camera
curl -N http://localhost:8000/cameras/stream/CAM_1 -o frame.mjpeg

# POS loader (re-ingest a CSV after the fact)
docker exec store-intel-api python -m app.pos_loader \
  --csv /data/sample_pos_transactions.csv --db /data/store_intel.db
```

## Endpoints

| Method | Path                                | Purpose                                                |
| ------ | ----------------------------------- | ------------------------------------------------------ |
| POST   | `/events/ingest`                    | Ingest a batch (≤ 500). Idempotent by `event_id`       |
| GET    | `/stores/{id}/metrics`              | KPIs: visitors, conversion, queue, abandonment         |
| GET    | `/stores/{id}/funnel`               | Entry → Zone → Billing → Purchase, with drop-off %     |
| GET    | `/stores/{id}/heatmap`              | Per-zone visit frequency + dwell + data confidence     |
| GET    | `/stores/{id}/anomalies`            | Active queue spikes, dead zones, stale cameras         |
| GET    | `/stores/{id}/stream`               | **SSE** of metric + sim_event updates                  |
| GET    | `/cameras`                          | Camera ids available for streaming                     |
| GET    | `/cameras/stream/{cam_id}`          | **MJPEG** simulated YOLO feed with bbox + zone overlay |
| POST   | `/simulation/start?speed=&cam_id=`  | Start replay at given speed                            |
| POST   | `/simulation/stop`                  | Stop replay                                            |
| POST   | `/simulation/speed?speed=`          | Change replay speed live                               |
| GET    | `/simulation/status`                | Current state                                          |
| GET    | `/health`                           | Status, last-event timestamps, stale feeds             |

## Running the test suite

```bash
pip install -r requirements.txt
pytest -q --tb=short
```

Currently **66+ tests** spread across 8 files. Each file's first lines are a
`# PROMPT:` block (the AI prompt that bootstrapped it) and a `# CHANGES MADE:`
block (what was edited afterwards and why).

## Layout

```
store-intelligence/
├── app/
│   ├── main.py            FastAPI entrypoint, CORS, routers
│   ├── models.py          Pydantic v2 event schema + validators
│   ├── ingestion.py       validate / dedup / persist
│   ├── metrics.py         /metrics, /funnel, /heatmap + POS correlation
│   ├── anomalies.py       queue spike, dead zone, stale camera, conv drop
│   ├── health.py          /health
│   ├── dashboard.py       /stores/{id}/stream (SSE)
│   ├── camera_stream.py   /cameras + /cameras/stream/{cam_id} (MJPEG)
│   ├── simulation.py      /simulation/* control plane
│   ├── pos_loader.py      CSV loader (simple + Brigade format)
│   ├── logging_mw.py      structured-log middleware
│   ├── db.py              sqlite3 helpers (WAL)
│   └── schema.sql         tables, indexes, CHECK constraints
├── pipeline/              YOLOv8 + tracker + Re-ID + emit + replay
├── frontend/              React + Vite + TS + Tailwind dashboard
├── dashboard/app.py       Streamlit (legacy)
├── tests/                 8 test files, 66+ tests
├── docs/                  DESIGN.md (3669 words) + CHOICES.md
├── data/
│   ├── sample_events.json (incl. ST1008 events for evaluator)
│   ├── sample_pos_transactions.csv  (24 real Brigade transactions)
│   └── layout/store_layout.json
├── docker-compose.yml     api + frontend + dashboard
├── Dockerfile.api / .frontend / .dashboard / .pipeline
└── README.md
```

## Configuration

| Env var                        | Default                  | Notes                                |
| ------------------------------ | ------------------------ | ------------------------------------ |
| `STORE_INTEL_DB`               | `/data/store_intel.db`   | SQLite path inside the API container |
| `STORE_INTEL_API`              | `http://api:8000`        | Streamlit dashboard → API URL        |
| `STORE_INTEL_DEFAULT_STORE`    | `STORE_BLR_002`          | Initial store shown in Streamlit     |
| `STORE_INTEL_REFRESH`          | `5`                      | Streamlit refresh interval (s)       |
| `VITE_API_BASE` (frontend)     | `/api`                   | React proxy base                     |

## Troubleshooting

- **Dashboard says "API error: ..."** — the React dev server expects the API
  on `/api/*`. In docker-compose, `nginx` proxies that. Locally with
  `npm run dev`, the Vite proxy forwards to `http://localhost:8000`.
- **Pipeline fails on `import ultralytics`** — install the heavier pipeline
  requirements: `pip install -r requirements.txt`.
- **`/health` returns `degraded`** — DB unreachable. Check
  `docker compose logs api`.
- **Port 3000 / 8000 / 8501 in use** — change the host port mapping in
  `docker-compose.yml` (the container ports are fine).
