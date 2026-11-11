#!/usr/bin/env python3
"""Behavioral coverage for rescore deadlines and single-job retry recovery.

Timed-out rescore batches retry once at batch_size=1; repeated failures drop
the affected jobs rather than silently reverting to primary-only verdicts.
Primary and rescore passes retain independent timeout budgets. Fast transport
failures continue to use the separate split-retry recovery path.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


def _assert(cond: bool, msg: str) -> None:
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        raise AssertionError(msg)


def section(label: str) -> None:
    print(f"\n=== {label} ===")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@dataclass
class _FakeJob:
    """Minimal stand-in for dedupe.Job — only the attributes job_enrich reads."""
    external_id: str
    title: str = "Senior MLOps Engineer"
    company: str = "Acme Corp"
    location: str = "Remote · EU"
    salary: str = ""
    url: str = "https://example.com/job"
    snippet: str = "We need MLOps. Python, AWS, Docker, MLflow."

    @property
    def job_id(self) -> str:
        return f"jid-{self.external_id}"

    @property
    def source(self) -> str:
        return "smoke"


def _make_jobs(n: int) -> list[_FakeJob]:
    return [_FakeJob(external_id=f"ext-{i:04d}", url=f"https://example.com/job/ext-{i:04d}") for i in range(n)]


def _envelope_with_results(items: list[dict]) -> str:
    envelope = {
        "type": "result", "subtype": "success",
        "is_error": False,
        "result": json.dumps({"results": items}),
    }
    return json.dumps(envelope)


def _primary_response_for(present, score_for) -> str:
    items = []
    for j in present:
        items.append({
            "id": j.external_id,
            "match_score": int(score_for(j.external_id)),
            "why_match": "ok",
            "why_mismatch": "",
            "key_details": {
                "stack": "python", "seniority": "senior",
                "remote_policy": "remote", "location": "",
                "salary": "", "visa_support": "",
                "language": "", "standout": "",
            },
        })
    return _envelope_with_results(items)


def _rescore_response_for(present) -> str:
    items = []
    for j in present:
        items.append({
            "id": j.external_id,
            "match_score": 4,
            "why_match": "rescore-stamp",
            "why_mismatch": "",
            "key_details": {
                "stack": "python", "seniority": "senior",
                "remote_policy": "remote", "location": "",
                "salary": "", "visa_support": "",
                "language": "", "standout": "",
            },
        })
    return _envelope_with_results(items)


def _read_forensic_lines(state_dir: str) -> list[dict]:
    log_dir = Path(state_dir) / "forensic_logs"
    out: list[dict] = []
    if not log_dir.is_dir():
        return out
    for p in sorted(log_dir.glob("log.*.jsonl")):
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def test_rescore_timeout_triggers_batch_one_retry() -> None:
    section(
        "1. rescore batch times out → retried at batch_size=1; recovered "
        "verdicts carry the rescore stamp"
    )

    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td
    os.environ.pop("FORENSIC_OFF", None)

    for mod in ("forensic", "job_enrich"):
        if mod in sys.modules:
            del sys.modules[mod]
    import job_enrich

    # 10 jobs, all in the rescore window (all mid-band → primary scores 3).
    # With rescore_max_jobs_per_call=5 that's 2 rescore batches.
    # The FIRST rescore batch (5 jobs) will time out on its first attempt;
    # the batch=1 retry path should re-issue 5 single-job calls and
    # those succeed instantly.
    jobs = _make_jobs(10)
    first_rescore_batch_ids = {j.external_id for j in jobs[0:5]}

    rescore_call_log: list[int] = []  # batch sizes seen by rescore calls

    # Use a SHORT timeout so the test actually completes in seconds.
    test_rescore_timeout = 1  # 1s — easy to overshoot in a fake

    def fake_wrapped_run_p(store, caller, prompt, *, timeout_s=None, **kwargs):
        present = [j for j in jobs if j.external_id in prompt]
        if "rescore" in caller:
            rescore_call_log.append(len(present))
            # Time-out behaviour: the FULL 5-job batch sleeps past
            # timeout_s and returns None (mirrors subprocess.TimeoutExpired
            # → codex_cli.run_p returning None). Single-job calls (size=1)
            # succeed immediately.
            is_full_first_batch = (
                len(present) == 5
                and {j.external_id for j in present} == first_rescore_batch_ids
            )
            if is_full_first_batch:
                # Sleep slightly longer than the timeout so the wrapper's
                # elapsed measurement crosses the _TIMEOUT_ELAPSED_THRESHOLD
                # (0.9 * timeout_s). 1.05 * timeout_s is enough margin.
                time.sleep((timeout_s or test_rescore_timeout) * 1.05)
                return None
            return _rescore_response_for(present)
        return _primary_response_for(present, lambda eid: 3)  # all mid

    job_enrich.wrapped_run_p = fake_wrapped_run_p

    verdicts = job_enrich.enrich_jobs_ai(
        jobs=jobs,
        resume_text="MLOps engineer.",
        prefs_text="MLOps engineer prefs",
        timeout_s=10,             # primary budget (generous)
        rescore_timeout_s=test_rescore_timeout,
        max_jobs_per_call=10,
        two_pass=True,
        triage_floor=2,
        triage_ceiling=5,
        rescore_max_jobs_per_call=5,
        workers=1,                # deterministic order
    )

    # All 10 jobs verdicted (5 via successful 2nd-batch rescore + 5 via
    # batch=1 retry).
    _assert(
        len(verdicts) == 10,
        f"all 10 verdicts present (got {len(verdicts)})",
    )
    # The originally-timed-out batch members must carry the rescore
    # stamp (=4) — NOT the primary verdict (=3). This pins that the retry
    # actually re-issued rescore calls; it did not fall back to primary.
    for jid in first_rescore_batch_ids:
        _assert(
            verdicts.get(jid, {}).get("match_score") == 4,
            f"timeout-recovered job {jid!r} carries rescore stamp "
            f"(got {verdicts.get(jid, {}).get('match_score')!r}); "
            f"if 3, this means we silently fell back to primary — "
            f"verdict provenance would be corrupted",
        )

    # rescore call shape: 1 timed-out full-batch (size=5) + 5 single-job
    # retry calls (size=1 each) + 1 second full-batch success (size=5)
    # = 7 calls. Sorted: [1, 1, 1, 1, 1, 5, 5].
    _assert(
        sorted(rescore_call_log) == [1, 1, 1, 1, 1, 5, 5],
        f"rescore call shape after timeout retry "
        f"(got {sorted(rescore_call_log)})",
    )


def test_primary_uses_separate_timeout() -> None:
    section(
        "2. primary triage uses the generous timeout_s, NOT the rescore cap "
        "— primary batches that take longer than rescore_timeout_s still pass"
    )

    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td

    for mod in ("forensic", "job_enrich"):
        if mod in sys.modules:
            del sys.modules[mod]
    import job_enrich

    jobs = _make_jobs(5)

    primary_call_timeouts: list[int] = []
    rescore_call_timeouts: list[int] = []

    def fake_wrapped_run_p(store, caller, prompt, *, timeout_s=None, **kwargs):
        present = [j for j in jobs if j.external_id in prompt]
        if "rescore" in caller:
            rescore_call_timeouts.append(timeout_s)
            return _rescore_response_for(present)
        primary_call_timeouts.append(timeout_s)
        return _primary_response_for(present, lambda eid: 3)

    job_enrich.wrapped_run_p = fake_wrapped_run_p

    job_enrich.enrich_jobs_ai(
        jobs=jobs,
        resume_text="MLOps engineer.",
        prefs_text="MLOps engineer prefs",
        timeout_s=600,             # generous primary budget
        rescore_timeout_s=120,      # tight rescore cap
        max_jobs_per_call=10,
        two_pass=True,
        triage_floor=2,
        triage_ceiling=5,
        rescore_max_jobs_per_call=5,
        workers=1,
    )

    # primary calls must see the generous timeout, rescore calls the tight one.
    _assert(
        all(t == 600 for t in primary_call_timeouts),
        f"primary calls received timeout_s=600 "
        f"(got {primary_call_timeouts})",
    )
    _assert(
        all(t == 120 for t in rescore_call_timeouts),
        f"rescore calls received rescore_timeout_s=120 "
        f"(got {rescore_call_timeouts})",
    )


def test_double_timeout_drops_jobs() -> None:
    section(
        "3. batch=1 retry that ALSO times out → job is DROPPED, "
        "NOT attributed to primary; forensic emits the drop count"
    )

    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td
    os.environ.pop("FORENSIC_OFF", None)

    for mod in ("forensic", "job_enrich"):
        if mod in sys.modules:
            del sys.modules[mod]
    import job_enrich

    # 6 jobs (one rescore batch of 5 + one of 1 with batch=5). The first
    # rescore batch times out; the batch=1 retry for the 5 jobs in that
    # batch ALSO times out for 2 of them, succeeds for 3.
    jobs = _make_jobs(6)
    first_batch_ids = {j.external_id for j in jobs[0:5]}
    # Of those 5, ids 0 and 1 will keep timing out even at size=1.
    unrecoverable_ids = {jobs[0].external_id, jobs[1].external_id}

    test_rescore_timeout = 1

    def fake_wrapped_run_p(store, caller, prompt, *, timeout_s=None, **kwargs):
        present = [j for j in jobs if j.external_id in prompt]
        if "rescore" in caller:
            is_full_first_batch = (
                len(present) == 5
                and {j.external_id for j in present} == first_batch_ids
            )
            is_unrecoverable_single = (
                len(present) == 1 and present[0].external_id in unrecoverable_ids
            )
            if is_full_first_batch or is_unrecoverable_single:
                time.sleep((timeout_s or test_rescore_timeout) * 1.05)
                return None
            return _rescore_response_for(present)
        return _primary_response_for(present, lambda eid: 3)

    job_enrich.wrapped_run_p = fake_wrapped_run_p

    verdicts = job_enrich.enrich_jobs_ai(
        jobs=jobs,
        resume_text="MLOps engineer.",
        prefs_text="MLOps engineer prefs",
        timeout_s=10,
        rescore_timeout_s=test_rescore_timeout,
        max_jobs_per_call=10,
        two_pass=True,
        triage_floor=2,
        triage_ceiling=5,
        rescore_max_jobs_per_call=5,
        workers=1,
    )

    # 4 verdicts survive: 3 recovered via batch=1 retry + 1 from the
    # second rescore batch. 2 are dropped entirely (no fallback to primary).
    _assert(
        len(verdicts) == 4,
        f"4 jobs verdicted, 2 dropped on double timeout "
        f"(got {len(verdicts)} verdicts)",
    )
    for jid in unrecoverable_ids:
        _assert(
            jid not in verdicts,
            f"unrecoverable job {jid!r} is DROPPED (NOT silently filled "
            f"with primary verdict)",
        )
    for jid in first_batch_ids - unrecoverable_ids:
        _assert(
            verdicts.get(jid, {}).get("match_score") == 4,
            f"recovered job {jid!r} carries the rescore stamp "
            f"(got {verdicts.get(jid, {}).get('match_score')!r})",
        )

    # Forensic must surface the recovery split.
    all_lines = _read_forensic_lines(td)
    timeout_lines = [
        ln for ln in all_lines
        if ln.get("op") == "enrich_jobs_ai.rescore_timeout"
    ]
    _assert(
        len(timeout_lines) == 1,
        f"exactly one rescore_timeout forensic line "
        f"(got {len(timeout_lines)})",
    )
    outp = (timeout_lines[0].get("output") or {})
    inp = (timeout_lines[0].get("input") or {})
    _assert(
        outp.get("recovered_count") == 3,
        f"forensic.recovered_count=3 (got {outp.get('recovered_count')!r})",
    )
    _assert(
        outp.get("dropped_count") == 2,
        f"forensic.dropped_count=2 (got {outp.get('dropped_count')!r})",
    )
    _assert(
        inp.get("n_jobs") == 5,
        f"forensic.input.n_jobs=5 "
        f"(the 5 jobs in the timed-out batch; got {inp.get('n_jobs')!r})",
    )
    _assert(
        inp.get("timeout_s") == test_rescore_timeout,
        f"forensic.input.timeout_s matches the configured cap "
        f"(got {inp.get('timeout_s')!r})",
    )
    _assert(
        inp.get("pass") == "rescore",
        f"forensic.input.pass='rescore' "
        f"(got {inp.get('pass')!r})",
    )


def test_no_timeout_no_retry_no_forensic() -> None:
    section(
        "4. Healthy path: no rescore timeouts → no batch=1 retry, "
        "no rescore_timeout forensic line"
    )

    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td
    os.environ.pop("FORENSIC_OFF", None)

    for mod in ("forensic", "job_enrich"):
        if mod in sys.modules:
            del sys.modules[mod]
    import job_enrich

    jobs = _make_jobs(8)

    rescore_call_log: list[int] = []

    def fake_wrapped_run_p(store, caller, prompt, *, timeout_s=None, **kwargs):
        present = [j for j in jobs if j.external_id in prompt]
        if "rescore" in caller:
            rescore_call_log.append(len(present))
            return _rescore_response_for(present)
        return _primary_response_for(present, lambda eid: 3)

    job_enrich.wrapped_run_p = fake_wrapped_run_p

    verdicts = job_enrich.enrich_jobs_ai(
        jobs=jobs,
        resume_text="MLOps engineer.",
        prefs_text="MLOps engineer prefs",
        timeout_s=60,
        rescore_timeout_s=30,
        max_jobs_per_call=10,
        two_pass=True,
        triage_floor=2,
        triage_ceiling=5,
        rescore_max_jobs_per_call=5,
        workers=1,
    )

    _assert(len(verdicts) == 8, f"all 8 verdicts present (got {len(verdicts)})")
    # Only the two primary rescore batches — no batch=1 retries.
    _assert(
        sorted(rescore_call_log) == [3, 5],
        f"only primary rescore batches ran; no retry calls "
        f"(got {sorted(rescore_call_log)})",
    )
    all_lines = _read_forensic_lines(td)
    timeout_lines = [
        ln for ln in all_lines
        if ln.get("op") == "enrich_jobs_ai.rescore_timeout"
    ]
    _assert(
        timeout_lines == [],
        f"healthy path emits zero rescore_timeout forensic lines "
        f"(got {len(timeout_lines)})",
    )


def test_cli_missing_not_misclassified_as_timeout() -> None:
    section(
        "5. CLI returning None FAST (true cli_missing, elapsed ≈ 0) is "
        "NOT treated as a timeout — split-retry path still fires, "
        "batch=1 retry path does NOT"
    )

    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td
    os.environ.pop("FORENSIC_OFF", None)

    for mod in ("forensic", "job_enrich"):
        if mod in sys.modules:
            del sys.modules[mod]
    import job_enrich

    jobs = _make_jobs(10)
    first_batch_ids = {j.external_id for j in jobs[0:5]}

    rescore_call_log: list[int] = []

    def fake_wrapped_run_p(store, caller, prompt, *, timeout_s=None, **kwargs):
        present = [j for j in jobs if j.external_id in prompt]
        if "rescore" in caller:
            rescore_call_log.append(len(present))
            is_full_first_batch = (
                len(present) == 5
                and {j.external_id for j in present} == first_batch_ids
            )
            if is_full_first_batch:
                # Return None IMMEDIATELY — no sleep. This is the
                # "CLI literally missing / crashed instantly" case.
                # The wrapper's elapsed measurement will be ~0s; with
                # threshold = 0.9 * timeout_s, this is classified as
                # CLI-missing, NOT timeout. The split-retry path (the
                # legacy recovery for transient model glitches) should
                # fire; the new batch=1 retry path should NOT.
                return None
            return _rescore_response_for(present)
        return _primary_response_for(present, lambda eid: 3)

    job_enrich.wrapped_run_p = fake_wrapped_run_p

    verdicts = job_enrich.enrich_jobs_ai(
        jobs=jobs,
        resume_text="MLOps engineer.",
        prefs_text="MLOps engineer prefs",
        timeout_s=60,
        rescore_timeout_s=30,
        max_jobs_per_call=10,
        two_pass=True,
        triage_floor=2,
        triage_ceiling=5,
        rescore_max_jobs_per_call=5,
        workers=1,
    )

    # CLI-missing → split-retry path tries 2 + 3 sub-batches (those
    # SUCCEED in this fake because they no longer match the full-batch
    # poison condition). Plus the second primary batch (size 5).
    # Expected calls: [5 (poisoned), 2 (split half), 3 (split half), 5
    # (other primary)] = 4 calls total.
    _assert(
        sorted(rescore_call_log) == [2, 3, 5, 5],
        f"split-retry fired on CLI-missing (NOT batch=1 retry) "
        f"(got {sorted(rescore_call_log)})",
    )
    _assert(
        len(verdicts) == 10,
        f"split-retry recovered all 10 verdicts (got {len(verdicts)})",
    )
    # No rescore_timeout forensic line.
    all_lines = _read_forensic_lines(td)
    timeout_lines = [
        ln for ln in all_lines
        if ln.get("op") == "enrich_jobs_ai.rescore_timeout"
    ]
    _assert(
        timeout_lines == [],
        f"CLI-missing does NOT trigger rescore_timeout forensic "
        f"(got {len(timeout_lines)})",
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        test_rescore_timeout_triggers_batch_one_retry()
        test_primary_uses_separate_timeout()
        test_double_timeout_drops_jobs()
        test_no_timeout_no_retry_no_forensic()
        test_cli_missing_not_misclassified_as_timeout()
    except AssertionError as e:
        print(f"\nFAILED: {e}")
        sys.exit(1)
    print("\nAll rescore-timeout smoke checks passed.")
