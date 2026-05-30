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
    csv_path.write_text(
        "order_id,order_date,order_time,store_id,store_name,total_amount\n"
        "100,bad-date,12:55:36,ST1008,Brigade_Bangalore,500\n"
        "200,10-04-2026,13:10:00,ST1008,Brigade_Bangalore,1200\n",
        encoding="utf-8",
    )
    rows = detect_and_read(csv_path)
    assert len(rows) == 1   # bad-date row is dropped
    assert rows[0][1] == "TXN_200"
