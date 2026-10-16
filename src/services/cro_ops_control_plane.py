"""CRO ops control-plane snapshot helpers.

This module wires the S131-S140 operational helpers into one safe snapshot.
It is intentionally read-first: callers must opt in to the DR drill and to
capacity sample recording.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from src.services.cro_canary_release import evaluate_health
from src.services.cro_capacity_planning import assess_components
from src.services.cro_compliance_audit import generate_audit_report
from src.services.cro_continuous_improvement import (
    collect_signals,
    generate_suggestions,
    rank_suggestions,
    render_weekly_brief,
)
from src.services.cro_disaster_recovery import run_dr_drill

DEFAULT_DB_CAPACITY_BYTES = 5 * 1024 * 1024 * 1024


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _read_jsonl(path: Optional[Path]) -> List[Dict[str, Any]]:
    if not path or not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        with path.open('r', encoding='utf-8') as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return rows


def _append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')


def _is_same_day_sample(row: Dict[str, Any], name: str, day_index: int) -> bool:
    try:
        return row.get('component') == name and int(row.get('day_index', -1)) == day_index
    except Exception:
        return False


def build_capacity_components_from_paths(
    paths: Dict[str, Path],
    *,
    capacities: Optional[Dict[str, float]] = None,
    sample_log_path: Optional[Path] = None,
    record_sample: bool = False,
    now: Optional[datetime] = None,
) -> Dict[str, Dict[str, Any]]:
    """Build capacity-planning component inputs from local file sizes."""
    now = now or _utcnow()
    day_index = now.date().toordinal()
    capacities = capacities or {}
    sample_rows = _read_jsonl(sample_log_path)
    components: Dict[str, Dict[str, Any]] = {}

    for name, path in paths.items():
        current_value = float(path.stat().st_size) if path.exists() else 0.0
        if record_sample and sample_log_path is not None:
            already_recorded = any(
                _is_same_day_sample(row, name, day_index)
                for row in sample_rows
            )
            if not already_recorded:
                record = {
                    'ts': now.isoformat(),
                    'component': name,
                    'day_index': day_index,
                    'value': current_value,
                    'path': str(path),
                }
                _append_jsonl(sample_log_path, record)
                sample_rows.append(record)

        samples = [
            {
                'day_index': row.get('day_index'),
                'value': row.get('value'),
            }
            for row in sample_rows
            if row.get('component') == name
            and row.get('day_index') is not None
            and row.get('value') is not None
        ]
        if not samples:
            samples = [{'day_index': day_index, 'value': current_value}]

        components[name] = {
            'samples': samples,
            'capacity': float(capacities.get(name, DEFAULT_DB_CAPACITY_BYTES)),
            'current_value': current_value,
            'path': str(path),
        }

    return components


def _capacity_section(components: Optional[Dict[str, Dict[str, Any]]]) -> Dict[str, Any]:
    if not components:
        return {'status': 'skipped', 'reason': 'no_components'}
    return {'status': 'ok', **assess_components(components)}


def _compliance_section(log_path: Optional[Path]) -> Dict[str, Any]:
    if not log_path:
        return {'status': 'skipped', 'reason': 'no_log_path'}
    if not log_path.exists():
        return {'status': 'skipped', 'reason': 'log_not_found', 'path': str(log_path)}
    return {'status': 'ok', 'path': str(log_path), **generate_audit_report(str(log_path))}


def _dr_section(source_path: Optional[Path], run_now: bool) -> Dict[str, Any]:
    if not run_now:
        return {'status': 'skipped', 'reason': 'not_requested'}
    if not source_path or not source_path.exists():
        return {'status': 'failed', 'reason': 'source_not_found', 'path': str(source_path)}
    drill = run_dr_drill(str(source_path))
    return {'status': 'ok' if drill.get('success') else 'failed', 'drill': drill}


def _canary_section(metrics: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if metrics is None:
        return {'status': 'skipped', 'reason': 'no_metrics'}
    health = evaluate_health(metrics)
    return {'status': 'ok' if health['healthy'] else 'failed', 'health': health}


def _improvement_section(
    *,
    traffic_light_fn: Optional[Callable[[], Dict[str, Any]]] = None,
    returns_fn: Optional[Callable[[], Dict[str, Any]]] = None,
    lift_fn: Optional[Callable[[], Dict[str, Any]]] = None,
    cost_fn: Optional[Callable[[], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    signals = collect_signals(
        traffic_light_fn=traffic_light_fn,
        returns_fn=returns_fn,
        lift_fn=lift_fn,
        cost_fn=cost_fn,
    )
    suggestions = rank_suggestions(generate_suggestions(signals))
    return {
        'status': 'ok',
        'signals': signals,
        'suggestions': suggestions,
        'suggestion_count': len(suggestions),
        'weekly_brief': render_weekly_brief(suggestions),
    }


def _overall_status(sections: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    red: List[str] = []
    yellow: List[str] = []

    canary = sections.get('canary', {})
    if canary.get('status') == 'failed':
        red.append('canary unhealthy')

    capacity = sections.get('capacity', {})
    worst = capacity.get('worst_severity')
    if worst == 'critical':
        red.append('capacity critical')
    elif worst in {'high', 'medium'}:
        yellow.append(f'capacity {worst}')

    disaster_recovery = sections.get('disaster_recovery', {})
    if disaster_recovery.get('status') == 'failed':
        red.append('disaster recovery drill failed')

    compliance = sections.get('compliance', {})
    if int(compliance.get('missing_legal_basis', 0) or 0) > 0:
        red.append('compliance missing legal basis')

    improvement = sections.get('continuous_improvement', {})
    severities = {s.get('severity') for s in improvement.get('suggestions', [])}
    if 'critical' in severities:
        yellow.append('critical improvement suggestions')
    elif severities:
        yellow.append('improvement suggestions pending')

    if red:
        return {'overall_status': 'red', 'reasons': red + yellow}
    if yellow:
        return {'overall_status': 'yellow', 'reasons': yellow}
    return {'overall_status': 'green', 'reasons': []}


def build_ops_snapshot(
    *,
    canary_metrics: Optional[Dict[str, Any]] = None,
    capacity_components: Optional[Dict[str, Dict[str, Any]]] = None,
    compliance_log_path: Optional[Path] = None,
    dr_source_path: Optional[Path] = None,
    run_dr_drill_now: bool = False,
    traffic_light_fn: Optional[Callable[[], Dict[str, Any]]] = None,
    returns_fn: Optional[Callable[[], Dict[str, Any]]] = None,
    lift_fn: Optional[Callable[[], Dict[str, Any]]] = None,
    cost_fn: Optional[Callable[[], Dict[str, Any]]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Build a single CRO ops snapshot for scheduler, CLI, and UI surfaces."""
    now = now or _utcnow()
    sections = {
        'canary': _canary_section(canary_metrics),
        'capacity': _capacity_section(capacity_components),
        'disaster_recovery': _dr_section(dr_source_path, run_dr_drill_now),
        'compliance': _compliance_section(compliance_log_path),
        'continuous_improvement': _improvement_section(
            traffic_light_fn=traffic_light_fn,
            returns_fn=returns_fn,
            lift_fn=lift_fn,
            cost_fn=cost_fn,
        ),
    }
    overall = _overall_status(sections)
    return {
        'generated_at': now.isoformat(),
        **overall,
        'sections': sections,
    }


