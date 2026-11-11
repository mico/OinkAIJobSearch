"""Best-effort telemetry around the shared Codex transport.

The persisted ``claude_calls`` table and ``record_claude_call`` store method
retain their historical names for database compatibility. Runtime provider and
forensic operation labels are Codex. Token usage comes from Codex events;
subscription dollar cost is unknown, never inferred as an actual charge.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from codex_cli import (
    extract_assistant_text, pop_last_failure, resolve_model, run_p, run_p_with_tools,
)

if TYPE_CHECKING:
    from telemetry import MonitorStore

log = logging.getLogger(__name__)
_DEFAULT_STORE: "MonitorStore | None" = None


def _resolve_store(store: "MonitorStore | None") -> "MonitorStore | None":
    """Lazily capture calls without making telemetry load-bearing."""
    if store is not None:
        return store
    global _DEFAULT_STORE
    if _DEFAULT_STORE is not None:
        return _DEFAULT_STORE
    try:
        from db import DB
        from telemetry import MonitorStore as _MonitorStore
        project_root = Path(__file__).resolve().parents[4]
        state_dir = project_root / os.environ.get("STATE_DIR", "state")
        _DEFAULT_STORE = _MonitorStore(DB(state_dir / "jobs.db"))
        return _DEFAULT_STORE
    except Exception:
        log.exception("wrappers: failed to build default MonitorStore — telemetry off")
        return None


def _safe_result_chars(stdout: str | None) -> int:
    if stdout is None:
        return 0
    try:
        return len(extract_assistant_text(stdout) or "")
    except Exception:
        log.exception("wrappers: cannot parse assistant result")
        return 0


def _safe_usage_from_envelope(stdout: str | None) -> dict[str, Any]:
    """Extract measured usage; missing counters/costs remain NULL.

    Actual cost is accepted only when explicitly supplied by the envelope,
    preserving persisted historical-envelope support without fabricating a
    Codex subscription charge. Codex currently supplies no dollar cost.
    """
    out: dict[str, Any] = dict.fromkeys((
        "cost_actual_us", "input_tokens", "output_tokens", "cache_read_tokens",
        "cache_creation_tokens", "num_turns",
    ))
    if not stdout:
        return out
    try:
        envelope = json.loads(stdout)
        if not isinstance(envelope, dict):
            return out

        def opt_int(value: Any) -> int | None:
            if value is None or isinstance(value, bool):
                return None
            try:
                number = int(value)
                return number if number >= 0 else None
            except (TypeError, ValueError, OverflowError):
                return None

        cost = envelope.get("total_cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
            out["cost_actual_us"] = int(round(cost * 1_000_000))
        out["num_turns"] = opt_int(envelope.get("num_turns"))
        usage = envelope.get("usage")
        if isinstance(usage, dict):
            for source, target in (
                ("input_tokens", "input_tokens"),
                ("output_tokens", "output_tokens"),
                ("cache_read_input_tokens", "cache_read_tokens"),
                ("cache_creation_input_tokens", "cache_creation_tokens"),
            ):
                out[target] = opt_int(usage.get(source))
    except (TypeError, ValueError, OverflowError):
        return dict.fromkeys(out)
    return out


_FAILURE_REASON_STATUS = frozenset({
    "cli_absent", "timeout", "start_error", "nonzero_exit", "api_error",
})


def _infer_status(
    stdout: str | None, output_chars: int, result_chars: int,
    failure: dict[str, Any] | None = None,
) -> str:
    if stdout is None:
        reason = (failure or {}).get("reason")
        return reason if reason in _FAILURE_REASON_STATUS else "cli_missing"
    if output_chars > 0 and result_chars == 0:
        return "empty_result"
    return "ok"


def _forensic_log_call(
    *, op: str, caller: str, prompt: str, stdout: str | None, model: str | None,
    chat_id: int | None, pipeline_run_id: int | None, elapsed_ms: int,
    status: str, extra_kwargs: dict[str, Any],
) -> None:
    try:
        from forensic import log_step
        log_step(
            op,
            input={
                "caller": caller, "provider": "codex", "model": model,
                "prompt_chars": len(prompt or ""), "prompt_head": (prompt or "")[:1500],
                "kwargs": {
                    k: v for k, v in extra_kwargs.items()
                    if k in ("timeout_s", "allowed_tools", "disallowed_tools", "output_format", "model")
                },
            },
            output={
                "status": status, "output_chars": len(stdout or ""),
                "stdout_head": (stdout or "")[:1500] if stdout is not None else None,
            },
            chat_id=chat_id, run_id=pipeline_run_id, elapsed_ms=elapsed_ms,
        )
    except Exception:
        log.exception("wrappers: forensic log failed for caller=%s", caller)


def _wrapped_call(
    store: "MonitorStore | None", caller: str, prompt: str, *, tools: bool,
    pipeline_run_id: int | None, chat_id: int | None, model: str | None,
    **kwargs: Any,
) -> str | None:
    model = resolve_model(model)
    # Structured JSON is already requested by prompts; transport controls the
    # CLI event format independently of this application-level hint.
    kwargs.pop("json_mode", None)
    started_at = time.time()
    call = run_p_with_tools if tools else run_p
    stdout = call(prompt, model=model, **kwargs)
    finished_at = time.time()
    failure = pop_last_failure()
    elapsed_ms = int((finished_at - started_at) * 1000)
    output_chars = len(stdout or "")
    result_chars = _safe_result_chars(stdout)
    status = _infer_status(stdout, output_chars, result_chars, failure)
    exit_code = failure.get("exit_code") if stdout is None and failure else None
    usage = _safe_usage_from_envelope(stdout)
    if stdout:
        try:
            envelope = json.loads(stdout)
            if isinstance(envelope, dict) and isinstance(envelope.get("model"), str):
                model = envelope["model"]
        except (TypeError, ValueError):
            pass
    resolved = _resolve_store(store)
    if resolved is not None:
        try:
            resolved.record_claude_call(
                pipeline_run_id=pipeline_run_id, chat_id=chat_id, caller=caller,
                model=model, prompt_chars=len(prompt or ""), output_chars=output_chars,
                result_chars=result_chars, elapsed_ms=elapsed_ms, exit_code=exit_code,
                status=status, started_at=started_at, finished_at=finished_at, **usage,
            )
        except Exception:
            log.exception("wrappers: failed to record caller=%s", caller)
    _forensic_log_call(
        op="codex_cli.run_p_with_tools" if tools else "codex_cli.run_p",
        caller=caller, prompt=prompt, stdout=stdout, model=model, chat_id=chat_id,
        pipeline_run_id=pipeline_run_id, elapsed_ms=elapsed_ms, status=status,
        extra_kwargs=kwargs,
    )
    return stdout


def wrapped_run_p(
    store: "MonitorStore | None", caller: str, prompt: str, *,
    pipeline_run_id: int | None = None, chat_id: int | None = None,
    model: str | None = None, **kwargs: Any,
) -> str | None:
    """Time and record an offline Codex call, preserving str|None results."""
    return _wrapped_call(
        store, caller, prompt, tools=False, pipeline_run_id=pipeline_run_id,
        chat_id=chat_id, model=model, **kwargs,
    )


def wrapped_run_p_with_tools(
    store: "MonitorStore | None", caller: str, prompt: str, *,
    pipeline_run_id: int | None = None, chat_id: int | None = None,
    model: str | None = None, **kwargs: Any,
) -> str | None:
    """Time and record a native-web Codex call, with no provider fallback."""
    return _wrapped_call(
        store, caller, prompt, tools=True, pipeline_run_id=pipeline_run_id,
        chat_id=chat_id, model=model, **kwargs,
    )
