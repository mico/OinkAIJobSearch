"""Measured Codex usage, honest unknown costs, and persisted schema safety."""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db import DB
from telemetry import MonitorStore, migrate
from instrumentation import wrappers

_USAGE_COLUMNS = (
    "cost_actual_us", "input_tokens", "output_tokens", "cache_read_tokens",
    "cache_creation_tokens", "num_turns",
)


@pytest.fixture
def store(tmp_path):
    return MonitorStore(DB(tmp_path / "jobs.db"))


@pytest.fixture(autouse=True)
def clean_runtime(monkeypatch):
    monkeypatch.delenv("OINK_CODEX_MODEL", raising=False)
    monkeypatch.setattr(wrappers, "_forensic_log_call", lambda **kw: None)
    monkeypatch.setattr(wrappers, "pop_last_failure", lambda: None)


def row_for(store, caller):
    with store._db._conn() as conn:
        return conn.execute("SELECT * FROM claude_calls WHERE caller = ?", (caller,)).fetchone()


def test_migrate_backfills_legacy_table_without_losing_rows(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE claude_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT, pipeline_run_id INTEGER, chat_id INTEGER,
        caller TEXT NOT NULL, model TEXT, prompt_chars INTEGER NOT NULL DEFAULT 0,
        output_chars INTEGER NOT NULL DEFAULT 0, result_chars INTEGER NOT NULL DEFAULT 0,
        elapsed_ms INTEGER NOT NULL DEFAULT 0, exit_code INTEGER, status TEXT NOT NULL,
        cost_estimate_us INTEGER NOT NULL DEFAULT 0, started_at REAL NOT NULL,
        finished_at REAL NOT NULL)""")
    conn.execute("""INSERT INTO claude_calls
        (caller, status, cost_estimate_us, started_at, finished_at)
        VALUES ('historical', 'ok', 250, 1, 2)""")
    conn.commit()
    migrate(conn)
    migrate(conn)
    saved = conn.execute("SELECT * FROM claude_calls WHERE caller='historical'").fetchone()
    assert saved["cost_estimate_us"] == 250
    for name in _USAGE_COLUMNS:
        assert saved[name] is None
    conn.close()


@pytest.mark.parametrize("with_tools", [False, True])
def test_wrappers_store_measured_tokens_without_dollar_cost(store, monkeypatch, with_tools):
    envelope = json.dumps({
        "provider": "codex", "model": "gpt-5.4-mini", "result": '{"ok":true}',
        "usage": {"input_tokens": 1200, "output_tokens": 340, "cache_read_input_tokens": 980},
        "num_turns": 1,
    })
    seen = []
    def run(prompt, **kwargs):
        seen.append(kwargs)
        return envelope
    monkeypatch.setattr(wrappers, "run_p_with_tools" if with_tools else "run_p", run)
    monkeypatch.setenv("OINK_CODEX_MODEL", "gpt-5.4-mini")
    call = wrappers.wrapped_run_p_with_tools if with_tools else wrappers.wrapped_run_p
    assert call(store, "measured", "prompt") == envelope
    saved = row_for(store, "measured")
    assert saved["model"] == "gpt-5.4-mini" and saved["status"] == "ok"
    assert saved["input_tokens"] == 1200 and saved["output_tokens"] == 340
    assert saved["cache_read_tokens"] == 980 and saved["num_turns"] == 1
    assert saved["cache_creation_tokens"] is None
    assert saved["cost_actual_us"] is None
    assert saved["cost_estimate_us"] == 0
    assert seen[0]["model"] == "gpt-5.4-mini"


def test_reported_model_overrides_requested_model_for_telemetry(store, monkeypatch):
    monkeypatch.setattr(wrappers, "run_p", lambda *a, **kw:
                        json.dumps({"provider": "codex", "model": "gpt-5.4", "result": "ok"}))
    wrappers.wrapped_run_p(store, "model", "prompt", model="gpt-5.4-mini")
    assert row_for(store, "model")["model"] == "gpt-5.4"


@pytest.mark.parametrize("stdout,status", [
    ("plain text", "ok"), ('{"result":"hello"}', "ok"),
    ('{"result":""}', "empty_result"), (None, "cli_missing"),
])
def test_missing_usage_stays_null(store, monkeypatch, stdout, status):
    monkeypatch.setattr(wrappers, "run_p", lambda *a, **kw: stdout)
    assert wrappers.wrapped_run_p(store, "no_usage", "prompt") == stdout
    saved = row_for(store, "no_usage")
    assert saved["status"] == status
    for name in _USAGE_COLUMNS:
        assert saved[name] is None
    assert saved["cost_estimate_us"] == 0


def test_empty_result_keeps_measured_tokens(store, monkeypatch):
    monkeypatch.setattr(wrappers, "run_p", lambda *a, **kw: json.dumps({
        "provider": "codex", "result": "", "usage": {"input_tokens": 100, "output_tokens": 0},
        "num_turns": 1,
    }))
    wrappers.wrapped_run_p(store, "empty", "prompt")
    saved = row_for(store, "empty")
    assert saved["status"] == "empty_result" and saved["result_chars"] == 0
    assert saved["input_tokens"] == 100 and saved["output_tokens"] == 0
    assert saved["cost_actual_us"] is None


def test_structured_failure_status_and_exit_code(store, monkeypatch):
    monkeypatch.setattr(wrappers, "run_p", lambda *a, **kw: None)
    monkeypatch.setattr(wrappers, "pop_last_failure", lambda: {"reason": "api_error", "exit_code": 1})
    assert wrappers.wrapped_run_p(store, "failed", "prompt") is None
    saved = row_for(store, "failed")
    assert saved["status"] == "api_error" and saved["exit_code"] == 1


def test_non_numeric_usage_cannot_break_result(store, monkeypatch):
    envelope = json.dumps({"result": "ok", "usage": {
        "input_tokens": True, "output_tokens": "invalid", "cache_read_input_tokens": -5,
    }, "total_cost_usd": "not a charge", "num_turns": False})
    monkeypatch.setattr(wrappers, "run_p", lambda *a, **kw: envelope)
    assert wrappers.wrapped_run_p(store, "bad_usage", "prompt") == envelope
    for name in _USAGE_COLUMNS:
        assert row_for(store, "bad_usage")[name] is None


def test_historical_actual_cost_remains_readable(store):
    now = time.time()
    store.record_claude_call(
        caller="historical", prompt_chars=100, output_chars=30, elapsed_ms=10,
        status="ok", started_at=now, finished_at=now + .01,
        cost_actual_us=12300, input_tokens=1200, output_tokens=340,
    )
    saved = row_for(store, "historical")
    assert saved["cost_actual_us"] == 12300 and saved["input_tokens"] == 1200
    assert store.claude_call_window_summary(since_ts=now - 1)["cost_us"] == 12300
    assert store.claude_call_cost_by_caller(since_ts=now - 1)[0]["cost_us"] == 12300


def test_telemetry_failure_does_not_fail_underlying_call(monkeypatch):
    class BrokenStore:
        def record_claude_call(self, **kwargs):
            raise RuntimeError("telemetry unavailable")
    monkeypatch.setattr(wrappers, "run_p", lambda *a, **kw: '{"result":"ok"}')
    assert wrappers.wrapped_run_p(BrokenStore(), "best_effort", "prompt") == '{"result":"ok"}'
