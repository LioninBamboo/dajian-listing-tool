"""S128 — secret manager tests."""
from __future__ import annotations

import json

import pytest

from src.services import cro_secret_manager as sm


@pytest.fixture(autouse=True)
def _clear_cache_each_test():
    sm.clear_cache()
    yield
    sm.clear_cache()


def test_env_takes_priority(tmp_path):
    p = tmp_path / 's.json'
    p.write_text(json.dumps({'A': 'from_file'}))
    val = sm.get_secret('A', env={'A': 'from_env'}, secrets_file=str(p))
    assert val == 'from_env'


def test_falls_back_to_file(tmp_path):
    p = tmp_path / 's.json'
    p.write_text(json.dumps({'A': 'from_file'}))
    val = sm.get_secret('A', env={}, secrets_file=str(p))
    assert val == 'from_file'


def test_default_when_missing():
    val = sm.get_secret('NOPE', env={}, default='dflt')
    assert val == 'dflt'


def test_required_missing_raises():
    with pytest.raises(sm.SecretMissingError):
        sm.get_secret('NOPE', env={}, required=True)


def test_empty_env_value_treated_as_missing(tmp_path):
    val = sm.get_secret('A', env={'A': ''}, default='dflt')
    assert val == 'dflt'


def test_has_secret_true_false(tmp_path):
    assert sm.has_secret('X', env={'X': '1'}) is True
    assert sm.has_secret('X', env={}) is False


def test_cache_round_trip(tmp_path):
    val1 = sm.get_secret('A', env={'A': 'first'})
    # 改变 env 后由于缓存仍返回旧值
    val2 = sm.get_secret('A', env={'A': 'second'})
    assert val1 == val2 == 'first'
    sm.clear_cache()
    val3 = sm.get_secret('A', env={'A': 'second'})
    assert val3 == 'second'


def test_use_cache_false_bypasses(tmp_path):
    sm.get_secret('A', env={'A': '1'})
    val = sm.get_secret('A', env={'A': '2'}, use_cache=False)
    assert val == '2'


def test_invalid_name_raises():
    with pytest.raises(ValueError):
        sm.get_secret('')


def test_bad_secrets_file_silently_ignored(tmp_path):
    p = tmp_path / 's.json'
    p.write_text('not valid json')
    val = sm.get_secret('A', env={}, secrets_file=str(p), default='d')
    assert val == 'd'
