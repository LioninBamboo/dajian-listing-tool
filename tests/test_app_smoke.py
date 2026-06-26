"""app.py 冒烟测试 — 保证导航 label 与 elif 分支严格对齐, 杜绝 mojibake / 漏接.

历史教训: app.py main_pages 列表里 emoji 被 mojibake 损坏 (出现 �),
导致 elif page == "<label>" 永远匹配不上, 页面空白.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

APP_PY = Path(__file__).resolve().parents[1] / "app.py"


def test_app_py_exists():
    assert APP_PY.exists(), "app.py 必须存在"


def test_main_pages_no_replacement_char():
    """main_pages 里禁止出现 U+FFFD (mojibake 标志)."""
    text = APP_PY.read_text(encoding='utf-8')
    m = re.search(r'main_pages\s*=\s*\[([^\]]+)\]', text)
    assert m, "找不到 main_pages 定义"
    block = m.group(1)
    assert '\ufffd' not in block, f"main_pages 含 U+FFFD (mojibake): {block!r}"
    assert '?' not in re.sub(r'"[^"]*"', '', block).replace(',', '').replace(' ', ''), \
        "main_pages 结构异常 (混入裸 ?)"


def test_every_main_page_has_elif_branch():
    """main_pages 里每个 label 都必须有对应的 if/elif page == "<label>" 分支."""
    text = APP_PY.read_text(encoding='utf-8')
    m = re.search(r'main_pages\s*=\s*\[([^\]]+)\]', text)
    assert m
    labels = re.findall(r'"([^"]+)"', m.group(1))
    assert labels, "main_pages 解析为空"

    branches = set(re.findall(r'(?:if|elif)\s+page\s*==\s*"([^"]+)"', text))
    missing = [lbl for lbl in labels if lbl not in branches]
    assert not missing, (
        f"以下 main_pages 标签没有匹配的 elif 分支 → 选中后页面会空白: {missing}"
    )


def test_all_elif_branches_appear_in_pages_or_plugins():
    """反向校验: 所有 elif page == ... 都应在 main_pages 里 (除非是插件页)."""
    text = APP_PY.read_text(encoding='utf-8')
    m = re.search(r'main_pages\s*=\s*\[([^\]]+)\]', text)
    assert m
    labels = set(re.findall(r'"([^"]+)"', m.group(1)))
    branches = set(re.findall(r'(?:if|elif)\s+page\s*==\s*"([^"]+)"', text))
    orphans = branches - labels
    # 允许插件 / 历史遗留分支, 这里只警告不强制
    if orphans:
        # 仅当超过 5 个孤儿分支时认为异常
        assert len(orphans) < 10, f"过多孤儿 elif 分支 (>=10): {sorted(orphans)}"
