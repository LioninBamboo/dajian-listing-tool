from scripts import sales_health_check as sales_health


def test_price_mismatch_email_keeps_summary_without_product_detail():
    checker = sales_health.SalesHealthChecker()
    checker.published_count = 1
    checker._load_sku_thumbnails = lambda skus: {}
    checker.results['price_mismatch'] = [{
        'sku': 'PRICE-MISMATCH-001',
        'listing_id': '123456789',
        'ebay_price': 19.99,
        'db_price': 24.99,
        'diff_pct': -20.0,
        'source_type': '历史遗留不同步',
        'source_reason': '历史数据未同步',
    }]

    html = checker.generate_email_html()

    assert '价格偏差产品: 1 个' in html
    assert '历史遗留不同步 1' in html
    assert 'PRICE-MISMATCH-001' not in html
    assert 'eBay售价' not in html
    assert 'DB目标价' not in html
    assert '原因说明' not in html
    assert '历史数据未同步' not in html
