# PROMPT: "Tests for app/pos_loader.py: detect simple vs Brigade-format
#          CSVs, parse correctly, dedupe via INSERT OR IGNORE."
#
# CHANGES MADE:
#   - Added a temp-file fixture so the test does not touch the
#     committed sample_pos_transactions.csv.
"""Tests for the POS CSV loader."""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import pytest

from app.pos_loader import detect_and_read, upsert_transactions


@pytest.fixture()
def db_with_schema(tmp_path):
    db = tmp_path / "pos.db"
    conn = sqlite3.connect(db)
    schema = (Path("app") / "schema.sql").read_text(encoding="utf-8")
    conn.executescript(schema)
    conn.close()
    return str(db)


def test_loader_reads_simple_format(tmp_path, db_with_schema):
    csv_path = tmp_path / "simple.csv"
    csv_path.write_text(
        "store_id,transaction_id,timestamp,basket_value_inr\n"
        "ST1008,TXN_1,2026-04-10T07:25:36Z,1240.00\n"
        "ST1008,TXN_2,2026-04-10T07:30:00Z,680.00\n",
        encoding="utf-8",
    )
    rows = detect_and_read(csv_path)
    assert len(rows) == 2
    assert rows[0][0] == "ST1008"
    assert rows[0][3] == 1240.00

    n = upsert_transactions(rows, db_with_schema)
    assert n == 2

    # Idempotent re-load
    n2 = upsert_transactions(rows, db_with_schema)
    assert n2 == 0


def test_loader_reads_brigade_format(tmp_path, db_with_schema):
    csv_path = tmp_path / "brigade.csv"
    csv_path.write_text(
        "order_id,order_date,order_time,store_id,store_name,total_amount\n"
        "100,10-04-2026,12:55:36,ST1008,Brigade_Bangalore,500\n"
        "100,10-04-2026,12:55:36,ST1008,Brigade_Bangalore,500\n"   # 2 lines, 1 order
        "200,10-04-2026,13:10:00,ST1008,Brigade_Bangalore,1200\n",
        encoding="utf-8",
    )
    rows = detect_and_read(csv_path)
    # 2 orders (100 with sum=1000, 200 with 1200)
    assert len(rows) == 2
    by_id = {r[1]: r for r in rows}
    assert by_id["TXN_100"][3] == 1000.0
    assert by_id["TXN_200"][3] == 1200.0
    # IST 12:55:36 → UTC 07:25:36
    assert by_id["TXN_100"][2] == "2026-04-10T07:25:36Z"


def test_loader_skips_malformed_rows(tmp_path, db_with_schema):
    csv_path = tmp_path / "bad.csv"
    # Use 3 rows for the same order so the per-cart heuristic still
    # picks the order_id-grouping branch (3 rows / 2 unique = 1.5).
    csv_path.write_text(
        "order_id,order_date,order_time,store_id,store_name,total_amount\n"
        "100,bad-date,12:55:36,ST1008,Brigade_Bangalore,500\n"
        "100,bad-date,12:55:36,ST1008,Brigade_Bangalore,500\n"
        "200,10-04-2026,13:10:00,ST1008,Brigade_Bangalore,1200\n",
        encoding="utf-8",
    )
    rows = detect_and_read(csv_path)
    assert len(rows) == 1   # bad-date cart is dropped
    assert rows[0][1] == "TXN_200"


def test_loader_groups_v2_lineitem_csv_by_time(tmp_path, db_with_schema):
    """v2 sample CSV uses per-line-item order_id (1, 2, 3 ...).

    The loader must recognise this shape and group line items into
    carts by (store_id, order_date, order_time) so 3 line items in
    the same cart collapse to 1 transaction.
    """
    csv_path = tmp_path / "v2.csv"
    csv_path.write_text(
        "order_id,order_date,order_time,store_id,product_id,brand_name,total_amount\n"
        # cart A: 3 line items at 12:15:05 → one txn, total 1247.98
        "1,10-04-2026,12:15:05,ST1008,P1,Faces Canada,302.33\n"
        "2,10-04-2026,12:15:05,ST1008,P2,Faces Canada,500.00\n"
        "3,10-04-2026,12:15:05,ST1008,P3,Faces Canada,445.65\n"
        # cart B: 1 line item at 13:42:18 → one txn, total 600.00
        "4,10-04-2026,13:42:18,ST1008,P4,Lakme,600.00\n",
        encoding="utf-8",
    )
    rows = detect_and_read(csv_path)
    assert len(rows) == 2, (
        f"expected 2 carts (3 lines + 1 line), got {len(rows)} — "
        "the per-line-item heuristic likely fired the wrong branch"
    )
    totals = sorted(round(r[3], 2) for r in rows)
    assert totals == [600.00, 1247.98]
    # Deterministic, time-derived txn_id (not the per-line order_id).
    for store_id, txn_id, _ts, _amt in rows:
        assert txn_id.startswith("TXN_")
        assert txn_id not in {"TXN_1", "TXN_2", "TXN_3", "TXN_4"}, (
            "txn_id reuses per-line order_id — grouping branch wrong"
        )


def test_loader_handles_real_v2_sample_file(db_with_schema):
    """End-to-end check against the actual v2 file shipped by Purplle.

    The file has 101 rows but only 24 unique (date, time) cart keys
    and total revenue ₹34,331.71. If either invariant breaks, the
    loader's auto-detection has regressed.
    """
    sample = Path("data") / "new_data" / "POS - sample transactions.csv"
    if not sample.exists():
        pytest.skip("v2 sample file not present in this checkout")
    rows = detect_and_read(sample)
    assert len(rows) == 24, f"expected 24 carts, got {len(rows)}"
    assert all(r[0] == "ST1008" for r in rows)
    total = round(sum(r[3] for r in rows), 2)
    assert total == 34331.71, f"revenue mismatch: ₹{total}"
