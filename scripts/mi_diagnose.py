"""F33 — MI 模块一键自助诊断 CLI。

输出当前流水线全景：
- 快照数 / 最新快照 / 最新机会数
- 最近 7 天日报归档列表
- 长周期 trend 历史（最新一条 + 是否触发 F27 持续衰退）
- 最近一次告警时间（按 type）
- 屏蔽 SKU 数（生效中）
- 品类规则文件状态（mtime + 条目数）
- F32 自检结果（与调度器一致）

用途：现场排障 / cron 巡检 / 上线后验证。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

REPORTS = PROJECT_ROOT / "reports"


def _section(title: str) -> None:
    print("\n" + "-" * 60)
    print(f"  {title}")
    print("-" * 60)


def build_report(project_root: Path | None = None, now: datetime | None = None) -> dict:
    root = project_root or PROJECT_ROOT
    reports = root / "reports"
    ts = now or datetime.now()
    report = {
        "generated_at": ts.isoformat(),
        "project_root": str(root),
        "reports_dir": str(reports),
        "snapshots": {
            "count": 0,
            "latest_name": None,
            "latest_opportunity_count": 0,
            "latest_by_category_count": 0,
            "top_category": None,
        },
        "digests": {
            "count_last_7d": 0,
            "latest_names": [],
        },
        "trend": {
            "exists": False,
            "entry_count": 0,
            "latest_date": None,
            "latest_trend_label": None,
            "recent": [],
            "persistent_falling": False,
        },
        "alerts": {
            "exists": False,
            "type_count": 0,
            "latest_last_sent_at": None,
            "by_type": {},
        },
        "blacklist": {
            "exists": False,
            "active_count": 0,
            "reasons": {},
        },
        "category_rules": {
            "exists": False,
            "category_count": 0,
            "fallback": None,
            "mtime": None,
        },
        "self_check": {
            "ok": False,
            "status": "fail",
            "severity": "high",
            "issues": ["自检未执行"],
            "details": {},
        },
    }

    if not reports.exists():
        report["self_check"] = {
            "ok": False,
            "status": "fail",
            "severity": "high",
            "issues": ["reports/ 目录不存在"],
            "details": {},
        }
        return report

    snaps = sorted(reports.glob("mi_opportunities_*.json"),
                   key=lambda path: path.stat().st_mtime, reverse=True)
    report["snapshots"]["count"] = len(snaps)
    if snaps:
        latest = snaps[0]
        report["snapshots"]["latest_name"] = latest.name
        try:
            data = json.loads(latest.read_text(encoding="utf-8"))
            opps = data.get("opportunities") or []
            by_cat = data.get("by_category") or []
            report["snapshots"]["latest_opportunity_count"] = len(opps)
            report["snapshots"]["latest_by_category_count"] = len(by_cat)
            if by_cat:
                top = by_cat[0]
                report["snapshots"]["top_category"] = {
                    "category": top.get("category"),
                    "count": top.get("count"),
                    "avg_score": top.get("avg_score"),
                }
        except Exception as exc:
            report["snapshots"]["read_error"] = str(exc)

    digests = sorted(reports.glob("mi_digest_*.html"),
                     key=lambda path: path.stat().st_mtime, reverse=True)
    seven_days_ago = ts - timedelta(days=7)
    report["digests"]["count_last_7d"] = sum(
        1 for digest in digests
        if datetime.fromtimestamp(digest.stat().st_mtime) >= seven_days_ago
    )
    report["digests"]["latest_names"] = [digest.name for digest in digests[:7]]

    trend_path = reports / "mi_long_window_history.json"
    report["trend"]["exists"] = trend_path.exists()
    if trend_path.exists():
        try:
            history = json.loads(trend_path.read_text(encoding="utf-8"))
            if isinstance(history, list):
                report["trend"]["entry_count"] = len(history)
                report["trend"]["recent"] = history[-7:]
                if history:
                    latest = history[-1]
                    report["trend"]["latest_date"] = latest.get("date")
                    report["trend"]["latest_trend_label"] = latest.get("trend_label")
                    recent = history[-3:]
                    report["trend"]["persistent_falling"] = (
                        len(recent) >= 3
                        and all(isinstance(entry, dict) and entry.get("trend_label") == "falling"
                                for entry in recent)
                    )
            else:
                report["trend"]["read_error"] = "trend 文件结构不是 list"
        except Exception as exc:
            report["trend"]["read_error"] = str(exc)

    alerts_path = reports / "mi_alerts_state.json"
    report["alerts"]["exists"] = alerts_path.exists()
    if alerts_path.exists():
        try:
            state = json.loads(alerts_path.read_text(encoding="utf-8"))
            if isinstance(state, dict):
                report["alerts"]["type_count"] = len(state)
                report["alerts"]["by_type"] = state
                last_sent_values = [
                    info.get("last_sent_at") for info in state.values()
                    if isinstance(info, dict) and info.get("last_sent_at")
                ]
                report["alerts"]["latest_last_sent_at"] = max(last_sent_values) if last_sent_values else None
        except Exception as exc:
            report["alerts"]["read_error"] = str(exc)

    blacklist_path = reports / "mi_blacklist.json"
    report["blacklist"]["exists"] = blacklist_path.exists()
    if blacklist_path.exists():
        try:
            blacklist = json.loads(blacklist_path.read_text(encoding="utf-8"))
            if isinstance(blacklist, dict):
                report["blacklist"]["active_count"] = len(blacklist)
                reasons = {}
                for value in blacklist.values():
                    if isinstance(value, dict):
                        reason = value.get("reason", "?")
                        reasons[reason] = reasons.get(reason, 0) + 1
                report["blacklist"]["reasons"] = reasons
        except Exception as exc:
            report["blacklist"]["read_error"] = str(exc)

    rules_path = root / "mi_categories.json"
    report["category_rules"]["exists"] = rules_path.exists()
    if rules_path.exists():
        try:
            data = json.loads(rules_path.read_text(encoding="utf-8"))
            categories = data.get("categories") or []
            report["category_rules"]["category_count"] = len(categories)
            report["category_rules"]["fallback"] = data.get("fallback")
            report["category_rules"]["mtime"] = datetime.fromtimestamp(
                rules_path.stat().st_mtime
            ).isoformat()
        except Exception as exc:
            report["category_rules"]["read_error"] = str(exc)

    try:
        from scheduler_daemon import check_mi_pipeline_health
        report["self_check"] = check_mi_pipeline_health(reports_dir=reports, now=ts)
    except Exception as exc:
        report["self_check"] = {
            "ok": False,
            "status": "fail",
            "severity": "high",
            "issues": [f"调用 scheduler_daemon.check_mi_pipeline_health 失败: {exc}"],
            "details": {},
        }

    return report


def render_report(report: dict) -> str:
    lines = [
        f"MI Diagnose · {datetime.fromisoformat(report['generated_at']):%Y-%m-%d %H:%M:%S}",
        f"项目根: {report['project_root']}",
    ]

    _append = lines.append

    _append("\n" + "-" * 60)
    _append("  快照（mi_opportunities_*.json）")
    _append("-" * 60)
    _append(f"  快照总数: {report['snapshots']['count']}")
    if report['snapshots']['latest_name']:
        _append(f"  最新: {report['snapshots']['latest_name']}")
        _append(f"    机会数: {report['snapshots']['latest_opportunity_count']}")
        _append(f"    by_category: {report['snapshots']['latest_by_category_count']} 类（F30）")
        top = report['snapshots'].get('top_category')
        if top:
            _append(
                f"    Top 品类: {top.get('category')} (count={top.get('count')}, avg_score={top.get('avg_score')})"
            )
    elif report['snapshots'].get('read_error'):
        _append(f"  读取失败: {report['snapshots']['read_error']}")

    _append("\n" + "-" * 60)
    _append("  日报归档（mi_digest_*.html）")
    _append("-" * 60)
    _append(f"  最近 7 天归档数: {report['digests']['count_last_7d']}")
    for name in report['digests']['latest_names']:
        _append(f"    · {name}")

    _append("\n" + "-" * 60)
    _append("  长周期 trend 历史（F27）")
    _append("-" * 60)
    if not report['trend']['exists']:
        _append("  文件不存在")
    elif report['trend'].get('read_error'):
        _append(f"  读取失败: {report['trend']['read_error']}")
    else:
        _append(f"  条目数: {report['trend']['entry_count']}")
        _append(f"  最新日期: {report['trend']['latest_date']}")
        _append(f"  最新趋势: {report['trend']['latest_trend_label']}")
        if report['trend']['persistent_falling']:
            _append("  已触发持续衰退条件（最近 3 天均为 falling）")

    _append("\n" + "-" * 60)
    _append("  告警状态（F21 抑制表）")
    _append("-" * 60)
    if not report['alerts']['exists']:
        _append("  无告警记录")
    elif report['alerts'].get('read_error'):
        _append(f"  读取失败: {report['alerts']['read_error']}")
    else:
        _append(f"  告警类型数: {report['alerts']['type_count']}")
        _append(f"  最近发送时间: {report['alerts']['latest_last_sent_at']}")
        for alert_type, info in sorted(report['alerts']['by_type'].items()):
            if isinstance(info, dict):
                _append(
                    f"    · {alert_type}: last_sent_at={info.get('last_sent_at')} severity={info.get('severity')}"
                )

    _append("\n" + "-" * 60)
    _append("  屏蔽名单（F10/F14）")
    _append("-" * 60)
    if not report['blacklist']['exists']:
        _append("  无")
    elif report['blacklist'].get('read_error'):
        _append(f"  读取失败: {report['blacklist']['read_error']}")
    else:
        _append(f"  生效中: {report['blacklist']['active_count']} 条")
        for reason, count in sorted(report['blacklist']['reasons'].items(), key=lambda item: (-item[1], item[0])):
            _append(f"    · {reason}: {count}")

    _append("\n" + "-" * 60)
    _append("  品类规则（F22 - mi_categories.json）")
    _append("-" * 60)
    if not report['category_rules']['exists']:
        _append("  文件不存在，将回退到内置默认")
    elif report['category_rules'].get('read_error'):
        _append(f"  读取失败: {report['category_rules']['read_error']}")
    else:
        _append(
            f"  条目: {report['category_rules']['category_count']}  fallback={report['category_rules']['fallback']}  最后修改: {report['category_rules']['mtime']}"
        )

    _append("\n" + "-" * 60)
    _append("  F32 自检（同步调用 scheduler_daemon.check_mi_pipeline_health）")
    _append("-" * 60)
    _append(f"  ok = {report['self_check']['ok']}")
    _append(f"  status = {report['self_check'].get('status')}")
    _append(f"  severity = {report['self_check'].get('severity')}")
    if report['self_check']['issues']:
        _append("  问题:")
        for issue in report['self_check']['issues']:
            _append(f"    · {issue}")
    if report['self_check']['details']:
        _append("  详情:")
        for key, value in report['self_check']['details'].items():
            _append(f"    · {key} = {value}")

    _append("\n" + "=" * 60)
    _append("  完成")
    _append("=" * 60)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MI 流水线自助诊断")
    parser.add_argument("--json", action="store_true", help="以 JSON 形式输出诊断结果")
    args = parser.parse_args(argv)

    report = build_report()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
