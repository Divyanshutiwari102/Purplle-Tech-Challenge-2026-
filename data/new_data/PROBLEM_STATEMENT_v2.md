# Purplle Tech Challenge 2026 — Problem Statement (v2, 2-June-2026)

> Saved verbatim from the PDF Purplle re-issued after the participant
> feedback round on 2 June 2026. The deadline was extended by **one
> day**. This file is for reference / diff against the v1 PDF — no
> code change is implied by saving it here.

## Field summary

| Field | Details |
| --- | --- |
| Format | Take-home — work independently within the window |
| Input | Raw anonymised CCTV footage |
| Output | Working containerised Store Intelligence API with live metrics |
| AI Policy | Fully open-book — all AI tools permitted and expected |
| Scoring | Automated correctness tests + contextual follow-up questions |
| Submission | Git repo link + DESIGN.md + CHOICES.md |

## Pipeline stages (unchanged)

```
Raw CCTV → Detection Layer → Event Stream → Intelligence API → Live Dashboard
```

| Stage | Responsibility | Constraints |
| --- | --- | --- |
| 1. Detection Layer | Process CCTV clips. Detect people. Track movement. Determine direction (entry vs exit). Assign per-session visitor token. | Use any model or library. Output must be structured events. |
| 2. Event Stream | Define event schema. Emit structured events from detection layer into ingest pipeline. | Schema must support the analytics queries in Stage 3. |
| 3. Intelligence API | Ingest events, compute real-time metrics, detect anomalies, expose queryable endpoints. | Must run via `docker compose up`. API must be production-aware. |
| 4. Live Dashboard | Show at least one metric updating in real time as events flow in. | Terminal output acceptable. Web UI scores higher. |

## Dataset (Section 3)

You receive a ZIP archive containing:

- **CCTV clips** — Entry camera, Main floor camera, Billing area camera
- **store_layout plan** — zone definitions for each store
- **pos_transactions.csv** — timestamped POS transaction records (store ID, amount, timestamp — no customer identity)
- **sample_events.jsonl** — example events in the expected output schema
- **assertions.py** — 10 example test assertions your API must pass (not the full scoring suite)

### v2 wording differences vs v1

- v1 explicitly listed clip counts: **"5 stores, 3 camera angles each, 20 minutes per clip (Entry/Main floor/Billing)"**.
- v2 lists the same camera roles but **drops the "5 stores × 20 minutes" wording**. This is just text cleanup; the dataset shape is the same.
- v2 makes the assertions.py mention again (consistent with v1).
- v2 says **"sample events.jsonl — example events"** without the "200 examples" exact count.

### Video clip specs (unchanged)

| Property | Detail |
| --- | --- |
| Duration | 20 minutes per clip per camera angle |
| Cameras | 3 angles: Entry/Exit threshold · Main floor · Billing counter |
| Resolution | 1080p, 15 fps |
| Anonymisation | Full-face blur, branding masked, no audio |

### Edge cases the footage exercises

`group entry`, `staff movement`, `re-entry`, `partial occlusion`,
`billing queue buildup`, `empty store periods`, `camera angle overlap`.

## POS Transactions (Section 3.4)

```
store_id, transaction_id, timestamp, basket_value_inr
STORE_BLR_002, TXN_00441, 2026-03-03T14:38:12Z, 1240.00
STORE_BLR_002, TXN_00442, 2026-03-03T14:41:55Z, 680.00
```

> **A visitor who was in the billing zone in the 5-minute window before
> a transaction timestamp counts as a converted visitor for that session.**

### v2: real sample CSV format

The actual CSV that ships in v2's resource center is **per line item**:

```
order_id,order_date,order_time,store_id,product_id,brand_name,total_amount
```

— different schema from the spec's example. Our `app/pos_loader.py`
already detects this and converts it (one row per `order_id`,
`total_amount` summed, IST → UTC).

## Scoring breakdown (Section 5.1, unchanged)

| Part | Dimension | Points |
| --- | --- | --- |
| A | Entry/exit count accuracy vs ground truth | 10 |
| A | Staff exclusion, re-entry, group handling | 10 |
| A | Schema compliance and event quality | 10 |
| B | API endpoint correctness (held-out event set) | 20 |
| B | Funnel accuracy and session deduplication | 10 |
| B | Anomaly detection correctness | 5 |
| C | Containerisation + README (acceptance gate) | 5 |
| C | Structured logs + health endpoint | 5 |
| C | Test coverage and edge case handling | 10 |
| D | AI usage depth | 15 |
| E | Live dashboard bonus | +10 |
| **Total** | (without bonus) | **100** |

## Acceptance gate (Section 5.2)

1. **Runs**: `docker compose up` starts the API. No manual steps beyond `git clone`.
2. **Produces events**: The README explains how to run the detection pipeline against the clips and where the output goes.
3. **Ingests**: `POST /events/ingest` accepts events without a 5xx response.
4. **Responds**: `GET /stores/STORE_BLR_002/metrics` returns valid JSON.
5. **Documents**: `DESIGN.md` and `CHOICES.md` both > 250 words.

## Submission checklist (Section 7.2)

- Git repo link (private — invite reviewer at `purplletechchallenge2026@hackerearth.com`)
- `docker compose up` confirmed working on a clean machine
- README explains how to run the detection pipeline
- DESIGN.md includes "AI-Assisted Decisions"
- CHOICES.md covers: model selection, schema design, one API decision
- Prompt blocks at top of each test file
- If doing Part E: local URL noted in README

### v2 differences

- **Reviewer email is now explicit**: `purplletechchallenge2026@hackerearth.com`. (v1 just said "invite reviewer handle provided in challenge email".)
- **Deadline extended by one day** (per the email accompanying v2).

## North Star (unchanged)

```
North Star Metric: Offline Store Conversion Rate
Conversion Rate = visitors who completed a purchase ÷ total unique visitors in a session window
```

Same business questions and endpoints map as v1.
