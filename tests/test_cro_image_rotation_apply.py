"""S58 — 主图轮换 apply tests."""
from __future__ import annotations

from src.services.cro_image_rotation_apply import apply_rotation_for_skus


def test_dry_run_when_no_callable(tmp_path):
    p = tmp_path / 'rot.jsonl'
    out = apply_rotation_for_skus(
        [{'sku': 'A', 'candidates': ['u1', 'u2']}],
        week_now=1, rotation_path=p,
    )
    assert out['dry_run']
    assert out['applied'][0]['revise_ok'] is None


def test_revise_called_for_each_sku(tmp_path):
    p = tmp_path / 'rot.jsonl'
    calls = []
    out = apply_rotation_for_skus(
        [{'sku': 'A', 'candidates': ['u1', 'u2']},
         {'sku': 'B', 'candidates': ['v1', 'v2']}],
        week_now=1, rotation_path=p,
        revise_callable=lambda sku, url: calls.append((sku, url)) or True,
    )
    assert out['success_count'] == 2
    assert {c[0] for c in calls} == {'A', 'B'}


def test_revise_exception_marked_fail(tmp_path):
    p = tmp_path / 'rot.jsonl'
    def boom(sku, url):
        raise RuntimeError('eBay 500')
    out = apply_rotation_for_skus(
        [{'sku': 'A', 'candidates': ['u1', 'u2']}],
        week_now=1, rotation_path=p, revise_callable=boom,
    )
    assert out['fail_count'] == 1
    assert out['applied'][0]['revise_ok'] is False


def test_skip_sku_with_one_candidate(tmp_path):
    p = tmp_path / 'rot.jsonl'
    out = apply_rotation_for_skus(
        [{'sku': 'A', 'candidates': ['only_one']}],
        week_now=1, rotation_path=p,
    )
    assert out['skipped'][0]['reason'] == 'insufficient_candidates'
    assert out['applied'] == []


def test_cohort_writer_invoked(tmp_path):
    p = tmp_path / 'rot.jsonl'
    cohort = []
    apply_rotation_for_skus(
        [{'sku': 'A', 'candidates': ['u1', 'u2']}],
        week_now=5, rotation_path=p,
        cohort_writer=lambda sku, label, w: cohort.append((sku, label, w)),
    )
    assert cohort[0][0] == 'A'
    assert cohort[0][1].startswith('image_idx_')
    assert cohort[0][2] == 5


def test_cohort_writer_exception_does_not_crash(tmp_path):
    p = tmp_path / 'rot.jsonl'
    out = apply_rotation_for_skus(
        [{'sku': 'A', 'candidates': ['u1', 'u2']}],
        week_now=1, rotation_path=p,
        cohort_writer=lambda *a: (_ for _ in ()).throw(RuntimeError('db')),
    )
    assert len(out['applied']) == 1
