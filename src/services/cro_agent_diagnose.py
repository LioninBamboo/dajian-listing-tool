"""S87 — 多步推理 RCA agent.

不引入 LangGraph; 用纯 Python 流水线: collect_signals → hypothesise →
verify → conclude. 每步可注入数据 fetcher / llm_call.
所有步骤捕获异常, 失败步骤标 status='failed' 不阻断后续.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from src.services.cro_alert_rca import diagnose_features


def _step(name: str, fn: Callable[[], Any]) -> Dict[str, Any]:
    try:
        out = fn()
        return {'step': name, 'status': 'ok', 'output': out, 'error': None}
    except Exception as e:
        return {'step': name, 'status': 'failed', 'output': None,
                'error': repr(e)}


def diagnose_agent(sku: str,
                   *,
                   features_fetcher: Optional[Callable[[str],
                                                        Dict[str, Any]]] = None,
                   verify_fetcher: Optional[Callable[[str, str], bool]] = None,
                   llm_call: Optional[Callable[[str], str]] = None,
                   ) -> Dict[str, Any]:
    """完整推理链, 输出 trace + final."""
    trace: List[Dict[str, Any]] = []

    # Step 1 collect signals
    features: Dict[str, Any] = {}
    if features_fetcher is not None:
        s1 = _step('collect_signals', lambda: features_fetcher(sku))
        trace.append(s1)
        if s1['status'] == 'ok' and isinstance(s1['output'], dict):
            features = s1['output']
    else:
        trace.append({'step': 'collect_signals', 'status': 'skipped',
                      'output': None, 'error': None})

    # Step 2 hypothesise via rule RCA
    s2 = _step('hypothesise', lambda: diagnose_features(features))
    trace.append(s2)
    hypothesis = (s2['output'] or {}).get('root_cause_hypothesis') \
        if isinstance(s2['output'], dict) else None
    suggested = (s2['output'] or {}).get('suggested_action') \
        if isinstance(s2['output'], dict) else None

    # Step 3 verify (可选)
    verified = None
    if verify_fetcher is not None and hypothesis:
        s3 = _step('verify', lambda: bool(verify_fetcher(sku, hypothesis)))
        trace.append(s3)
        if s3['status'] == 'ok':
            verified = s3['output']
    else:
        trace.append({'step': 'verify', 'status': 'skipped',
                      'output': None, 'error': None})

    # Step 4 conclude — 可选 LLM 复述; 否则模板拼装
    if llm_call is not None and hypothesis:
        prompt = (f'SKU={sku}\n根因假说: {hypothesis}\n'
                  f'建议: {suggested}\n请用一句话向运营总结.')
        s4 = _step('conclude_llm', lambda: llm_call(prompt))
        trace.append(s4)
        narrative = s4['output'] if s4['status'] == 'ok' else None
    else:
        narrative = (f'SKU {sku}: {hypothesis or "无明显模式"}; '
                     f'建议 {suggested or "manual_review"}')
        trace.append({'step': 'conclude_template', 'status': 'ok',
                      'output': narrative, 'error': None})

    return {
        'sku': sku,
        'features': features,
        'hypothesis': hypothesis,
        'suggested_action': suggested,
        'verified': verified,
        'narrative': narrative,
        'trace': trace,
        'success': all(t['status'] in ('ok', 'skipped') for t in trace),
    }
