#!/usr/bin/env python3
"""Audit root-level clutter without moving or deleting files."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

ROOT_KEEP_FILES = {
    '.gitattributes',
    '.gitignore',
    'README.md',
    'agent.md',
    'skill.md',
    'pytest.ini',
    'requirements.txt',
    'app.py',
    'server.py',
    'daily_tasks.py',
    'batch_publish.py',
    'batch_analyze.py',
    'qwen_optimizer.py',
    'scheduler_daemon.py',
    'scheduler_watchdog.py',
    'start.bat',
    'stop.bat',
    'setup_scheduled_tasks.bat',
    'daily_tasks.bat',
    'gigacloud_rule.yaml',
    'mi_categories.json',
}

ROOT_KEEP_DIRS = {
    '.github',
    'archive',
    'cache',
    'config',
    'data',
    'docs',
    'extension',
    'logs',
    'reports',
    'scripts',
    'src',
    'tasks',
    'tests',
    'tools',
}

ROOT_LOCAL_WORK_DIRS = {
    '.qwen',
    '.streamlit',
    '.venv-1',
    '.vscode',
    'backups',
    'scratch',
    'tmp_imgs',
    'tmp_pdfs',
}

ROOT_RUNTIME_FILES = {
    'favorite_progress.json',
    'sku_product_id_map.json',
    'unfavorited_skus.txt',
}

ROOT_LOCAL_CONFIG_FILES = {
    '.env',
}

RUNTIME_SUFFIXES = {
    '.db',
    '.db-shm',
    '.db-wal',
    '.pid',
    '.log',
}


def _classification(path: Path) -> Dict[str, Any]:
    name = path.name
    if path.is_dir():
        if name in ROOT_KEEP_DIRS:
            return {'category': 'root_directory', 'action': 'keep'}
        if name in ROOT_LOCAL_WORK_DIRS:
            return {
                'category': 'local_workspace_directory',
                'action': 'ignore_or_keep_local_only',
                'suggested_target': '.gitignore or local-only workspace convention',
            }
        if name.startswith('.'):
            return {'category': 'tooling_directory', 'action': 'keep'}
        return {
            'category': 'unknown_directory',
            'action': 'review',
            'suggested_target': 'docs/ROOT_STRUCTURE.md decision',
        }

    if name in ROOT_KEEP_FILES:
        return {'category': 'active_root_entrypoint', 'action': 'keep'}
    if name in ROOT_LOCAL_CONFIG_FILES:
        return {'category': 'local_config_file', 'action': 'keep'}
    if name in ROOT_RUNTIME_FILES or ''.join(path.suffixes[-2:]) in RUNTIME_SUFFIXES or path.suffix in RUNTIME_SUFFIXES:
        return {
            'category': 'runtime_root_file',
            'action': 'review_then_move_or_ignore',
            'suggested_target': 'cache/ or logs/ with compatibility shim if referenced',
        }
    if path.suffix == '.py':
        return {
            'category': 'root_python_candidate',
            'action': 'review_then_move',
            'suggested_target': 'scripts/ for maintained workflows or tools/local_diagnostics/ for probes',
        }
    if path.suffix.lower() in {'.txt', '.json', '.csv'}:
        return {
            'category': 'root_data_candidate',
            'action': 'review_then_move_or_ignore',
            'suggested_target': 'logs/, cache/, or reports/',
        }
    return {
        'category': 'unknown_file',
        'action': 'review',
        'suggested_target': 'docs/ROOT_STRUCTURE.md decision',
    }


def audit_root(root: Path, *, exclude: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    exclude_names = set(exclude or {'.git', '.venv', '__pycache__'})
    items: List[Dict[str, Any]] = []
    by_category: Dict[str, int] = {}
    for path in sorted(root.iterdir(), key=lambda item: item.name.lower()):
        if path.name in exclude_names:
            continue
        info = _classification(path)
        row = {
            'path': path.name,
            'type': 'dir' if path.is_dir() else 'file',
            **info,
        }
        items.append(row)
        by_category[row['category']] = by_category.get(row['category'], 0) + 1
    return {
        'root': str(root),
        'count': len(items),
        'by_category': by_category,
        'items': items,
    }


def render_markdown(report: Dict[str, Any]) -> str:
    lines = [
        '# Root Structure Audit',
        '',
        f"Root: {report.get('root')}",
        f"Items: {report.get('count', 0)}",
        '',
        '## Summary',
    ]
    for category, count in sorted((report.get('by_category') or {}).items()):
        lines.append(f'- {category}: {count}')
    lines.extend(['', '## Review Candidates'])
    for item in report.get('items') or []:
        if item.get('action') == 'keep':
            continue
        target = item.get('suggested_target', '')
        suffix = f' -> {target}' if target else ''
        lines.append(f"- {item['path']} ({item['category']}): {item['action']}{suffix}")
    lines.append('')
    return '\n'.join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description='Audit root-level repository structure')
    parser.add_argument('--root', default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--output', default='')
    parser.add_argument('--markdown', default='')
    args = parser.parse_args()

    report = audit_root(Path(args.root))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding='utf-8')
    if args.markdown:
        md = Path(args.markdown)
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(render_markdown(report), encoding='utf-8')
    print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