def render_ops_markdown(snapshot: Dict[str, Any]) -> str:
    lines = [
        f"# CRO Ops Snapshot ({snapshot.get('generated_at', '')})",
        '',
        f"Overall: {str(snapshot.get('overall_status', 'unknown')).upper()}",
    ]
    reasons = snapshot.get('reasons') or []
    if reasons:
        lines.append('')
        lines.extend(f'- {reason}' for reason in reasons)

    sections = snapshot.get('sections') or {}
    capacity = sections.get('capacity') or {}
    if capacity.get('status') == 'ok':
        lines.extend([
            '',
            f"Capacity: {capacity.get('worst_severity', 'unknown')}",
        ])
    dr = sections.get('disaster_recovery') or {}
    if dr.get('status') != 'skipped':
        lines.extend(['', f"DR drill: {dr.get('status')}"])
    compliance = sections.get('compliance') or {}
    if compliance.get('status') == 'ok':
        lines.extend([
            '',
            f"Compliance events: {compliance.get('total_in_window', 0)}",
            f"Missing legal basis: {compliance.get('missing_legal_basis', 0)}",
        ])
    improvement = sections.get('continuous_improvement') or {}
    lines.extend([
        '',
        f"Improvement suggestions: {improvement.get('suggestion_count', 0)}",
        '',
    ])
    return '\n'.join(lines)


def write_ops_snapshot(snapshot: Dict[str, Any], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, default=str),
        encoding='utf-8',
    )
    return output_path