from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.db import connect  # noqa: E402

SAMPLE_DIR = ROOT / "samples"


@pytest.fixture()
def app(tmp_path):
    application = create_app(
        {
            "TESTING": True,
            "DB_PATH": str(tmp_path / "test_vibration.db"),
            "SAMPLE_DIR": str(SAMPLE_DIR),
        }
    )
    yield application


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def conn(app):
    connection = connect(app.config["DB_PATH"])
    yield connection
    connection.close()
