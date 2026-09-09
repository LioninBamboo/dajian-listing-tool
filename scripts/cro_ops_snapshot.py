#!/usr/bin/env python3
"""Generate the CRO ops control-plane snapshot."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.services.cro_ops_control_plane import (  # noqa: E402
    DEFAULT_DB_CAPACITY_BYTES,
    build_capacity_components_from_paths,
    build_ops_snapshot,
    render_ops_markdown,
    write_ops_snapshot,
)


def _load_json(path: Optional[Path]) -> Optional[Dict[str, Any]]:
    if not path or not path.exists():
        return None
    return json.loads(path.read_text(encoding='utf-8'))


def main() -> int:
    parser = argparse.ArgumentParser(description='Generate CRO ops snapshot')
    parser.add_argument('--db', default=str(PROJECT_ROOT / 'ebay_collection.db'))
    parser.add_argument('--output', default=str(PROJECT_ROOT / 'logs' / 'cro_ops_snapshot.json'))
    parser.add_argument('--brief', default='')
    parser.add_argument('--capacity-sample-log', default=str(PROJECT_ROOT / 'logs' / 'cro_capacity_samples.jsonl'))
    parser.add_argument('--db-capacity-bytes', type=float, default=float(os.getenv('CRO_DB_CAPACITY_BYTES', DEFAULT_DB_CAPACITY_BYTES)))
    parser.add_argument('--record-capacity-sample', action='store_true')
    parser.add_argument('--canary-metrics-json', default='')
    parser.add_argument('--compliance-log', default=str(PROJECT_ROOT / 'logs' / 'cro_compliance_audit.jsonl'))
    parser.add_argument('--dr-drill', action='store_true')
    parser.add_argument('--fail-on-red', action='store_true')
    args = parser.parse_args()

    db_path = Path(args.db)
    capacity_components = build_capacity_components_from_paths(
        {'ebay_collection_db': db_path},
        capacities={'ebay_collection_db': args.db_capacity_bytes},
        sample_log_path=Path(args.capacity_sample_log),
        record_sample=args.record_capacity_sample,
    )
    snapshot = build_ops_snapshot(
        canary_metrics=_load_json(Path(args.canary_metrics_json)) if args.canary_metrics_json else None,
        capacity_components=capacity_components,
        compliance_log_path=Path(args.compliance_log),
        dr_source_path=db_path,
        run_dr_drill_now=args.dr_drill,
    )
    write_ops_snapshot(snapshot, Path(args.output))

    markdown = render_ops_markdown(snapshot)
    if args.brief:
        brief_path = Path(args.brief)
        brief_path.parent.mkdir(parents=True, exist_ok=True)
        brief_path.write_text(markdown, encoding='utf-8')
    print(markdown)
    if args.fail_on_red and snapshot.get('overall_status') == 'red':
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())