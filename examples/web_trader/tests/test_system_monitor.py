"""Unit tests for system_monitor helpers (no live Redis/QuestDB required)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from system_monitor import SERVICE_NAMES, _parse_heartbeat, _rows


def test_service_names() -> None:
    assert SERVICE_NAMES == ("md_receiver", "recorder", "web")


def test_parse_heartbeat_ok() -> None:
    import json
    import time

    raw = json.dumps({"service": "web", "ts": time.time(), "pid": 1})
    hb = _parse_heartbeat(raw)
    assert hb is not None
    assert hb["ok"] is True
    assert hb["age_sec"] is not None
    assert hb["age_sec"] < 5


def test_parse_heartbeat_stale() -> None:
    import json
    import time

    raw = json.dumps({"service": "web", "ts": time.time() - 60, "pid": 1})
    hb = _parse_heartbeat(raw)
    assert hb is not None
    assert hb["ok"] is False


def test_parse_heartbeat_invalid() -> None:
    assert _parse_heartbeat(None) is None
    assert _parse_heartbeat("not-json") is None


def test_rows_mapping() -> None:
    result = {
        "columns": [{"name": "tableName"}, {"name": "diskSize"}, {"name": "rowCount"}],
        "dataset": [["dbtickdata", 1024, 10], ["dbbardata", 2048, 20]],
    }
    rows = _rows(result)
    assert rows[0]["tableName"] == "dbtickdata"
    assert rows[1]["rowCount"] == 20
