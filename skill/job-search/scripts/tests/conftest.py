"""Shared test isolation guards.

Several modules in this package keep PROCESS-GLOBAL caches that a test may
legitimately (or accidentally) mutate:

  * ``instrumentation.wrappers._resolve_store`` / ``._DEFAULT_STORE`` — the
    lazy telemetry-store resolver + its cached default store.

At least one existing test (``test_listing_verifier_hardening.py``)
OVERWRITES ``wrappers._resolve_store`` with a capture stub via direct
module assignment and never restores it — by design, to dodge the
stale-reference problem the ``from instrumentation.wrappers import ...``
pattern would create. On its own that is harmless, but it LEAKS into any
later test whose telemetry path calls ``_resolve_store``, silently routing
its ``record_claude_call`` rows into the stub instead of the test's store.

This autouse fixture snapshots those globals before each test and restores
them afterwards, so no test can pollute another regardless of collection
order. Restoring a lazy cache to its prior value is always safe — the
worst case is one extra rebuild on next use.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _hiring_contact_off_by_default(monkeypatch):
    """Disable the send-time hiring-contact pass for every test by default.

    ``send_per_job_digest`` runs an LLM + web-search lookup per alive job;
    on a machine with the ``codex`` CLI installed that would spawn real
    subprocesses from any test that exercises the send path. The flag is
    read per call (not at import), so feature tests re-enable it with
    ``monkeypatch.setenv("HIRING_CONTACT_OFF", "0")`` in their body.
    """
    monkeypatch.setenv("HIRING_CONTACT_OFF", "1")
    yield


@pytest.fixture(autouse=True)
def _restore_module_globals():
    # Snapshot before the test.
    try:
        from instrumentation import wrappers as _w
    except Exception:  # pragma: no cover - import should always succeed
        _w = None
    if _w is not None:
        saved_resolve = _w.__dict__.get("_resolve_store")
        saved_default = _w.__dict__.get("_DEFAULT_STORE")


    yield

    # Restore after the test (pass or fail).
    if _w is not None:
        if saved_resolve is not None:
            _w._resolve_store = saved_resolve
        _w._DEFAULT_STORE = saved_default
