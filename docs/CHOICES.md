# CHOICES.md — key engineering decisions, with reasoning

> Specific numbers below come from CPU benchmarks on my own laptop
> (Intel i7-13th gen, no GPU). The footage is 1920×1080 at 25–30 fps,
> 2.1–2.5 minutes per clip. The challenge shipped two stores in the
> v2 resource center: **STORE_BLR_002** (the suggested layout, 5
> cameras) and **ST1008 / Brigade Bangalore** (Store 2, 4 cameras),
> and a cleaned per-line-item `pos_transactions.csv` (101 line items
> spanning 24 carts, ₹34,331.71). The dashboard drives both stores
> from one API via a `?store_id=` parameter.

---

## Decision 1: Detection model

### Options considered

| Option           | Pros                                                       | Cons                                                                |
| ---------------- | ---------------------------------------------------------- | ------------------------------------------------------------------- |
| **YOLOv8n**      | 6 MB weights, ~30 fps on CPU at 640px input                | Misses some partial occlusions in the billing-camera clip           |
| YOLOv8s          | 22 MB, +3-4 mAP over n on partial occlusion                | ~7 fps on CPU — 4× slower for a marginal win on this dataset        |
| RT-DETR-l        | Best mAP in the family, strong on small persons            | Needs GPU for real-time, harder to package into a slim API image    |
| MediaPipe Person | Apache-licensed, lightweight                               | Single-person bias; weak on the group-entry edge case               |
| YOLO-World       | Open-vocabulary; could prompt "person in store uniform"    | Slower, prompts add latency, less battle-tested                     |
| GPT-4V / Gemini Vision (per-frame VLM) | Zero training, qualitative reasoning over scenes | Cost-prohibitive at this scale: 5 clips × 2.5 min × 15 fps = 11,250 frames; at $0.01/frame that is **$112.50 per detection run** before retries — vs $0 for YOLOv8n |

### What AI suggested

Claude's first answer was YOLOv8n with the comment that v8s is the
right upgrade path if accuracy on the billing camera is poor. It also
flagged that re-detecting at 15 fps is wasteful: running detection on
every 3rd–5th frame plus the IoU tracker between detections gets most
of the accuracy at a fraction of the compute. I adopted both points.

### What I chose: YOLOv8n with `--frame-stride 5`

- **Effective detection rate**: clips arrive at 15 fps, I detect on
  every 5th frame → **3 fps detection**, the IoU tracker fills the
  gaps. Pre-computed runs over my 5 clips ranged 90–160 s each on CPU.
- **Per-clip cost**: 5 clips × ~120 s = ~10 minutes total, $0 marginal
  cost. Compare against the VLM number above.
- **Footprint**: 6 MB of weights ship inside a slim container; no
  torch model download at runtime when weights are vendored.

### Why

1. **Throughput on CPU**: YOLOv8n hits ~30 fps on a modern laptop
   without a GPU. The MJPEG live stream wants ~10 fps, so even live
   inference would have headroom — though I went with pre-computation
   anyway (see `pipeline/precompute_detections.py`) because that lets
   the stream stay smooth without an inference worker pool.
2. **Defensible**: every box in `pipeline/tracker.py` and
   `pipeline/detect.py` is hand-written. No magic.
3. **Honest baseline**: the problem says the reasoning is what scores,
   not the model. n is the obvious starting point and the only way the
   trade-off conversation in the interview becomes meaningful.

### What breaks at scale

Two things break before 40 stores at 1080p/15 fps:

1. **Single-process detection on CPU** can't keep up with 120
   simultaneous video streams (40 stores × 3 cameras). Becomes
   "GPU per store" or "GPU pool with a queue", probably Triton
   Inference Server or NVIDIA DeepStream.
2. **YOLOv8n's recall drops on partial occlusion** in the billing
   camera (CAM_4 produced only 1 detected frame out of 3,647 in my run
   — that clip is genuinely sparse but the few people present were
   still missed by n). I would A/B YOLOv8n vs YOLOv8s per camera and
   use the larger model only on billing where the accuracy matters
   most for queue-depth.

---

## Decision 2: Event schema design

### Why `zone_id` is null for ENTRY/EXIT/REENTRY (not empty string)

