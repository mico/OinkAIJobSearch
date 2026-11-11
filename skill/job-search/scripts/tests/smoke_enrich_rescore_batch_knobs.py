#!/usr/bin/env python3
"""Behavioral coverage for two-pass Codex scoring.

Exercises independent rescore batching, the triage ceiling, split retries,
and per-job forensic records for primary verdicts trusted without a rescore.
Model calls are stubbed; no provider-specific model aliases or default pins.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
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
    """Wrap a list of result dicts in the `codex -p --output-format json`
    envelope shape so extract_assistant_text + parse_json_block see a
    happy-path response."""
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": json.dumps({"results": items}),
    }
    return json.dumps(envelope)


def _primary_response_for(present: list[_FakeJob], score_for) -> str:
    """Build a primary-shape happy response where each posting is scored by
    the `score_for(ext_id) -> int` callback. Used to control which jobs
    end up in the rescore rescore window."""
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


def _rescore_response_for(present: list[_FakeJob]) -> str:
    """rescore always emits a fixed score=4 verdict — used so the test
    can detect WHICH jobs rescore actually saw (score=4 means rescore
    touched it; score from the primary fake means it didn't)."""
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


def _empty_envelope() -> str:
    return json.dumps({
        "type": "result", "subtype": "success",
        "is_error": False, "result": "",
    })



def test_triage_ceiling_skips_top_end() -> None:
    section("1. triage_ceiling=5 — primary=5 verdicts skip the rescore rescore")

    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td
    os.environ.pop("FORENSIC_OFF", None)

    for mod in ("forensic", "job_enrich"):
        if mod in sys.modules:
            del sys.modules[mod]
    import job_enrich

    # 20 jobs. primary will score:
    #   ext-0000..ext-0009 → 5 (top end → SKIP rescore)
    #   ext-0010..ext-0014 → 3 (mid → rescore rescores)
    #   ext-0015..ext-0019 → 1 (below floor → no rescore)
    jobs = _make_jobs(20)
    top_ids = {j.external_id for j in jobs[0:10]}
    mid_ids = {j.external_id for j in jobs[10:15]}
    low_ids = {j.external_id for j in jobs[15:20]}

    def score_for(ext_id: str) -> int:
        if ext_id in top_ids:
            return 5
        if ext_id in mid_ids:
            return 3
        return 1

    rescore_seen_ids: set[str] = set()

    def fake_wrapped_run_p(store, caller, prompt, **kwargs):
        present = [j for j in jobs if j.external_id in prompt]
        if "rescore" in caller:
            rescore_seen_ids.update(j.external_id for j in present)
            return _rescore_response_for(present)
        return _primary_response_for(present, score_for)

    job_enrich.wrapped_run_p = fake_wrapped_run_p

    verdicts = job_enrich.enrich_jobs_ai(
        jobs=jobs,
        resume_text="MLOps engineer, 8 years Python and AWS.",
        prefs_text="MLOps engineer prefs",
        timeout_s=60,
        max_jobs_per_call=10,
        two_pass=True,
        triage_floor=2,
        triage_ceiling=5,           # production value
        rescore_max_jobs_per_call=5, # production value
        workers=1,                  # deterministic ordering
    )

    # 1) rescore must have seen ONLY the mid-band ids — not the top end,
    #    not the below-floor band.
    _assert(
        rescore_seen_ids == mid_ids,
        f"rescore saw exactly the mid-band ids "
        f"(extras={rescore_seen_ids - mid_ids}, "
        f"missing={mid_ids - rescore_seen_ids})",
    )
    # 2) The trusted-top primary verdicts must be returned UNTOUCHED
    #    (score=5, not the score=4 rescore stamp).
    for tid in top_ids:
        _assert(
            verdicts.get(tid, {}).get("match_score") == 5,
            f"primary=5 verdict {tid!r} passed through untouched "
            f"(got {verdicts.get(tid, {}).get('match_score')!r})",
        )
    # 3) Mid-band ids carry the rescore stamp (=4).
    for mid in mid_ids:
        _assert(
            verdicts.get(mid, {}).get("match_score") == 4,
            f"mid-band verdict {mid!r} carries rescore stamp "
            f"(got {verdicts.get(mid, {}).get('match_score')!r})",
        )
    # 4) Below-floor primary verdicts also pass through (score=1).
    for lid in low_ids:
        _assert(
            verdicts.get(lid, {}).get("match_score") == 1,
            f"below-floor verdict {lid!r} passed through "
            f"(got {verdicts.get(lid, {}).get('match_score')!r})",
        )


def test_rescore_batch_size_drives_chunking() -> None:
    section(
        "2. rescore_max_jobs_per_call=5 — rescore pool chunks at 5, "
        "primary at 10"
    )

    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td

    for mod in ("forensic", "job_enrich"):
        if mod in sys.modules:
            del sys.modules[mod]
    import job_enrich

    # 12 jobs all in the rescore window so the rescore touches every one.
    jobs = _make_jobs(12)
    every_id = {j.external_id for j in jobs}

    primary_batch_sizes: list[int] = []
    rescore_batch_sizes: list[int] = []

    def fake_wrapped_run_p(store, caller, prompt, **kwargs):
        present = [j for j in jobs if j.external_id in prompt]
        if "rescore" in caller:
            rescore_batch_sizes.append(len(present))
            return _rescore_response_for(present)
        primary_batch_sizes.append(len(present))
        return _primary_response_for(present, lambda eid: 3)  # all mid

    job_enrich.wrapped_run_p = fake_wrapped_run_p

    verdicts = job_enrich.enrich_jobs_ai(
        jobs=jobs,
        resume_text="MLOps engineer.",
        prefs_text="MLOps engineer prefs",
        timeout_s=60,
        max_jobs_per_call=10,        # primary batch knob
        two_pass=True,
        triage_floor=2,
        triage_ceiling=5,
        rescore_max_jobs_per_call=5,  # rescore batch knob — this is the test
        workers=1,
    )

    _assert(
        len(verdicts) == 12,
        f"all 12 jobs verdicted (got {len(verdicts)})",
    )
    # primary ran at the bigger batch knob: 12 jobs → 10 + 2.
    _assert(
        sorted(primary_batch_sizes) == [2, 10],
        f"primary chunks at max_jobs_per_call=10 (got {primary_batch_sizes})",
    )
    # rescore ran at the smaller knob: 12 jobs → 5 + 5 + 2.
    _assert(
        sorted(rescore_batch_sizes) == [2, 5, 5],
        f"rescore chunks at rescore_max_jobs_per_call=5 "
        f"(got {rescore_batch_sizes})",
    )
    # Every rescore-rescored job carries the rescore stamp.
    _assert(
        all(verdicts[eid]["match_score"] == 4 for eid in every_id),
        "every job carries the rescore stamp after rescore",
    )


def test_rescore_split_retry_still_fires_at_batch_5() -> None:
    section(
        "3. split-retry still recovers when a rescore batch=5 call fails — "
        "the smaller batch knob doesn't disable the failure-recovery path"
    )

    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td

    for mod in ("forensic", "job_enrich"):
        if mod in sys.modules:
            del sys.modules[mod]
    import job_enrich

    # 10 jobs, all in the rescore window. With rescore_max_jobs_per_call=5
    # that's 2 rescore batches. We poison the FIRST rescore batch so it
    # returns an empty-result envelope on the first attempt, but only at
    # batch_size >= 5. Splitting it into 2 + 3 then succeeds — the same
    # shape as the original batch-failure test but at the new batch size.
    jobs = _make_jobs(10)
    first_rescore_batch_ids = {j.external_id for j in jobs[0:5]}

    rescore_call_log: list[int] = []

    def fake_wrapped_run_p(store, caller, prompt, **kwargs):
        present = [j for j in jobs if j.external_id in prompt]
        if "rescore" in caller:
            rescore_call_log.append(len(present))
            # Empty-result on the FULL first rescore batch (size 5,
            # contains every one of first_rescore_batch_ids); on the
            # split halves return success.
            is_full_first_batch = (
                len(present) == 5
                and {j.external_id for j in present} == first_rescore_batch_ids
            )
            if is_full_first_batch:
                return _empty_envelope()
            return _rescore_response_for(present)
        return _primary_response_for(present, lambda eid: 3)  # all mid

    job_enrich.wrapped_run_p = fake_wrapped_run_p

    verdicts = job_enrich.enrich_jobs_ai(
        jobs=jobs,
        resume_text="MLOps engineer.",
        prefs_text="MLOps engineer prefs",
        timeout_s=60,
        max_jobs_per_call=10,
        two_pass=True,
        triage_floor=2,
        triage_ceiling=5,
        rescore_max_jobs_per_call=5,
        workers=1,
    )

    # Every job should still come back via the primary verdict (score=3)
    # OR the rescore stamp (=4). The retry-recovered first batch should
    # carry the rescore stamp.
    _assert(
        len(verdicts) == 10,
        f"all 10 verdicts present (got {len(verdicts)})",
    )
    for jid in first_rescore_batch_ids:
        _assert(
            verdicts.get(jid, {}).get("match_score") == 4,
            f"split-retry recovered rescore stamp for {jid!r} "
            f"(got {verdicts.get(jid, {}).get('match_score')!r})",
        )
    # rescore call shape: 1 full-batch failure (size=5) + 2 split-retry
    # subcalls (sizes 2 + 3) + 1 second-batch success (size=5) = 4 calls.
    _assert(
        sorted(rescore_call_log) == [2, 3, 5, 5],
        f"rescore split-retry produced expected call shape (got "
        f"{rescore_call_log})",
    )


def _read_forensic_lines(state_dir: str) -> list[dict]:
    """Slurp every forensic JSONL line written under STATE_DIR. The
    forensic writer rotates files but the test never hits the rotation
    threshold, so log.0.jsonl typically contains everything."""
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


def test_trusted_top_emits_per_job_forensic() -> None:
    section(
        "4. trusted-top primary verdicts emit a per-job forensic line — "
        "audit trail for any 5/5 alert that bypassed rescore"
    )

    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td
    os.environ.pop("FORENSIC_OFF", None)

    for mod in ("forensic", "job_enrich"):
        if mod in sys.modules:
            del sys.modules[mod]
    import job_enrich

    # Same shape as test 1: top band skips rescore, mid band goes to
    # rescore. We only care that the top band shows up in the forensic
    # log here.
    jobs = _make_jobs(8)
    top_ids = {j.external_id for j in jobs[0:5]}
    mid_ids = {j.external_id for j in jobs[5:8]}

    def score_for(ext_id: str) -> int:
        if ext_id in top_ids:
            return 5
        if ext_id in mid_ids:
            return 3
        return 1

    def fake_wrapped_run_p(store, caller, prompt, **kwargs):
        present = [j for j in jobs if j.external_id in prompt]
        if "rescore" in caller:
            return _rescore_response_for(present)
        return _primary_response_for(present, score_for)

    job_enrich.wrapped_run_p = fake_wrapped_run_p

    job_enrich.enrich_jobs_ai(
        jobs=jobs,
        resume_text="MLOps engineer, 8 years Python and AWS.",
        prefs_text="MLOps engineer prefs",
        timeout_s=60,
        max_jobs_per_call=10,
        two_pass=True,
        triage_floor=2,
        triage_ceiling=5,
        rescore_max_jobs_per_call=5,
        workers=1,
    )

    all_lines = _read_forensic_lines(td)
    trusted_lines = [
        ln for ln in all_lines
        if ln.get("op") == "enrich_jobs_ai.trusted_top"
    ]

    # 1) Exactly one forensic line per trusted-top job.
    _assert(
        len(trusted_lines) == len(top_ids),
        f"one trusted_top forensic line per primary-5 job "
        f"(expected {len(top_ids)}, got {len(trusted_lines)})",
    )

    # 2) Every top-band ext_id is represented exactly once.
    logged_ext_ids = sorted(
        (ln.get("input", {}) or {}).get("ext_id") for ln in trusted_lines
    )
    _assert(
        logged_ext_ids == sorted(top_ids),
        f"trusted_top lines cover every primary-5 ext_id "
        f"(missing={set(top_ids) - set(logged_ext_ids)}, "
        f"extras={set(logged_ext_ids) - set(top_ids)})",
    )

    # 3) Shape contract: each line carries job_id, ext_id, title,
    #    primary_score, triage_ceiling. This is what the operator will
    #    `jq` over when chasing a bad 5/5 alert — if any of these go
    #    missing the audit trail breaks.
    sample = trusted_lines[0]
    inp = sample.get("input", {}) or {}
    outp = sample.get("output", {}) or {}
    for key in ("job_id", "ext_id", "title", "company"):
        _assert(
            key in inp and inp[key] not in (None, ""),
            f"trusted_top.input carries {key!r} (sample={inp!r})",
        )
    _assert(
        outp.get("primary_score") == 5,
        f"trusted_top.output.primary_score==5 (got {outp.get('primary_score')!r})",
    )
    _assert(
        outp.get("triage_ceiling") == 5,
        f"trusted_top.output.triage_ceiling==5 "
        f"(got {outp.get('triage_ceiling')!r})",
    )

    # 4) Mid-band ids must NOT show up as trusted_top — they went to
    #    rescore and should be auditable through enrich_jobs_ai.batch
    #    forensic lines instead.
    for mid in mid_ids:
        _assert(
            mid not in logged_ext_ids,
            f"mid-band id {mid!r} is NOT logged as trusted_top",
        )


def test_trusted_top_no_emit_when_ceiling_open() -> None:
    section(
        "5. triage_ceiling=6 (open / legacy default) emits zero "
        "trusted_top lines — every primary verdict still goes to rescore"
    )

    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td
    os.environ.pop("FORENSIC_OFF", None)

    for mod in ("forensic", "job_enrich"):
        if mod in sys.modules:
            del sys.modules[mod]
    import job_enrich

    jobs = _make_jobs(6)

    def fake_wrapped_run_p(store, caller, prompt, **kwargs):
        present = [j for j in jobs if j.external_id in prompt]
        if "rescore" in caller:
            return _rescore_response_for(present)
        # All primary=5 — yet ceiling=6 means none are "trusted".
        return _primary_response_for(present, lambda eid: 5)

    job_enrich.wrapped_run_p = fake_wrapped_run_p

    job_enrich.enrich_jobs_ai(
        jobs=jobs,
        resume_text="MLOps engineer.",
        prefs_text="MLOps engineer prefs",
        timeout_s=60,
        max_jobs_per_call=10,
        two_pass=True,
        triage_floor=2,
        triage_ceiling=6,            # legacy / backwards-compat default
        rescore_max_jobs_per_call=5,
        workers=1,
    )

    all_lines = _read_forensic_lines(td)
    trusted_lines = [
        ln for ln in all_lines
        if ln.get("op") == "enrich_jobs_ai.trusted_top"
    ]
    _assert(
        trusted_lines == [],
        f"ceiling=6 emits zero trusted_top lines (got {len(trusted_lines)})",
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        test_triage_ceiling_skips_top_end()
        test_rescore_batch_size_drives_chunking()
        test_rescore_split_retry_still_fires_at_batch_5()
        test_trusted_top_emits_per_job_forensic()
        test_trusted_top_no_emit_when_ceiling_open()
    except AssertionError as e:
        print(f"\nFAILED: {e}")
        sys.exit(1)
    print("\nAll rescore-batch-knob smoke checks passed.")
