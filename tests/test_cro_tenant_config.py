"""S89 — tenant config tests."""
from __future__ import annotations

import pytest

from src.services.cro_tenant_config import (
    get_config, get_setting, list_tenants, set_default_config,
    set_tenant_config,
)


def test_no_file_returns_empty_default(tmp_path):
    assert get_config('any', path=tmp_path / 'none.json') == {}


def test_set_default_then_get(tmp_path):
    p = tmp_path / 'c.json'
    set_default_config({'roi_threshold': 1.0}, path=p)
    assert get_config('t1', path=p) == {'roi_threshold': 1.0}


def test_tenant_overrides_default(tmp_path):
    p = tmp_path / 'c.json'
    set_default_config({'roi_threshold': 1.0, 'safety_days': 3}, path=p)
    set_tenant_config('t1', {'roi_threshold': 0.8}, path=p)
    cfg = get_config('t1', path=p)
    assert cfg == {'roi_threshold': 0.8, 'safety_days': 3}


def test_nested_dict_merge(tmp_path):
    p = tmp_path / 'c.json'
    set_default_config({'thresholds': {'lift': 0.05, 'roi': 1.0}}, path=p)
    set_tenant_config('t1', {'thresholds': {'lift': 0.10}}, path=p)
    cfg = get_config('t1', path=p)
    assert cfg == {'thresholds': {'lift': 0.10, 'roi': 1.0}}


def test_unknown_tenant_uses_default_only(tmp_path):
    p = tmp_path / 'c.json'
    set_default_config({'k': 'v'}, path=p)
    set_tenant_config('t1', {'k': 'override'}, path=p)
    assert get_config('t2', path=p) == {'k': 'v'}


def test_set_tenant_config_rejects_default_key(tmp_path):
    p = tmp_path / 'c.json'
    with pytest.raises(ValueError):
        set_tenant_config('_default', {}, path=p)


def test_corrupt_file_treated_as_empty(tmp_path):
    p = tmp_path / 'c.json'
    p.write_text('not json', encoding='utf-8')
    assert get_config('t1', path=p) == {}


def test_list_tenants_excludes_default(tmp_path):
    p = tmp_path / 'c.json'
    set_default_config({'k': 'v'}, path=p)
    set_tenant_config('t1', {}, path=p)
    set_tenant_config('t2', {}, path=p)
    assert sorted(list_tenants(path=p)) == ['t1', 't2']


def test_get_setting_nested_path(tmp_path):
    p = tmp_path / 'c.json'
    set_default_config({'a': {'b': {'c': 42}}}, path=p)
    assert get_setting('t1', 'a.b.c', path=p) == 42


def test_get_setting_fallback_when_missing(tmp_path):
    p = tmp_path / 'c.json'
    set_default_config({}, path=p)
    assert get_setting('t1', 'missing.key',
                       fallback='dflt', path=p) == 'dflt'
