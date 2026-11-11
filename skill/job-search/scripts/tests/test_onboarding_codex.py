"""Codex-backed onboarding translation, answer extraction, and teaser behavior."""
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import onboarding


def _envelope(data):
    body = data if isinstance(data, str) else json.dumps(data)
    return json.dumps({"result": body})


class TranslationCache:
    def __init__(self):
        self.rows = {}

    def get_ui_translation(self, language, key):
        return self.rows.get((language, key))

    def set_ui_translation(self, language, key, text):
        self.rows[language, key] = text


def test_translation_calls_instrumented_codex_and_caches(monkeypatch):
    calls = []

    def run(store, caller, prompt, **kwargs):
        calls.append((caller, prompt))
        return _envelope("Hola 👋")

    monkeypatch.setattr(onboarding, "wrapped_run_p", run)
    cache = TranslationCache()
    assert onboarding.translate(cache, "Hello 👋", "ES") == "Hola 👋"
    assert onboarding.translate(cache, "Hello 👋", "es") == "Hola 👋"
    assert len(calls) == 1
    assert calls[0][0] == "onboarding_translate"
    assert 'code "es"' in calls[0][1]
    assert "Hello 👋" in calls[0][1]
    assert cache.rows["es", hashlib.sha1("Hello 👋".encode()).hexdigest()] == "Hola 👋"


def test_translation_failure_keeps_english_without_caching(monkeypatch):
    monkeypatch.setattr(onboarding, "wrapped_run_p", lambda *a, **kw: None)
    cache = TranslationCache()
    assert onboarding.translate(cache, "Hello", "es") == "Hello"
    assert cache.rows == {}
    monkeypatch.setattr(onboarding, "wrapped_run_p", lambda *a, **kw: pytest.fail("English needs no AI"))
    assert onboarding.translate(cache, "Hello", "en-US") == "Hello"


def test_answer_extraction_validates_closed_fields(monkeypatch):
    monkeypatch.setattr(onboarding, "wrapped_run_p", lambda *a, **kw: _envelope({
        "seniority": "senior", "remote": "remote", "role": " Engineer ",
        "location": "Berlin", "unrelated": "ignore",
    }))
    assert onboarding._llm_parse_answer("Senior engineer, remote Berlin", {}) == {
        "seniority": "senior", "remote": "remote", "role": "Engineer", "location": "Berlin",
    }
    monkeypatch.setattr(onboarding, "wrapped_run_p", lambda *a, **kw: _envelope({
        "seniority": "unknown", "remote": [], "role": 123,
    }))
    assert onboarding._llm_parse_answer("text", {}) == {}
    monkeypatch.setattr(onboarding, "wrapped_run_p", lambda *a, **kw: None)
    assert onboarding._llm_parse_answer("text", {}) == {}


@pytest.mark.parametrize("pick", [None, 0, 3, True, "1"])
def test_teaser_rejects_unknown_or_invalid_picks(monkeypatch, pick):
    monkeypatch.setattr(onboarding, "wrapped_run_p", lambda *a, **kw: _envelope({"pick": pick}))
    assert onboarding._pick_teaser_job({}, [{"title": "One"}, {"title": "Two"}]) is None


def test_teaser_returns_the_selected_original_job(monkeypatch):
    calls = []
    jobs = [{"title": "One", "url": "https://example.com/one"}, {"title": "Two"}]

    def run(store, caller, prompt, **kwargs):
        calls.append((caller, prompt))
        return _envelope({"pick": 2})

    monkeypatch.setattr(onboarding, "wrapped_run_p", run)
    assert onboarding._pick_teaser_job({"role": "Engineer"}, jobs) is jobs[1]
    assert calls[0][0] == "onboarding_teaser"
    assert "Engineer" in calls[0][1] and "https://example.com/one" in calls[0][1]
    monkeypatch.setattr(onboarding, "wrapped_run_p", lambda *a, **kw: pytest.fail("No jobs needs no AI"))
    assert onboarding._pick_teaser_job({}, []) is None
