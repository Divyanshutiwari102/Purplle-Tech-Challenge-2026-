# Store Intelligence

Real-time analytics for offline retail. Raw CCTV → structured events →
queryable REST API → live dashboard.

## Quickstart (5 commands)

```bash
git clone <this-repo>.git store-intelligence && cd store-intelligence
docker compose up -d --build
curl -s http://localhost:8000/health | python -m json.tool
curl -s -X POST http://localhost:8000/events/ingest \
  -H 'Content-Type: application/json' \
  --data-binary @data/sample_events.json | python -m json.tool
open http://localhost:8501          # dashboard (use `start` on Windows)
```

- **API** — http://localhost:8000  (OpenAPI at `/docs`)
- **Dashboard** — http://localhost:8501
- **Health** — http://localhost:8000/health

## What's running

| Service     | Port | Purpose                             |
| ----------- | ---- | ----------------------------------- |
| `api`       | 8000 | FastAPI + SQLite (WAL)              |
| `dashboard` | 8501 | Streamlit, refreshes every 5s       |

A named volume `store-data` holds the SQLite file across restarts.

## Run the detection pipeline against the clips

The pipeline is packaged separately because it carries the heavier
computer-vision dependencies (torch, ultralytics, OpenCV). It can be
run on a workstation and the events POSTed to the API container.

```bash
# 1. Install pipeline deps (once, on the host or a build container)
pip install -r requirements.txt

# 2. Process every clip in data/clips and emit JSONL into data/events
./pipeline/run.sh data/clips data/layout/store_layout.json

# 3. Replay those events into the API
python -m pipeline.replay --events-dir data/events --api http://localhost:8000

# 4. (optional) simulate real-time for the dashboard demo
python -m pipeline.replay --events-dir data/events --api http://localhost:8000 --rate 5
```

`pipeline/run.sh` discovers stores by parsing clip filenames of the
form `STORE_BLR_002_CAM_ENTRY_01.mp4`. Layout and zones come from
`data/layout/store_layout.json` (sample committed in this repo).

## Verify the system is working

```bash
# Liveness + DB
curl http://localhost:8000/health

# Ingest a single event (works even before the pipeline runs)
curl -X POST http://localhost:8000/events/ingest \
  -H 'Content-Type: application/json' \
  -d '{"events":[{"event_id":"d1f4f5b8-1f3c-4f8a-9f9b-aaaaaaaaaaaa",
       "store_id":"STORE_BLR_002","camera_id":"CAM_ENTRY_01",
       "visitor_id":"VIS_demo","event_type":"ENTRY",
       "timestamp":"2026-05-30T12:00:00Z","zone_id":null,
       "dwell_ms":0,"is_staff":false,"confidence":0.95,
       "metadata":{"queue_depth":null,"sku_zone":null,"session_seq":1}}]}'

# Read back the metrics
curl http://localhost:8000/stores/STORE_BLR_002/metrics
curl http://localhost:8000/stores/STORE_BLR_002/funnel
curl http://localhost:8000/stores/STORE_BLR_002/heatmap
curl http://localhost:8000/stores/STORE_BLR_002/anomalies
```

## Running the test suite

```bash
pip install -r requirements.txt
pytest -q --cov=app --cov=pipeline --cov-report=term-missing
```

Each test file's first lines are a `# PROMPT:` block (the AI prompt I
used to bootstrap the file) and a `# CHANGES MADE:` block (what I
edited afterwards and why).

## Layout

```
store-intelligence/
├── pipeline/
│   ├── detect.py      # YOLOv8 + IoU tracker + Re-ID + zone classification
│   ├── tracker.py     # Re-ID and staff heuristic
│   ├── emit.py        # event builder + JSONL writer
│   ├── replay.py      # POST JSONL to the API
│   └── run.sh         # one command, all stores
├── app/
│   ├── main.py        # FastAPI entrypoint
│   ├── models.py      # Pydantic v2 event schema + validators
│   ├── ingestion.py   # validate, dedup, persist
│   ├── metrics.py     # /metrics, /funnel, /heatmap, POS correlation
│   ├── funnel.py      # re-export to keep suggested layout
│   ├── anomalies.py   # /anomalies — queue spike, dead zone, …
│   ├── health.py      # /health
│   ├── logging_mw.py  # structured-log middleware (trace_id, latency_ms)
│   ├── db.py          # sqlite3 connection helpers
│   └── schema.sql     # tables, indexes, CHECK constraints
├── dashboard/
│   └── app.py         # Streamlit, polls API every 5s
├── tests/
│   ├── test_pipeline.py   # edge cases from the problem statement
│   ├── test_metrics.py    # zero purchases, re-entry, dedup, POS corr
│   ├── test_anomalies.py  # queue spike, dead zone, stale camera, /health
│   └── test_models.py     # Pydantic validation rules
├── docs/
│   ├── DESIGN.md      # architecture + AI-Assisted Decisions
│   └── CHOICES.md     # 3 decisions with full reasoning
├── data/
│   ├── sample_events.json     # works with curl, see Quickstart
│   └── layout/store_layout.json
├── docker-compose.yml
├── Dockerfile.api
├── Dockerfile.dashboard
├── Dockerfile.pipeline
└── README.md
```

## Configuration

| Env var                        | Default                  | Notes                                |
| ------------------------------ | ------------------------ | ------------------------------------ |
| `STORE_INTEL_DB`               | `/data/store_intel.db`   | SQLite path inside the API container |
| `STORE_INTEL_API`              | `http://api:8000`        | Dashboard → API URL                  |
| `STORE_INTEL_DEFAULT_STORE`    | `STORE_BLR_002`          | Initial store shown in the dashboard |
| `STORE_INTEL_REFRESH`          | `5`                      | Dashboard refresh interval (s)       |

## Troubleshooting

- **`/health` returns `degraded`** — DB is unreachable. Inspect the API
  logs (`docker compose logs api`).
- **Dashboard says "Cannot reach API"** — the dashboard waits for the
  API healthcheck to pass; start them with `docker compose up -d` and
  give it ~15s.
- **Pipeline fails on `import ultralytics`** — install the heavier
  pipeline requirements: `pip install -r requirements.txt`. The API
  itself does not need ultralytics.
