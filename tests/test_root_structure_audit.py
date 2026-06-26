"""Root structure audit tests."""
from __future__ import annotations

from tools.root_structure_audit import audit_root, render_markdown


def test_root_audit_classifies_keep_runtime_and_candidates(tmp_path):
    (tmp_path / 'README.md').write_text('x', encoding='utf-8')
    (tmp_path / '.env').write_text('TOKEN=x', encoding='utf-8')
    (tmp_path / 'logs').mkdir()
    (tmp_path / 'favorite_progress.json').write_text('{}', encoding='utf-8')
    (tmp_path / 'one_off_probe.py').write_text('print(1)', encoding='utf-8')

    report = audit_root(tmp_path)
    by_path = {item['path']: item for item in report['items']}

    assert by_path['README.md']['action'] == 'keep'
    assert by_path['.env']['category'] == 'local_config_file'
    assert by_path['logs']['category'] == 'root_directory'
    assert by_path['favorite_progress.json']['category'] == 'runtime_root_file'
    assert by_path['one_off_probe.py']['category'] == 'root_python_candidate'


def test_root_audit_markdown_lists_review_candidates(tmp_path):
    (tmp_path / 'scratch.py').write_text('print(1)', encoding='utf-8')
    report = audit_root(tmp_path)

    markdown = render_markdown(report)

    assert 'Root Structure Audit' in markdown
    assert 'scratch.py' in markdown