Empty string is a real value; null says "this attribute does not
apply here". An ENTRY happens at the threshold, not in any zone.
Conflating the two would mean every analytics query has to
special-case `zone_id = ''`. The Pydantic validator rejects ENTRY
with a non-null `zone_id` outright (`models.py::_check_event_type_constraints`),
so detectors can't drift.

### Why `confidence` is always emitted as a top-level field

Two reasons.

1. **The spec forbids silently dropping low-confidence detections**.
   Confidence calibration is one of the scored criteria, so the
   producer must report it honestly.
2. **Index efficiency**. `confidence` is a top-level REAL column with
   a CHECK constraint. SQLite can use `WHERE confidence > 0.6` against
   the `idx_events_store_ts` covering index — a range scan that
   touches only the rows we care about. Compare against putting
   confidence inside `metadata`: `WHERE json_extract(metadata,'$.confidence') > 0.6`
   has to **read every row, parse JSON, extract the field, then
   compare**. That's a full-table scan even with the index.

I checked this on my own DB after a pipeline run with ~9 k events:
the indexed range scan returned in 2 ms, the json_extract version
in 19 ms — a **9× difference at 9 k rows**. It would only get worse
as the events table grows.

### Why `session_seq` is in `metadata`, not top-level

`session_seq` is a debugging aid, not a query target. Top-level
fields are the things we filter and join on (`store_id`, `event_type`,
`timestamp`, `visitor_id`). `session_seq` exists so an engineer
reviewing an event log can say "this is the 7th event in the session"
without computing it. Pushing it into `metadata` keeps the index list
small and makes the schema additive: new metadata fields don't change
the table layout.

### Trade-off

The biggest trade-off is the JSON `metadata` column. We get
forward-compatibility for free — emit a new key, no migration — but
queries against `metadata` go through `json_extract`, which is slower
than a typed column (see the 9× number above). I accepted that because
the only metadata field used in hot queries is `queue_depth`, and even
that is read once per `/anomalies` request. If `metadata.<X>` ever
joins the hot path, it graduates to a top-level column with an index.

---

## Decision 3: Storage engine — SQLite (now) vs PostgreSQL (at scale)

### Options considered

| Option                          | Pros                                                  | Cons                                                       |
| ------------------------------- | ----------------------------------------------------- | ---------------------------------------------------------- |
| **SQLite + WAL**                | Single file, no daemon, ships in one container        | Single writer, no horizontal scaling                       |
| Postgres + PgBouncer            | Proper concurrency, replication, mature tooling       | Extra container, secrets management, harder local dev      |
| DuckDB                          | Columnar, fast for analytics                          | Weaker on the OLTP write path the ingest uses              |
| Time-series DB (InfluxDB)       | Optimised for the event shape                         | Wrong for the relational joins (sessions ↔ transactions)   |

### What AI suggested

Claude's recommendation was Postgres — "you need it once you're past a
single store". I disagreed for the 48-hour challenge scope and
documented the disagreement here, which is exactly what the spec asks
for in CHOICES.md.

### What I chose: SQLite in WAL mode, with a documented migration path

### Sizing this decision with actual numbers

**SQLite WAL throughput on commodity SSD**: ~1,000 inserts/sec
sustained, ~5,000 burst (measured on this laptop with `synchronous=NORMAL`).

**Our peak ingest rate**:
- Producer batches up to 500 events.
- One batch lands every ~30 s during real-time replay.
- 500 events ÷ 30 s = **~17 writes/sec**.
- That's **60× headroom** at 1,000 writes/sec.

**At 40 stores in production**:
- 40 stores × 500 events ÷ 30 s = **~667 writes/sec**.
- Still under the SQLite ceiling, but no margin. Anomaly detection
  spikes (rapid queue events) could push past it briefly.

**At read time**, `/metrics` and `/funnel` are 5–25 ms locally with
WAL: readers don't block the single writer. That's exactly what makes
SQLite sufficient for the demo.

### Migration path to Postgres

Moving to Postgres is a `psql -f schema.sql` away because the schema
already uses standard SQL with no SQLite-specific types (the json1
calls would map to Postgres `jsonb`). What changes:

1. Swap `sqlite3` for `psycopg2` in `app/db.py` (50-line module).
2. Add a `Postgres` service to `docker-compose.yml`.
3. Put **PgBouncer** between the API and Postgres for connection pooling
   — at 40 stores × N API workers, opening per-request connections
   becomes the bottleneck before the DB itself does.
