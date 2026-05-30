# CHOICES.md — three decisions, with reasoning

## Decision 1: Detection model

### Options considered

| Option       | Pros                                                              | Cons                                                       |
| ------------ | ----------------------------------------------------------------- | ---------------------------------------------------------- |
| YOLOv8n      | 6 MB, runs ~30 fps on CPU, person class is the COCO baseline      | Lower mAP than larger variants, struggles with occlusion   |
| YOLOv8s      | 22 MB, ~15 fps on CPU, noticeably better on partial occlusion     | 4× slower than n on the same hardware                      |
| RT-DETR-l    | State of the art mAP, strong on small objects                     | Needs GPU for real-time, larger weights, harder to package |
| MediaPipe    | Apache-licensed, ships with a holistic person model               | Single-person bias, weaker on group/crowd scenes           |
| YOLO-World   | Open-vocabulary; would let us prompt "person in store uniform"    | Heavier, prompts add latency, novel for production         |

### What AI suggested

Claude's recommendation, given the constraints (CPU, 1080p@15 fps,
five stores, 48-hour build window), was YOLOv8n with the comment that
YOLOv8s is the right upgrade path if accuracy on the billing-camera
clip is poor. It also flagged that re-detecting at 15 fps is wasteful —
running detection every 3rd frame plus the IoU tracker between detections
gets us most of the accuracy for a third of the compute.

### What I chose: YOLOv8n

I kept YOLOv8n as the default in `pipeline/detect.py` and made the
weights path a CLI flag so an operator can swap in YOLOv8s in one
command if accuracy on a specific clip needs the bump. I did not adopt
the every-3rd-frame suggestion in this submission because the test
clips are 20 minutes long and the IoU tracker hasn't been hardened
against the longer gap; documenting the option is honest about what's
left on the table.

### Why

- **Throughput on CPU.** The challenge is judged by event-stream
  quality, not detection latency, but the live dashboard demo (Part E)
  must keep up. YOLOv8n hits 30 fps on a modern CPU.
- **Footprint.** 6 MB of weights ship inside a slim container. There
  is no torch model download at runtime if we vendor the weights.
- **Honest baseline.** The problem statement says we are evaluated on
  reasoning, not on a specific model. n is the obvious starting point
  and the only way the trade-off conversation in the interview is
  meaningful.

### What breaks at scale

Two things break before 40 stores at 1080p/15 fps. First, single-process
detection on CPU can't keep up with 120 simultaneous video streams, so
this becomes "GPU per store" or "GPU pool with a queue". Second,
YOLOv8n's recall drops on partial occlusion in the billing-counter
clip; I would A/B test n vs s on a per-camera basis and use the larger
model only on the billing camera, where the accuracy matters most for
queue-depth.

---

## Decision 2: Event schema design

### Why `zone_id` is null for ENTRY/EXIT (not empty string)

Empty string is a real value; null says "this attribute does not apply
here". An ENTRY happens at the threshold, not in any zone; conflating
the two would mean every analytics query has to special-case
`zone_id = ''`. The Pydantic validator rejects ENTRY with a non-null
zone_id outright, so detectors can't drift.

### Why `confidence` is always emitted (never suppressed)

Two reasons. First, the problem statement explicitly forbids silently
dropping low-confidence detections — confidence calibration is one of
the scored criteria. Second, downstream we need the value to compute
data quality metrics and to weight aggregations: an `is_staff`
classifier with confidence 0.55 should not be treated the same as one
at 0.95. The only thing the API does with low-confidence events today
is keep them; tomorrow we can add a `confidence_floor` query parameter
without changing the producer.

### Why `session_seq` is in `metadata`, not top-level

`session_seq` is a debugging aid, not a query target. Top-level fields
are the things we filter and join on (`store_id`, `event_type`,
`timestamp`, `visitor_id`). `session_seq` exists so an engineer
reviewing an event log can see "this is the 7th event in the session"
without computing it. Pushing it into `metadata` keeps the index
list small and the schema additive: new metadata fields don't change
the table layout.

### Trade-off

The biggest trade-off is the JSON `metadata` column. We get
forward-compatibility for free — emit a new key, no migration — but
queries against `metadata` use `json_extract`, which is slower than a
typed column. We accepted that because the only metadata field used in
hot queries is `queue_depth`, and even that is read once per
`/anomalies` request. If `metadata.<X>` ever joins the hot path, it
graduates to a top-level column with an index.

---

## Decision 3: Re-ID approach (HOG/colour-histogram heuristic vs OSNet)

### Options considered

| Option                                    | Pros                                       | Cons                                              |
| ----------------------------------------- | ------------------------------------------ | ------------------------------------------------- |
| OSNet via torchreid                       | State-of-the-art mAP on Re-ID benchmarks   | +400 MB, GPU for real-time, complex install       |
| Bounding-box trajectory only              | Trivial to implement                       | Fails on stop-and-return cases                    |
| Colour histogram + region + time-gap rule | CPU-only, interpretable, ~50 LOC           | Collides under similar clothing, dim lighting     |
| Vendor SDK (e.g. Hikvision)               | Often built-in to the camera               | Closed source, vendor lock-in, no anonymised eval |

### What AI suggested

Claude's first answer was OSNet. When I pushed back on the
GPU/footprint cost it offered a fallback: a frozen MobileNet feature
extractor with cosine matching, which is lighter than OSNet but still
needs `torch` and ~15 MB of weights. Claude was reluctant to recommend
the colour-histogram approach at first because of the false-match
risk; it changed its mind when I framed the problem as "the cost of a
false match is one inflated visitor; the cost of unbuildable
infrastructure is zero events."

### What I chose: HOG/colour-histogram + entry region + 30-minute gap

The match function is `cosine_similarity(hist_a, hist_b) >= 0.75 AND
same_entry_camera AND time_gap <= 30 minutes`. The histogram is HSV
16×16 over the bounding-box crop, normalised. The hard constraints
(region + gap) carry most of the discrimination; the histogram is the
tie-breaker when two candidates match the constraints.

### Why

- **Defensible by hand.** Every line of `tracker.py` can be explained
  in the interview without invoking weights I didn't train.
- **Doesn't change the deployment surface.** No GPU, no extra
  container, no model download.
- **Composable.** If a real OSNet service shows up later, the
  `assign(...)` method is the only seam to swap.

### What would make me change this

If the held-out scoring shows that more than ~2% of returning visitors
are mis-classified as new (i.e. REENTRY recall is the bottleneck), I'd
add an OSNet sidecar service behind a gRPC call and use it only when
the histogram score is in the [0.6, 0.8] uncertainty band. Below 0.6
we're confident it's a different person; above 0.8 we're confident
it's the same; the OSNet call only matters in the middle. This caps
the latency and GPU cost.
