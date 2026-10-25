import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seatrail import config, db, demo  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    db.reset(tmp_path / "test.db")
    users = demo.seed_base()
    yield users
    db.reset()


@pytest.fixture()
def client(env):
    from fastapi.testclient import TestClient
    from seatrail.app import app
    return TestClient(app)


def as_user(u):
    return {"X-User-Id": str(u["id"])}
