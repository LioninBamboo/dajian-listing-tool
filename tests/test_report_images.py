from __future__ import annotations

from src.utils import report_images


def test_build_thumbnail_img_html_uses_https_not_data_uri(monkeypatch):
    report_images.inline_image_src.cache_clear()

    def boom(*args, **kwargs):
        raise AssertionError("report_images must not fetch images for data URI embedding")

    monkeypatch.setattr(report_images.requests, "get", boom)

    url = (
        "https://b2bfiles1.gigab2b.cn/image/wkseller/1/abc.png"
        "?x-oss-process=image%2Fresize%2Cw_800%2Ch_800%2Cm_pad"
    )
    html = report_images.build_thumbnail_img_html(url, width=40, height=40)

    assert "data:image" not in html
    assert "base64," not in html
    assert 'src="' in html
    assert "w_120%2Ch_120" in html
    assert html.startswith("<img ")


def test_inline_image_src_returns_empty_for_blank():
    report_images.inline_image_src.cache_clear()
    assert report_images.inline_image_src("") == ""
    assert report_images.build_thumbnail_img_html("") == ""
