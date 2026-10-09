from __future__ import annotations

from src.services.cro_email_digest import (
    summarize_rows_by_sku,
    render_promote_email,
    render_auto_execution_email,
)


def test_summarize_rows_by_sku_dedupes_and_counts_duplicates():
    rows = [
        {'sku': 'A', 'status': 'skipped', 'reason': 'already at cap', 'cur_bid': 5.0},
        {'sku': 'A', 'status': 'skipped', 'reason': 'already at cap', 'cur_bid': 5.0},
        {'sku': 'B', 'status': 'done', 'reason': 'updated', 'new_bid': 7.0},
    ]
    summary = summarize_rows_by_sku(rows)
    assert [row['sku'] for row in summary] == ['A', 'B']
    assert summary[0]['occurrences'] == 2
    assert summary[0]['reason_summary'] == 'already at cap x2'
    assert summary[1]['occurrences'] == 1


def test_render_promote_email_includes_title_thumbnail_and_links():
    report = {
        'pending_total': 2,
        'done': ['B'],
        'failed': [],
        'skipped': ['A'],
        'rows': [
            {'sku': 'A', 'status': 'skipped', 'reason': 'already at cap', 'cur_bid': 5.0, 'dyn_cap': 5.0},
            {'sku': 'B', 'status': 'done', 'reason': 'created ad', 'new_bid': 5.0, 'created_ad': True},
        ],
    }
    html = render_promote_email(
        report,
        product_briefs={
            'A': {
                'title': 'Alpha Chair',
                'thumbnail_url': 'https://img.example.com/a.jpg',
                'ebay_url': 'https://www.ebay.com/itm/111',
                'source_url': 'https://supplier.example.com/a',
            },
            'B': {
                'title': 'Beta Table',
                'thumbnail_url': 'https://img.example.com/b.jpg',
                'ebay_url': 'https://www.ebay.com/itm/222',
            },
        },
    )
    assert 'Alpha Chair' in html
    assert 'Beta Table' in html
    assert 'https://img.example.com/a.jpg' in html
    assert 'https://www.ebay.com/itm/111' in html
    assert 'https://supplier.example.com/a' in html
    assert 'already at cap' in html


def test_render_auto_execution_email_surfaces_product_rows():
    report = {
        'actions': [
            {
                'action': 'promote',
                'pending_total': 1,
                'done': 0,
                'failed': 0,
                'skipped': 1,
                'report': {
                    'rows': [
                        {'sku': 'A', 'status': 'skipped', 'reason': 'already at cap'},
                    ]
                },
            }
        ]
    }
    html = render_auto_execution_email(
        report,
        product_briefs={
            'A': {'title': 'Alpha Chair', 'thumbnail_url': 'https://img.example.com/a.jpg'}
        },
    )
    assert 'CRO 自动执行结果' in html
    assert 'Alpha Chair' in html
    assert 'already at cap' in html
    assert 'promote' in html