# Store Intelligence — Design

## 1. What this system does

Apex Retail wants the same instrumentation for offline stores that they
have online. We turn raw CCTV footage into a stream of structured
behavioural events, materialise them into a SQLite store, and serve
real-time analytics over a small REST surface. A Streamlit dashboard
subscribes to the API and updates every five seconds.

## 2. Architecture

```
   ┌────────────┐    frames    ┌────────────────┐   events.jsonl    ┌──────────────────┐
   │  CCTV mp4  │ ───────────► │ Detection      │ ────────────────► │ POST /events     │
   │  3 cameras │   15 fps     │ pipeline       │   batches ≤ 500   │ /ingest          │
   └────────────┘              │ YOLOv8 + IoU   │                   └────────┬─────────┘
                               │ + Re-ID + zone │                            │
                               │   classifier   │                            ▼
                               └────────────────┘                   ┌──────────────────┐
                                                                    │ SQLite (WAL)     │
                                                                    │ events,sessions, │
                                                                    │ txns,anomalies   │
                                                                    └────────┬─────────┘
                                                                             │ SQL
                                                                             ▼
                ┌──────────────────┐    HTTP/JSON    ┌──────────────────────────────────┐
                │ Streamlit        │ ◄───────────────│ FastAPI                          │
                │ dashboard 8501   │                 │ /metrics /funnel /heatmap        │
                └──────────────────┘                 │ /anomalies /health               │
                                                    └──────────────────────────────────┘
```

Three processes, one volume. The detection pipeline can run on any
machine with the clips; events are POSTed to the API. The API is
stateless aside from the SQLite file on the shared volume, which means
horizontal scaling later is a matter of swapping SQLite for Postgres
and putting the API behind a load balancer.

## 3. Data flow (one frame → one analytics number)

1. `cv2.VideoCapture` reads a frame at 15 fps.
2. YOLOv8n detects person bounding boxes (class 0).
3. `IoUTracker` assigns track IDs across frames.
4. For ENTRY cameras, the centroid's vertical position is checked
   against the layout's `entry_line_y`. Three consecutive frames on
   one side confirm direction (entry/exit) — this kills jitter at the
   threshold.
5. The `Tracker` Re-ID checks whether this person matches a recently
   exited track. If yes, REENTRY; if no, ENTRY with a new visitor_id.
6. For floor/billing cameras, the centroid is point-in-polygon tested
   against zones from `store_layout.json`. ZONE_ENTER, ZONE_DWELL
   (every 30s of continued presence), and ZONE_EXIT are emitted.
7. Events are written as JSONL and POSTed to `/events/ingest` in
   batches of up to 500.
8. The API validates with Pydantic v2, dedups by event_id (`INSERT OR
   IGNORE`), and materialises a `visitor_sessions` row on ENTRY/EXIT.
9. `/metrics` walks events for the day, joins to POS by store + 5-minute
   window, and computes conversion. `/funnel` collapses by visitor_id
   so re-entries don't double-count.
10. Dashboard polls `/metrics`, `/anomalies`, `/funnel`, `/heatmap`
    every 5s and renders.

## 4. Event lifecycle

```
ENTRY          → opens a visitor_session row (entry_time)
ZONE_ENTER     → bookkeeping in the detector + row in events
ZONE_DWELL     → emitted every 30s while the person stays in zone
ZONE_EXIT      → bookkeeping; closes the dwell window
BILLING_QUEUE_JOIN
               → carries queue_depth in metadata; feeds anomaly detector
BILLING_QUEUE_ABANDON
               → emitted when a visitor leaves billing without a
                 transaction in the next 5 min
EXIT           → closes the visitor_session (exit_time)
REENTRY        → reuses visitor_id, sets exit_time = NULL again,
                 increments reentry_count
```

## 5. Why these specific decisions

### 5.1 SQLite, not Postgres (yet)

For 5 stores × 3 cameras × 20 minutes the row count is ~10k–50k events.
SQLite in WAL mode handles this with a single file, no daemon, and zero
operational overhead in the docker-compose. The schema is written so
the migration to Postgres is a `psql -f schema.sql` away (see
CHOICES.md, Decision 3).

### 5.2 Event-source-of-truth + materialised sessions

Events are append-only. The `visitor_sessions` table is a derived view
that we maintain on ingest. If it ever gets out of sync we can rebuild
it from events. The trade-off is an extra write per ENTRY/EXIT, which
is cheap; the benefit is that `/funnel` and `/metrics` don't have to
walk the full event log per request.

