"""
POS transaction loader.

Reads a POS CSV in any of three known shapes and normalises it into the
API's `transactions` table.

Supported shapes (auto-detected, in order):

  1. **Simple** — the schema this repo commits as `data/sample_pos_transactions.csv`:
        store_id, transaction_id, timestamp, basket_value_inr

  2. **Brigade raw** — the original 39-column real-store dump
     (line-item rows, but `order_id` is **the cart id**: 8-digit string,
     repeated across all line items in the same order).
     We group by `order_id`, sum `total_amount`, and form one txn per cart.

  3. **v2 sample** (the CSV Purplle re-released on 2-June-2026 in
     `data/new_data/POS - sample transactions.csv`). Same 7 columns as
     Brigade raw minus the PII columns, **but `order_id` is now per
     line item (1, 2, 3, ...) instead of per cart**. Naive grouping
     by `order_id` would inflate 24 real carts to 101 fake "txns".
     We detect this shape by `max(order_id) > unique(order_time)` and
     group by `(store_id, order_date, order_time)` instead, producing
     a deterministic `TXN_<sha1[:10]>` id per cart.

Why a separate loader and not an HTTP endpoint?
  - POS files arrive as nightly batches in real production.
  - Loading from disk by an operator is the right shape; the API does
    not need to expose a CSV upload surface to be evaluated.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Tuple

IST = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------------
# Shape 1: simple schema
# ---------------------------------------------------------------------
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


# ---------------------------------------------------------------------
# Helpers shared between the two line-item shapes
# ---------------------------------------------------------------------
def _ist_to_utc_iso(date_dmy: str, time_hms: str) -> str:
    """`10-04-2026` + `12:55:36` (IST) → `2026-04-10T07:25:36Z`."""
    dt_ist = datetime.strptime(
        f"{date_dmy} {time_hms}", "%d-%m-%Y %H:%M:%S"
    ).replace(tzinfo=IST)
    return dt_ist.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _txn_id_from_cart_key(store_id: str, iso_ts: str) -> str:
    """Stable, deterministic id for a (store, cart-time) cart."""
    h = hashlib.sha1(f"{store_id}|{iso_ts}".encode()).hexdigest()[:10]
    return f"TXN_{h}"


# ---------------------------------------------------------------------
# Shape 2 + 3 dispatcher
# ---------------------------------------------------------------------
def _read_lineitem_csv(path: Path) -> List[Tuple[str, str, str, float]]:
    """
    Group line items into carts.

    Detection: an `order_id` is "per cart" if the same value repeats
    across multiple rows (the Brigade raw shape — order_id 104363838
    appears on every line of the same cart). It is "per line item" if
    the values are unique (the v2 sample shape — order_id 1, 2, 3, ...).

    We measure this with one pass: if `len(rows) > 1.5 × unique(order_id)`
    the order_id is reused → group by order_id (Brigade raw). Otherwise
    we group by `(store_id, order_date, order_time)` so each cart's
    line items collapse together.
    """
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        cols = set(reader.fieldnames or [])
        if not {"order_id", "order_date", "order_time", "store_id",
                "total_amount"}.issubset(cols):
            raise ValueError(
                "not a line-item POS CSV (missing required columns "
                "order_id/order_date/order_time/store_id/total_amount)"
            )
        rows = list(reader)

    if not rows:
        return []

    distinct_order_ids = {r["order_id"].strip() for r in rows
                          if r.get("order_id")}
    cart_per_order_id = (
        len(rows) > 0
        and len(distinct_order_ids) > 0
        and len(rows) >= 1.5 * len(distinct_order_ids)
    )

    # The grouping key is what defines a cart.
    def _cart_key(r: Dict[str, str]) -> Tuple[str, str, str, str]:
        if cart_per_order_id:
            return ("order_id",
                    r.get("order_id", "").strip(),
                    r.get("store_id", "").strip(),
                    "")
        return ("time",
                r.get("store_id", "").strip(),
                r.get("order_date", "").strip(),
                r.get("order_time", "").strip())

    carts: Dict[Tuple[str, str, str, str], dict] = {}
    for r in rows:
        oid = (r.get("order_id") or "").strip()
        if not oid:
            continue
        key = _cart_key(r)
        c = carts.setdefault(key, {
            "store_id": (r.get("store_id") or "").strip(),
            "order_date": (r.get("order_date") or "").strip(),
            "order_time": (r.get("order_time") or "").strip(),
            "first_order_id": oid,    # only used when grouping by order_id
            "total": 0.0,
            "line_count": 0,
        })
        try:
            c["total"] += float(r.get("total_amount") or 0)
        except ValueError:
            continue
        c["line_count"] += 1

    out: List[Tuple[str, str, str, float]] = []
    for key, c in carts.items():
        if not c["order_date"] or not c["order_time"]:
            continue
        try:
            iso_ts = _ist_to_utc_iso(c["order_date"], c["order_time"])
        except ValueError:
            continue
        if cart_per_order_id:
            txn_id = f"TXN_{c['first_order_id']}"
        else:
            txn_id = _txn_id_from_cart_key(c["store_id"], iso_ts)
        out.append((c["store_id"], txn_id, iso_ts, round(c["total"], 2)))

    # Sort by timestamp for determinism + readable diffs.
    out.sort(key=lambda r: (r[2], r[1]))
    return out


def detect_and_read(path: Path) -> List[Tuple[str, str, str, float]]:
    """Try the simple schema first, then fall back to the line-item formats."""
    try:
        return _read_simple_csv(path)
    except KeyError:
        return _read_lineitem_csv(path)


# Kept for backward compatibility — `_read_brigade_csv` was the public
# helper before v2's per-line-item order_id required us to generalise.
_read_brigade_csv = _read_lineitem_csv


# ---------------------------------------------------------------------
# DB upsert
# ---------------------------------------------------------------------
def upsert_transactions(rows: List[Tuple[str, str, str, float]],
                        db_path: str) -> int:
    if not rows:
        return 0
    conn = sqlite3.connect(db_path)
    try:
        # Make sure the foreign-key target exists (the simulation
        # manager does the same — see app/simulation.py).
        for (store_id, _txn_id, _ts, _amt) in rows:
            conn.execute(
                "INSERT OR IGNORE INTO stores "
                "(store_id, name, timezone, open_hours) VALUES (?, ?, ?, ?)",
                (store_id, store_id, "Asia/Kolkata", '{"mon":["10:00","21:00"]}'),
            )
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


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------
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
