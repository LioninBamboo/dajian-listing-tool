"""S134 — postmortem generator tests."""
from __future__ import annotations

from src.services.cro_postmortem_generator import render_postmortem


def test_minimal_incident():
    out = render_postmortem({})
    md = out['markdown']
    assert '# Postmortem: Untitled Incident' in md
    assert '## Impact' in md
    assert '## Timeline' in md
    assert '## Root Causes' in md
    assert '## Follow-up Recommendations' in md


def test_duration_calculated():
    out = render_postmortem({
        'started_at': '2026-05-01T12:00:00',
        'resolved_at': '2026-05-01T12:30:00',
    })
    assert out['duration_minutes'] == 30.0


def test_invalid_timestamps_yield_none_duration():
    out = render_postmortem({'started_at': 'wrong',
                              'resolved_at': 'also wrong'})
    assert out['duration_minutes'] is None


def test_timeline_rendered():
    out = render_postmortem({
        'timeline': [
            {'at': '12:00', 'event': 'alert fired'},
            {'at': '12:05', 'event': 'oncall acknowledged'},
        ],
    })
    assert 'alert fired' in out['markdown']
    assert 'oncall acknowledged' in out['markdown']


def test_impact_rendered():
    out = render_postmortem({'impact': {'orders_lost': 12,
                                          'duration_minutes': 30}})
    assert 'orders_lost: 12' in out['markdown']


def test_root_causes_drive_followup_when_no_llm():
    out = render_postmortem({'root_causes': ['db disk full',
                                                'missing alert']})
    assert 'Add monitoring/alert for: db disk full' in out['markdown']
    assert out['llm_used'] is False


def test_llm_summary_used_when_available():
    out = render_postmortem({'title': 'X'},
                              llm_call=lambda p: '- do A\n- do B\n- do C')
    assert '- do A' in out['markdown']
    assert out['llm_used'] is True


def test_llm_failure_does_not_crash():
    def bad(p):
        raise RuntimeError('llm down')
    out = render_postmortem({'title': 'X'}, llm_call=bad)
    assert out['llm_used'] is False
    assert 'LLM unavailable' in out['markdown']


def test_actions_taken_listed():
    out = render_postmortem({'actions_taken': ['reverted commit',
                                                  'added retry']})
    assert 'reverted commit' in out['markdown']
    assert 'added retry' in out['markdown']


def test_severity_present():
    out = render_postmortem({'severity': 'critical'})
    assert '**Severity**: critical' in out['markdown']