4. Add a `metrics_daily` materialised view, refreshed every 5 min, so
   `/metrics` reads from a 40-row table instead of scanning today's
   events. Postgres has triggered materialised views; SQLite doesn't,
   which is another tipping-point.

### What would make me change this decision today

If any of the following happen:

- **Sustained > 600 writes/sec** for any single store (the SQLite
  margin disappears).
- **More than one writer process** is needed (e.g. dispatching ingest
  to multiple workers — SQLite only allows one writer at a time, even
  in WAL mode).
- **Cross-store reporting** with a dashboard that queries all 40 at
  once, where Postgres parallel query and partitioning would matter.

Until then, SQLite is honest engineering: it does the job with one
file and one container, and the migration is mapped out for the day
the assumptions break.

---

## Decision 4: Real-time delivery — SSE + pre-computed MJPEG, not WebSockets or live inference

### The two real-time surfaces

The dashboard has two independent real-time needs, and I made a
different call for each.

1. **Metric/event updates** (visitors, conversion, the live ticker).
2. **The camera feed** with YOLO boxes that follow people.

### Updates: Server-Sent Events, not WebSockets

| Option | Why / why not |
| --- | --- |
| **SSE** (chosen) | One-way server→client is exactly the shape of the need. Rides plain HTTP, passes through nginx with `proxy_buffering off`, auto-reconnects in the browser, and needs no upgrade handshake. One `asyncio.Queue` per subscriber, fan-out on ingest. |
| WebSockets | Bidirectional — we don't need client→server over the socket; the control plane (start/stop sim) is fine as plain POST. Extra handshake + message router for no benefit at this scale. |
| Polling only | Already used as the resilient fallback (3–5 s). But polling alone can't show the per-event ticker without hammering the API. SSE complements it. |

The browser keeps **both** a 5 s poll (authoritative numbers) and the
SSE stream (instant event ticker). If SSE drops, polling still keeps
the dashboard live — defence in depth.

### Camera feed: pre-compute detections once, replay as downscaled MJPEG

Live YOLO at 1080p on CPU is ~80–120 ms/frame — it would saturate the
API and starve every other request. So:

1. **`pipeline/precompute_detections.py`** runs YOLOv8n + an IoU
   tracker over each clip **once** and writes a frame-indexed JSON of
   `[x1,y1,x2,y2,conf,track_id]`. The boxes are real model output, not
   faked — they're just computed ahead of time.
2. The MJPEG endpoint decodes the mp4 and overlays those boxes,
   **interpolating** bbox positions between detection samples so a
   box recorded every 5th frame still slides smoothly.

Two numbers that drove the streaming tuning:

- **Downscale to 960 px width before encoding.** A 1080p frame's
  decode + draw + JPEG encode is ~40–50 ms on CPU; at 960 px it's
  ~15–20 ms and the JPEG is ~1/3 the size (~112 KB vs ~300 KB). bbox
  and zone-polygon coordinates are scaled by the same factor so
  overlays stay aligned.
- **Cap real-mode playback at 15 fps and skip source frames** so the
  clip plays at real-time speed instead of slow-motion. 30 fps was
  unachievable on CPU and caused the visible stutter; 15 fps is fluid
  and sustainable.

### Concurrency: async generators + a 128-token thread pool

Each MJPEG stream is an **async** generator; cv2 decode and PIL render
run via `asyncio.to_thread`, pacing via `asyncio.sleep`. A *sync*
`StreamingResponse` generator would reserve one anyio pool thread for
the entire life of the stream — open a few camera panels across two
stores and the default 40-thread pool starves the whole API. I raised
the limiter to 128 and moved the blocking work into threads, so dozens
of concurrent streams coexist with the polling + SSE traffic.

Thumbnails use a **single cached poster JPEG** per camera (not an
MJPEG socket each) so the browser's six-connections-per-host budget
isn't blown by the 4–5 thumbnail strip. Posters are pre-warmed at
startup, so the first dashboard load after a restart is instant.

### What breaks at scale

The SSE fan-out is in-process, so it doesn't survive multiple API
workers — at scale the subscriber registry moves to Redis pub/sub and
the camera feeds move to a dedicated media service (or WebRTC/HLS via
a streaming server), leaving the API to do only JSON.
