"""Codex-only transport: isolation, native web, JSONL, auth and failures."""
from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_cli as cli
from instrumentation import wrappers


def stream(text='{"ok":true}', *, usage=None, commentary=None):
    events = [{"type": "thread.started", "thread_id": "thread-test"}]
    if commentary is not None:
        events.append({"type": "item.completed", "item": {
            "type": "agent_message", "text": commentary,
        }})
    events.extend([
        {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
        {"type": "turn.completed", "usage": usage if usage is not None else {
            "input_tokens": 1200, "cached_input_tokens": 980, "output_tokens": 340,
        }},
    ])
    return "\n".join(json.dumps(e) for e in events)


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.delenv("OINK_CODEX_MODEL", raising=False)
    monkeypatch.delenv("OINK_CODEX_BIN", raising=False)
    cli.pop_last_failure()
    yield
    cli.pop_last_failure()


@pytest.fixture
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/codex")

    def run(cmd, **kwargs):
        seen.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout=stream(), stderr="")

    monkeypatch.setattr(cli.subprocess, "run", run)
    return seen


def settings(cmd):
    return [cmd[i + 1] for i, value in enumerate(cmd[:-1]) if value == "-c"]


def test_plain_isolated_subscription_call(calls, monkeypatch):
    secret = "CV private text"
    for name in ("CODEX_API_KEY", "OPENAI_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_BASE_URL"):
        monkeypatch.setenv(name, "must-not-reach-child")
    monkeypatch.setenv("OINK_SENTINEL", "keep")
    result = json.loads(cli.run_p(secret))
    cmd, kwargs = calls[0]
    assert secret not in cmd and kwargs["input"] == secret
    assert cmd[0] == "/usr/bin/codex" and cmd[-1] == "-"
    assert "exec" in cmd and "--json" in cmd and "--no-daemon" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert cmd[cmd.index("--ask-for-approval") + 1] == "never"
    assert {"--ignore-user-config", "--ignore-rules", "--ephemeral"}.issubset(cmd)
    assert 'forced_login_method="chatgpt"' in settings(cmd)
    assert 'model_provider="openai"' in settings(cmd)
    assert 'web_search="disabled"' in settings(cmd)
    assert "mcp_servers={}" in settings(cmd)
    assert "orchestrator.mcp.enabled=false" in settings(cmd)
    for feature in cli._DISABLED_FEATURES:
        assert any(cmd[i:i + 2] == ["--disable", feature] for i in range(len(cmd) - 1))
    # Native web dispatch requires the host even when model-authored code is
    # disabled; disabling the host breaks web open/search, not just scripts.
    assert "code_mode_host" not in cli._DISABLED_FEATURES
    assert {"code_mode", "code_mode_only", "shell_tool", "unified_exec"}.issubset(cli._DISABLED_FEATURES)
    assert not Path(kwargs["cwd"]).exists()  # temporary directory removed after exit
    assert "oink-codex-" in kwargs["cwd"]
    assert not any(name in kwargs["env"] for name in cli._SHADOWING_AUTH_VARS)
    assert kwargs["env"]["OINK_SENTINEL"] == "keep"
    assert result["provider"] == "codex" and result["model"] is None
    assert result["usage"] == {
        "input_tokens": 1200, "cache_read_input_tokens": 980, "output_tokens": 340,
    }
    assert "total_cost_usd" not in result and result["num_turns"] == 1


def test_web_native_live_search_and_open(calls):
    assert cli.run_p_with_tools(
        "find jobs", allowed_tools=cli.TOOLS_WEB_BOTH,
        disallowed_tools=cli.TOOLS_DENY_SHELL_FS, cwd="/private/project",
    ) is not None
    cmd, kwargs = calls[0]
    assert 'web_search="live"' in settings(cmd)
    assert kwargs["cwd"] != "/private/project"
    assert "--ignore-user-config" in cmd


def test_fetch_only_uses_native_open_instruction(calls):
    cli.run_p_with_tools("https://example.org/jobs", allowed_tools=cli.TOOLS_WEB_FETCH_ONLY)
    cmd, kwargs = calls[0]
    assert 'web_search="live"' in settings(cmd)
    assert "Do not run search queries" in kwargs["input"]


def test_web_denied_and_no_security_override(calls):
    cli.run_p_with_tools("offline", allowed_tools=cli.TOOLS_WEB_BOTH,
                         disallowed_tools=cli.TOOLS_DENY_WEB_AND_SHELL_FS)
    assert 'web_search="disabled"' in settings(calls[0][0])
    assert cli.run_p_with_tools("unsafe", extra_args=["--dangerously-bypass-approvals-and-sandbox"]) is None
    assert cli.pop_last_failure()["reason"] == "start_error"
    assert cli.run_p_with_tools("unsafe", allowed_tools="Bash") is None
    assert len(calls) == 1


