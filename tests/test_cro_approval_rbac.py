"""S69 — RBAC tests."""
from __future__ import annotations

import pytest

from src.services.cro_approval_rbac import (
    annotate_permissions, can, filter_actionable, require,
    role_resolver_factory,
)


def test_viewer_can_only_view():
    assert can('viewer', 'view')
    assert not can('viewer', 'approve_threshold')
    assert not can('viewer', 'approve_delist')


def test_operator_can_threshold_and_promote_not_delist():
    assert can('operator', 'approve_threshold')
    assert can('operator', 'approve_promote')
    assert can('operator', 'approve_pricedrop')
    assert not can('operator', 'approve_delist')
    assert not can('operator', 'approve_rollback')


def test_admin_can_everything():
    for cap in ('view', 'approve_threshold', 'approve_promote',
                'approve_pricedrop', 'approve_rollback',
                'approve_delist', 'bulk_approve'):
        assert can('admin', cap)


def test_unknown_role_or_capability_returns_false():
    assert not can(None, 'view')
    assert not can('superuser', 'view')
    assert not can('admin', 'do_god_thing')


def test_require_raises_for_insufficient_role():
    with pytest.raises(PermissionError):
        require('operator', 'approve_delist')


def test_require_passes_for_admin():
    require('admin', 'approve_delist')  # 不抛


def test_filter_actionable_for_operator():
    items = [
        {'sku': 'A', 'kind': 'threshold'},
        {'sku': 'B', 'kind': 'delist'},
        {'sku': 'C', 'kind': 'promote'},
        {'sku': 'D', 'kind': 'rollback'},
    ]
    out = filter_actionable(items, 'operator')
    assert {x['sku'] for x in out} == {'A', 'C'}


def test_filter_actionable_for_admin():
    items = [
        {'sku': 'A', 'kind': 'threshold'},
        {'sku': 'B', 'kind': 'delist'},
        {'sku': 'C', 'kind': 'rollback'},
    ]
    assert len(filter_actionable(items, 'admin')) == 3


def test_annotate_permissions_does_not_drop_items():
    items = [{'sku': 'A', 'kind': 'delist'}]
    out = annotate_permissions(items, 'operator')
    assert len(out) == 1
    assert out[0]['_can_action'] is False
    assert out[0]['_required_capability'] == 'approve_delist'


def test_role_resolver_default_for_unknown():
    resolve = role_resolver_factory(lambda u: None)
    assert resolve('alice') == 'viewer'


def test_role_resolver_returns_known_role():
    resolve = role_resolver_factory(lambda u: 'admin')
    assert resolve('bob') == 'admin'


def test_role_resolver_swallows_lookup_exception():
    def _bad(_u):
        raise RuntimeError('db down')
    resolve = role_resolver_factory(_bad, default='viewer')
    assert resolve('x') == 'viewer'
