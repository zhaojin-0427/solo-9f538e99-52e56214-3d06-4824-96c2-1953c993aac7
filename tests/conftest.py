"""Shared pytest fixtures: isolated SQLite file per test and an HTTP client."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import db, main


@pytest.fixture()
def db_path(tmp_path) -> str:
    path = str(tmp_path / "test.db")
    db.configure_db(path)
    db.init_db()
    yield path
    db.configure_db(None)
    db.set_trace_hook(None)


@pytest.fixture()
def client(db_path) -> TestClient:
    with TestClient(main.app) as test_client:
        yield test_client