### 5.3 Idempotent ingest

`INSERT OR IGNORE` on `event_id` makes retries safe. The detection
pipeline can be at-least-once and the dashboard can replay JSONLs
without inflating counts. The response separates `ingested` from
`duplicates` so the producer can detect a network retry.

### 5.4 Entry/exit by line crossing, not zone enter

Bounding-box centroids are noisy near the threshold. We require three
consecutive frames on one side of the virtual line before emitting an
ENTRY/EXIT event. This is cheap (a deque per track) and removes the
biggest source of inflated counts.

### 5.5 Re-ID via colour histogram + region + time gap

A real Re-ID model needs a GPU. For the held-out clips and a 48-hour
window, an HSV colour histogram on the bbox crop, combined with the
constraint "same entry camera and within 30 minutes", is sufficient.
The cosine threshold (0.75) is tunable from one place.

### 5.6 Staff via three OR-ed heuristics

We never silently drop staff. We mark `is_staff=true` and let the API
exclude them from customer metrics. The three rules are:
duration > 45 min, billing crossings > 8, or seen on > 2 cameras.
A real deployment would replace this with a uniform classifier, but
the heuristic is interpretable and works on the released clips.

## 6. AI-Assisted Decisions

### 6.1 AGREED: schema layout

I asked Claude for a normalised SQL schema given the event catalogue and
asked it to recommend indexes for the analytics queries. It recommended
the composite `(store_id, timestamp)` and `(store_id, event_type)`
indexes, which I kept verbatim — they're exactly the access pattern of
`/metrics` and `/anomalies`. I added `(store_id, camera_id, timestamp)`
on top because the AI didn't see the staleness check.

### 6.2 OVERRODE: re-entry approach

The first suggestion was to use `torchreid` with an OSNet model. That
adds ~400 MB to the image, needs a GPU to run in real time, and was
overkill for a 48-hour challenge with five stores. I overrode this and
went with the HSV-histogram + region-and-gap heuristic. The trade-off
is documented in CHOICES.md — under high traffic with similar clothing
the heuristic will collide, and that's fine for now because the
problem statement explicitly asks how confidence degrades, not how
perfectly we match.

### 6.3 USED FOR EVALUATION, NOT GENERATION: detection model choice

I gave Claude the constraint table (CPU, 1080p/15fps, 5 stores, 48h
deadline) and asked it to compare YOLOv8n / YOLOv8s / RT-DETR /
MediaPipe on (latency, accuracy, license, ease of integration). I used
the answer as an evaluation grid, not a recipe. The final pick
(YOLOv8n with the option to bump to YOLOv8s) is mine — see CHOICES.md
Decision 1.

## 7. Trade-offs and what I'd do with more time

- **Re-ID**: replace the colour histogram with OSNet embeddings on a
  GPU-backed inference service, keep the heuristic as fallback for
  CPU-only edge nodes.
- **Storage**: move events into Postgres and put a 1-hour Redis cache
  in front of `/metrics`, `/funnel`, `/heatmap`. They're idempotent
  reads and the dashboard polls them every 5s.
- **Streaming**: replace JSONL replay with Kafka. The `/events/ingest`
  endpoint stays as a thin shim for non-Kafka producers.
- **Anomaly detection**: today it's threshold-based. Conversion drop
  vs 7-day average is a baseline; a Holt-Winters or simple EWMA on
  per-hour conversion would catch slower regressions.
- **Dashboard**: WebSocket push instead of poll, and a real heatmap
  visualisation (currently a dataframe — pandas was the right cost
  for the time budget).
- **Tests**: property-based tests for the funnel monotonicity
  invariants (entry ≥ zone_visit ≥ billing ≥ purchase), and a recorded
  20-minute clip with a hand-labelled ground truth committed under
  `tests/fixtures/`.

## 8. Acceptance gate self-check

- `docker compose up` builds and starts both services with healthchecks.
- `POST /events/ingest` validates against the exact schema in the
  problem statement and returns 200 even on partial failure.
- `GET /stores/STORE_BLR_002/metrics` returns a JSON object with
  `unique_visitors`, `conversion_rate`, `avg_dwell_per_zone`,
  `current_queue_depth`, `abandonment_rate`.
- DESIGN.md and CHOICES.md are both > 250 words with substantive
  reasoning, not boilerplate.
- Each test file's first lines are a `# PROMPT:` / `# CHANGES MADE:`
  block describing the AI prompt and what I changed.
