import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load():
    if "ball_plugin" in sys.modules:
        return sys.modules["ball_plugin"]
    spec = importlib.util.spec_from_file_location("ball_plugin", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ball_plugin"] = mod
    spec.loader.exec_module(mod)
    return mod


bp = _load()


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "ball.db"
    return bp.Store(lambda: sqlite3.connect(db, check_same_thread=False))


@pytest.fixture
def dbpath(tmp_path):
    return tmp_path / "ball.db"


@pytest.fixture(autouse=True)
def _isolated_state_db(tmp_path, monkeypatch):
    """Never let Ball's state.db fallback read the real ~/.hermes/state.db during tests."""
    p = tmp_path / "state.db"
    monkeypatch.setattr(bp.Ball, "_state_db_path", staticmethod(lambda: p))
    return p
