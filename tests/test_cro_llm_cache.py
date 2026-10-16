"""S81 — LLM cache tests."""
from __future__ import annotations

import pytest

from src.services.cro_llm_cache import (
    cached_llm_call, evict_expired, get_cached, put_cached,
)


def test_get_cached_miss(tmp_path):
    assert get_cached('hi', path=tmp_path / 'c.json') is None


def test_put_then_get(tmp_path):
    p = tmp_path / 'c.json'
    put_cached('hi', 'world', path=p)
    assert get_cached('hi', path=p) == 'world'


def test_ttl_expires(tmp_path):
    p = tmp_path / 'c.json'
    put_cached('hi', 'world', path=p, now=1000.0)
    assert get_cached('hi', path=p, now=1000 + 100, ttl_seconds=60) is None
    assert get_cached('hi', path=p, now=1000 + 30, ttl_seconds=60) == 'world'


def test_different_models_isolated(tmp_path):
    p = tmp_path / 'c.json'
    put_cached('hi', 'a', model='qwen', path=p)
    put_cached('hi', 'b', model='gpt', path=p)
    assert get_cached('hi', model='qwen', path=p) == 'a'
    assert get_cached('hi', model='gpt', path=p) == 'b'


def test_corrupt_file_safe(tmp_path):
    p = tmp_path / 'c.json'
    p.write_text('not json', encoding='utf-8')
    assert get_cached('hi', path=p) is None


def test_cached_llm_call_miss_then_hit(tmp_path):
    p = tmp_path / 'c.json'
    calls = []

    def llm(prompt):
        calls.append(prompt)
        return 'answer'

    r1 = cached_llm_call('q', llm, path=p)
    assert r1 == {'response': 'answer', 'hit': False}
    r2 = cached_llm_call('q', llm, path=p)
    assert r2 == {'response': 'answer', 'hit': True}
    assert len(calls) == 1


def test_cached_llm_call_propagates_exception(tmp_path):
    p = tmp_path / 'c.json'

    def bad(prompt):
        raise RuntimeError('api down')

    with pytest.raises(RuntimeError):
        cached_llm_call('q', bad, path=p)


def test_evict_expired_removes_old(tmp_path):
    p = tmp_path / 'c.json'
    put_cached('a', '1', path=p, now=1000.0)
    put_cached('b', '2', path=p, now=2000.0)
    removed = evict_expired(path=p, ttl_seconds=500, now=2100.0)
    assert removed == 1
    assert get_cached('b', path=p, now=2100.0, ttl_seconds=500) == '2'
    assert get_cached('a', path=p, now=2100.0, ttl_seconds=10000) is None
