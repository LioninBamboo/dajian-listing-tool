"""S120 — Prometheus 文本格式指标导出.

不依赖 prometheus_client; 纯字符串构造符合 0.0.4 文本格式.
支持 counter/gauge/histogram (简化), labels.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Tuple

_VALID_TYPES = {'counter', 'gauge', 'histogram', 'summary', 'untyped'}


def _escape_label_value(v: str) -> str:
    return (str(v).replace('\\', '\\\\')
                  .replace('\n', '\\n')
                  .replace('"', '\\"'))


def _format_labels(labels: Dict[str, Any]) -> str:
    if not labels:
        return ''
    parts = [f'{k}="{_escape_label_value(v)}"'
             for k, v in sorted(labels.items())]
    return '{' + ','.join(parts) + '}'


def format_metric_line(name: str,
                        value: float,
                        labels: Dict[str, Any] = None) -> str:
    return f'{name}{_format_labels(labels or {})} {float(value)}'


def format_metric_block(name: str,
                         help_text: str,
                         metric_type: str,
                         samples: Iterable[Tuple[Dict[str, Any], float]],
                         ) -> str:
    if metric_type not in _VALID_TYPES:
        raise ValueError(f'invalid metric type: {metric_type}')
    lines = [
        f'# HELP {name} {help_text}',
        f'# TYPE {name} {metric_type}',
    ]
    for labels, value in samples:
        lines.append(format_metric_line(name, value, labels))
    return '\n'.join(lines) + '\n'


class MetricsRegistry:
    """轻量进程内 metrics 注册表."""

    def __init__(self) -> None:
        # name -> {help, type, samples: dict[frozenset(labels.items()), value]}
        self._metrics: Dict[str, Dict[str, Any]] = {}

    def _ensure(self, name: str, help_text: str, metric_type: str) -> None:
        if name not in self._metrics:
            if metric_type not in _VALID_TYPES:
                raise ValueError(f'invalid metric type: {metric_type}')
            self._metrics[name] = {
                'help': help_text, 'type': metric_type, 'samples': {}}

    def _key(self, labels: Dict[str, Any]) -> frozenset:
        return frozenset((labels or {}).items())

    def set_gauge(self, name: str, value: float,
                   labels: Dict[str, Any] = None,
                   help_text: str = '') -> None:
        self._ensure(name, help_text, 'gauge')
        self._metrics[name]['samples'][self._key(labels)] = float(value)

    def inc_counter(self, name: str, amount: float = 1.0,
                     labels: Dict[str, Any] = None,
                     help_text: str = '') -> None:
        if amount < 0:
            raise ValueError('counter cannot decrease')
        self._ensure(name, help_text, 'counter')
        k = self._key(labels)
        cur = self._metrics[name]['samples'].get(k, 0.0)
        self._metrics[name]['samples'][k] = cur + float(amount)

    def get(self, name: str, labels: Dict[str, Any] = None) -> float:
        m = self._metrics.get(name)
        if not m:
            return 0.0
        return m['samples'].get(self._key(labels), 0.0)

    def render(self) -> str:
        out: List[str] = []
        for name in sorted(self._metrics.keys()):
            m = self._metrics[name]
            samples = [(dict(k), v) for k, v in m['samples'].items()]
            out.append(format_metric_block(
                name, m['help'] or name, m['type'], samples))
        return '\n'.join(out)

    def clear(self) -> None:
        self._metrics.clear()
