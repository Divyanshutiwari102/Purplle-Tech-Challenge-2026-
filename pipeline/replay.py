"""
Replay a directory of JSONL event files into the API.

Two modes:
  --rate 0   → as fast as possible (batches of 500)
  --rate N   → N events/sec, simulated real-time for the dashboard

Usage:
  python -m pipeline.replay --events-dir data/events --api http://localhost:8000
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Iterable, List

import urllib.request

BATCH = 500


def _post_batch(api: str, events: List[dict]) -> dict:
    req = urllib.request.Request(
        url=f"{api}/events/ingest",
        data=json.dumps({"events": events}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _iter_events(events_dir: Path) -> Iterable[dict]:
    for path in sorted(events_dir.glob("*.jsonl")):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events-dir", required=True)
    ap.add_argument("--api", required=True)
    ap.add_argument("--rate", type=float, default=0.0,
                    help="events/sec; 0 = max speed")
    args = ap.parse_args()

    batch: List[dict] = []
    total = ingested = duplicates = errors = 0
    period = 1.0 / args.rate if args.rate > 0 else 0.0

    for ev in _iter_events(Path(args.events_dir)):
        batch.append(ev)
        if period > 0:
            time.sleep(period)
            resp = _post_batch(args.api, batch)
            batch = []
        elif len(batch) >= BATCH:
            resp = _post_batch(args.api, batch)
            ingested += resp.get("ingested", 0)
            duplicates += resp.get("duplicates", 0)
            errors += len(resp.get("errors", []))
            batch = []
        total += 1

    if batch:
        resp = _post_batch(args.api, batch)
        ingested += resp.get("ingested", 0)
        duplicates += resp.get("duplicates", 0)
        errors += len(resp.get("errors", []))

    print(f"replayed={total} ingested={ingested} duplicates={duplicates} errors={errors}")


if __name__ == "__main__":
    main()
