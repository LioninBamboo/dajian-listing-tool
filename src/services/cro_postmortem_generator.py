"""S134 — Postmortem 自动生成.

输入: 事件描述 + 时间线 + 影响 + 已采取动作; 输出 markdown.
可选 LLM 注入提炼根因建议.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


def _ts(s: Any) -> str:
    if isinstance(s, datetime):
        return s.isoformat()
    return str(s) if s else ''


def render_postmortem(
    incident: Dict[str, Any],
    *,
    llm_call: Optional[Callable[[str], str]] = None,
) -> Dict[str, Any]:
    title = incident.get('title') or 'Untitled Incident'
    severity = incident.get('severity') or 'unknown'
    started = _ts(incident.get('started_at'))
    resolved = _ts(incident.get('resolved_at'))
    timeline = incident.get('timeline') or []
    impact = incident.get('impact') or {}
    actions_taken = incident.get('actions_taken') or []
    root_causes = incident.get('root_causes') or []

    duration_minutes = None
    try:
        if started and resolved:
            d_start = datetime.fromisoformat(started.replace('Z', ''))
            d_end = datetime.fromisoformat(resolved.replace('Z', ''))
            duration_minutes = round((d_end - d_start).total_seconds() / 60.0,
                                       1)
    except Exception:
        duration_minutes = None

    lines = [f'# Postmortem: {title}', '',
             f'- **Severity**: {severity}',
             f'- **Started**: {started or "?"}',
             f'- **Resolved**: {resolved or "?"}',
             f'- **Duration (min)**: '
             f'{duration_minutes if duration_minutes is not None else "?"}',
             '']

    lines.append('## Impact')
    if not impact:
        lines.append('- (none recorded)')
    else:
        for k, v in impact.items():
            lines.append(f'- {k}: {v}')
    lines.append('')

    lines.append('## Timeline')
    if not timeline:
        lines.append('- (empty)')
    else:
        for ev in timeline:
            ev_t = _ts(ev.get('at'))
            lines.append(f'- {ev_t}: {ev.get("event") or ""}')
    lines.append('')

    lines.append('## Root Causes')
    if not root_causes:
        lines.append('- (TBD)')
    else:
        for rc in root_causes:
            lines.append(f'- {rc}')
    lines.append('')

    lines.append('## Actions Taken')
    if not actions_taken:
        lines.append('- (none)')
    else:
        for a in actions_taken:
            lines.append(f'- {a}')
    lines.append('')

    llm_summary = None
    llm_error = None
    if llm_call:
        try:
            prompt = (f'Summarise the postmortem and propose 3 follow-up '
                       f'actions in bullet form. Title: {title}. Severity: '
                       f'{severity}. Root causes: {root_causes}. '
                       f'Actions taken: {actions_taken}.')
            llm_summary = llm_call(prompt)
        except Exception as e:
            llm_error = repr(e)

    lines.append('## Follow-up Recommendations')
    if llm_summary:
        lines.append(llm_summary.strip())
    elif root_causes:
        for rc in root_causes:
            lines.append(f'- Add monitoring/alert for: {rc}')
    else:
        lines.append('- Conduct manual review with on-call engineer.')
    if llm_error:
        lines.append(f'\n_(LLM unavailable: {llm_error})_')

    md = '\n'.join(lines).rstrip() + '\n'
    return {
        'markdown': md,
        'duration_minutes': duration_minutes,
        'llm_used': llm_summary is not None,
        'llm_error': llm_error,
    }
