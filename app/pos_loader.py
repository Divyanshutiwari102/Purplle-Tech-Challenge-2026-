"""
POS transaction loader.

Reads a CSV in either the simple `store_id,transaction_id,timestamp,
basket_value_inr` schema (the format committed in this repo) or the
richer real-store CSV (per-line-item, IST-localised). It normalises
both into the `transactions` table.

Why a separate loader and not an HTTP endpoint?
  - POS files arrive as nightly batches in real production.
  - Loading from disk by an operator is the right shape; the API does
    not need to expose a CSV upload surface to be evaluated.
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Tuple

IST = timezone(timedelta(hours=5, minutes=30))


def _read_simple_csv(path: Path) -> List[Tuple[str, str, str, float]]:
    rows: List[Tuple[str, str, str, float]] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            rows.append((
                r["store_id"].strip(),
                r["transaction_id"].strip(),
                r["timestamp"].strip(),
                float(r["basket_value_inr"]),
            ))
    return rows


def _read_brigade_csv(path: Path) -> List[Tuple[str, str, str, float]]:
    """
    Convert a Brigade-format POS CSV (one row per line item, IST times,
    `total_amount` per line) to the API's transaction schema.

    Steps:
      1. Group by order_id, sum total_amount.
      2. Combine order_date (DD-MM-YYYY) + order_time (HH:MM:SS) as IST.
      3. Convert to UTC ISO-8601.
    """
    orders: Dict[str, dict] = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        cols = set(reader.fieldnames or [])
        if "order_id" not in cols or "total_amount" not in cols:
            raise ValueError("not a Brigade-format CSV (missing order_id/total_amount)")
        for r in reader:
            oid = (r.get("order_id") or "").strip()
            if not oid:
                continue
            o = orders.setdefault(oid, {
                "store_id": (r.get("store_id") or "").strip(),
                "order_date": (r.get("order_date") or "").strip(),
                "order_time": (r.get("order_time") or "").strip(),
                "total": 0.0,
            })
            try:
                o["total"] += float(r.get("total_amount") or 0)
            except ValueError:
                continue

    out: List[Tuple[str, str, str, float]] = []
    for oid, o in orders.items():
        if not o["order_date"] or not o["order_time"]:
            continue
        try:
            dt_ist = datetime.strptime(
                f"{o['order_date']} {o['order_time']}",
                "%d-%m-%Y %H:%M:%S",
            ).replace(tzinfo=IST)
        except ValueError:
            continue
        ts = dt_ist.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        out.append((o["store_id"], f"TXN_{oid}", ts, round(o["total"], 2)))
    return out


def detect_and_read(path: Path) -> List[Tuple[str, str, str, float]]:
    """Try the simple schema first, then fall back to the Brigade format."""
    try:
        return _read_simple_csv(path)
    except KeyError:
        return _read_brigade_csv(path)


def upsert_transactions(rows: List[Tuple[str, str, str, float]],
                        db_path: str) -> int:
    if not rows:
        return 0
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.executemany(
            "INSERT OR IGNORE INTO transactions "
            "(transaction_id, store_id, timestamp, basket_value_inr) "
            "VALUES (?, ?, ?, ?)",
            [(r[1], r[0], r[2], r[3]) for r in rows],
        )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


def _cli() -> None:
    ap = argparse.ArgumentParser(description="Load POS transactions into the DB.")
    ap.add_argument("--csv", required=True, help="Path to POS CSV")
    ap.add_argument("--db", required=True, help="Path to SQLite DB")
    args = ap.parse_args()

    p = Path(args.csv)
    if not p.exists():
        print(f"file not found: {p}", file=sys.stderr)
        sys.exit(2)

    rows = detect_and_read(p)
    inserted = upsert_transactions(rows, args.db)
    print(f"read={len(rows)}  inserted={inserted}")


if __name__ == "__main__":
    _cli()
