"""
Shared pytest fixtures.

Each test gets a fresh on-disk SQLite (tempfile) so suites can run in
parallel without stepping on each other.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Make the test DB path env var visible BEFORE importing the app.
@pytest.fixture()
def db_path(tmp_path) -> str:
    p = tmp_path / "store_intel.db"
    return str(p)


@pytest.fixture()
def client(db_path, monkeypatch):
    monkeypatch.setenv("STORE_INTEL_DB", db_path)

    # Re-import db so it picks up the env var, then init.
    import importlib
    from app import db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()

    # Reload modules that hold a reference to db.get_connection.
    from app import ingestion, metrics, anomalies, health, main
    importlib.reload(ingestion)
    importlib.reload(metrics)
    importlib.reload(anomalies)
    importlib.reload(health)
    importlib.reload(main)

    with TestClient(main.app) as c:
        yield c