def test_models_are_dynamic_and_unsupported_aliases_rejected(calls, monkeypatch):
    monkeypatch.setenv("OINK_CODEX_MODEL", "gpt-5.4-mini")
    assert json.loads(cli.run_p("x"))["model"] == "gpt-5.4-mini"
    assert calls[0][0][calls[0][0].index("--model") + 1] == "gpt-5.4-mini"
    assert json.loads(cli.run_p("x", model="gpt-5.4"))["model"] == "gpt-5.4"
    assert cli.run_p("x", model="haiku") is None
    assert cli.pop_last_failure()["reason"] == "start_error"
    assert len(calls) == 2


def test_configured_binary(monkeypatch):
    monkeypatch.setenv("OINK_CODEX_BIN", "/custom/bin/codex")
    monkeypatch.setattr(cli.shutil, "which", lambda name: name)
    assert cli.find_codex() == "/custom/bin/codex"


@pytest.mark.parametrize("output,rc,active", [
    ("Logged in using ChatGPT", 0, True),
    ("Logged in using an API key", 0, False),
    ("Logged in using ChatGPT", 1, False),
])
def test_login_requires_chatgpt(monkeypatch, output, rc, active):
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=rc, stdout="", stderr=output))
    assert cli.chatgpt_login_active("/bin/codex") is active


def test_final_message_only_and_text_mode(calls, monkeypatch):
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout=stream(commentary="Searching now"), stderr=""))
    assert cli.run_p("x", output_format="text") == '{"ok":true}'
    assert cli.extract_assistant_text(cli.run_p("x")) == '{"ok":true}'


def test_empty_message_keeps_usage(calls, monkeypatch):
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout=stream(""), stderr=""))
    out = cli.run_p("x")
    assert cli.extract_assistant_text(out) == ""
    assert wrappers._infer_status(out, len(out), 0) == "empty_result"
    assert json.loads(out)["usage"]["input_tokens"] == 1200


@pytest.mark.parametrize("events,rc,reason", [
    ([{"type": "turn.failed", "error": {"message": "quota reached", "code": "quota"}}], 1, "api_error"),
    ([{"type": "turn.failed", "error": {"message": "unauthorized", "code": "401"}}], 0, "api_error"),
    ([{"type": "error", "message": "network error"}], 1, "api_error"),
    ([], 1, "nonzero_exit"),
    ([], 0, "nonzero_exit"),
])
def test_terminal_failures(calls, monkeypatch, events, rc, reason):
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=rc, stdout="\n".join(map(json.dumps, events)), stderr=""))
    assert cli.run_p("private") is None
    failure = cli.pop_last_failure()
    assert failure["reason"] == reason and failure["exit_code"] == rc
    assert "private" not in failure["stdout_head"]
    assert wrappers._infer_status(None, 0, 0, failure) == reason
    assert cli.pop_last_failure() is None


def test_intermediate_retry_error_does_not_fail_success(calls, monkeypatch):
    output = json.dumps({"type": "error", "message": "retrying"}) + "\n" + stream()
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout=output, stderr=""))
    assert cli.run_p("x") is not None
    assert cli.pop_last_failure() is None


@pytest.mark.parametrize("exception,reason", [
    (subprocess.TimeoutExpired("codex", 1), "timeout"), (OSError("spawn failed"), "start_error"),
])
def test_spawn_failure(calls, monkeypatch, exception, reason):
    def fail(*a, **kw):
        raise exception
    monkeypatch.setattr(cli.subprocess, "run", fail)
    assert cli.run_p("x") is None
    assert cli.pop_last_failure()["reason"] == reason


def test_absent_and_success_clear_failure(calls, monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.run_p("x") is None
    assert cli.pop_last_failure()["reason"] == "cli_absent"
    cli._set_failure("timeout")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/bin/codex")
    assert cli.run_p("x") is not None
    assert cli.pop_last_failure() is None


def test_failures_are_thread_local():
    cli._set_failure("timeout")
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(cli.pop_last_failure).result() is None
    assert cli.pop_last_failure()["reason"] == "timeout"


@pytest.mark.parametrize("message,code,category", [
    ("Invalid authentication", "401", "auth"),
    ("You have hit your usage limit", "quota_exceeded", "quota"),
    ("Overloaded", "429", "transient"),
])
def test_prewarm_classifies_errors(calls, monkeypatch, message, code, category):
    event = {"type": "turn.failed", "error": {"message": message, "code": code}}
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=1, stdout=json.dumps(event), stderr=""))
    assert cli.prewarm_token()[0] == category


@pytest.mark.parametrize("text,expected", [
    ('```json\n{"ok":true}\n```', {"ok": True}),
    ('Answer: [{"ok":true}] trailing', [{"ok": True}]),
    ('first {"x":"}"} then {"y":2}', {"x": "}"}),
    ("not JSON", None), ("", None),
])
def test_parse_json_block(text, expected):
    assert cli.parse_json_block(text) == expected


def test_extract_literal_empty_and_plain_text():
    assert cli.extract_assistant_text('{"result":""}') == ""
    assert cli.extract_assistant_text("plain") == "plain"
    assert cli.extract_assistant_text('{"content":"fallback"}') == "fallback"
