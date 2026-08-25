#!/usr/bin/env python3
"""
每日自动任务脚本

功能:
1. 处理所有 COLLECTED 状态产品 → 生成AI内容 → 更新为 READY
2. 库存同步检查
3. 智能调价
4. 记录日志

使用:
  python daily_tasks.py                    # 执行所有任务
  python daily_tasks.py --analyze-only     # 仅分析采集产品
  python daily_tasks.py --sync-only        # 仅同步库存
"""
import json
import os
import sys
import io

# Ensure UTF-8 output (avoid emoji crashes on Chinese Windows scheduled tasks)
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if not _stream:
        continue
    try:
        encoding = (_stream.encoding or "").lower()
        if hasattr(_stream, "reconfigure"):
            if encoding != "utf-8":
                _stream.reconfigure(encoding="utf-8", errors="replace")
        elif hasattr(_stream, "buffer") and encoding != "utf-8":
            setattr(
                sys,
                _stream_name,
                io.TextIOWrapper(_stream.buffer, encoding="utf-8", errors="replace", line_buffering=True),
            )
    except Exception:
        pass

import argparse
import logging
from datetime import datetime, timedelta, timezone
from html import escape as html_escape
from pathlib import Path

from src.utils.report_images import build_thumbnail_img_html, normalize_thumbnail_url
from src.utils.email_sender import send_email  # 模块级，供 _send_mi_alert_email / _send_mi_daily_digest 使用
from src.services.ebay_category_matcher import create_category_matcher
from src.utils.mi_draft_origin import (
    MI_DRAFT_ORIGIN,
    apply_mi_draft_origin,
    append_mi_draft_log,
)
from src.db.database_safety import assert_runtime_not_in_maintenance, validate_runtime_database
from src.utils.task_result_status import classify_daily_task_outcome
from src.utils.report_retention import cleanup_runtime_artifacts
from src.utils.report_retention import purge_named_artifacts
from src.utils.inventory_audit_contract import (
    AUDIT_SCOPE_FULL_OOS,
    AUDIT_SCOPE_INCREMENTAL,
    audit_scope_label,
    empty_inventory_audit,
    summarize_incremental_sync_results,
)
from src.utils.store_profile import get_store_profile
from src.utils.smart_reprice_schedule import (
    SMART_REPRICE_RUN_DAY_LABELS,
    should_run_smart_reprice,
)

UTC = getattr(datetime, "UTC", timezone.utc)
from src.utils.mi_opportunity_flow import auto_prepare_mi_opportunity_drafts, empty_auto_prepare_result

# 设置项目路径
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

# 确保工作目录为项目根目录
os.chdir(PROJECT_ROOT)
CATEGORY_MATCHER = create_category_matcher(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))

# 配置日志
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

log_file = LOG_DIR / f"daily_tasks_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def run_report_retention_cleanup() -> dict:
    """清理已过期的白名单报告/日志，不阻断每日主任务。"""
    try:
        summary = cleanup_runtime_artifacts(
            project_root=PROJECT_ROOT,
            dry_run=False,
        )
        logger.info(
            "运行期产物清理完成: 候选 %s, 删除 %s, 失败 %s",
            summary.get("candidate_count", 0),
            summary.get("removed_count", 0),
            summary.get("error_count", 0),
        )
        for error in summary.get("errors", []):
            logger.warning("运行期产物清理失败: %s (%s)", error.get("path"), error.get("error"))
        return summary
    except Exception as exc:
        logger.exception("运行期产物清理异常，继续执行每日主任务")
        return {
            "dry_run": False,
            "candidate_count": 0,
            "removed_count": 0,
            "error_count": 1,
            "errors": [{"path": "", "error": str(exc)}],
            "candidates": [],
            "removed": [],
        }


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _open_listing_qc_connection():
    """Open the local connection used by the semantic pre-publish guard."""
    import sqlite3

    return sqlite3.connect(str(PROJECT_ROOT / "ebay_collection.db"), timeout=30)


def analyze_collected_products(
    *,
    sku_filter: list[str] | tuple[str, ...] | set[str] | None = None,
    eligible_statuses: tuple[str, ...] = ("COLLECTED",),
    draft_origin: str | None = None,
) -> dict:
    """分析指定状态的产品并生成 READY 草稿。"""
    from src.db.collection_db import SessionLocal
    from src.db.collection_models import CollectedProduct
    from src.services.pricing_engine import PricingEngine
    from sqlalchemy.orm.attributes import flag_modified
    from qwen_optimizer import QwenOptimizer, optimize_product_full_with_timeout
    from src.utils.listing_quality_gate import normalize_generated_listing
    from src.services.listing_qc import run_listing_qc

    normalized_statuses = tuple(
        str(status).strip().upper() for status in eligible_statuses if str(status).strip()
    ) or ("COLLECTED",)
    normalized_skus: list[str] = []
    if sku_filter:
        seen_skus: set[str] = set()
        for sku in sku_filter:
            text = str(sku or "").strip()
            if not text or text in seen_skus:
                continue
            seen_skus.add(text)
            normalized_skus.append(text)
    
    logger.info("="*60)
    logger.info(f"开始处理 {', '.join(normalized_statuses)} 状态产品")
    logger.info("="*60)
    
    db = SessionLocal()
    fact_sheet_conn = None
    try:
        QWEN_KEY = os.getenv("QWEN_API_KEY")
        
        if not QWEN_KEY:
            logger.error("QWEN_API_KEY 未设置")
            return {'success': 0, 'failed': 0, 'error': 'QWEN_API_KEY 未设置'}

        try:
            fact_sheet_conn = _open_listing_qc_connection()
        except Exception as fact_conn_error:
            # The shared gate will classify candidates as unavailable rather
            # than silently skipping the semantic check.
            logger.warning(f"FactSheet connection unavailable: {fact_conn_error}")
        
        qwen = QwenOptimizer(api_key=QWEN_KEY)
        
        query = db.query(CollectedProduct).filter(CollectedProduct.status.in_(normalized_statuses))
        if normalized_skus:
            query = query.filter(CollectedProduct.sku.in_(normalized_skus))
        products = query.all()
        
        if not products:
            logger.info(f"没有需要处理的 {', '.join(normalized_statuses)} 状态产品")
            return {
                'success': 0,
                'failed': 0,
                'requested': len(normalized_skus),
                'matched': 0,
                'prepared_skus': [],
            }
        
        logger.info(f"发现 {len(products)} 个待处理产品")
        
        success = 0
        failed = 0
        prepared_skus: list[str] = []
        
        for i, product in enumerate(products, 1):
            logger.info(f"[{i}/{len(products)}] 处理 {product.sku}...")
            
            try:
                # 1. 计算价格
                specs = product.specs or {}
                attributes = product.attributes or {}
                is_oversize = 'Dimensions' in specs or 'Dimensions' in attributes
                
                dajian_costs = PricingEngine.calculate_dajian_cost(
                    product_price=product.price,
                    shipping_cost=product.shipping,
                    is_oversize=is_oversize
                )
                
                safe_price = PricingEngine.calculate_selling_price(
                    dajian_costs["total_dajian_cost"], 0.15
                )
                
                # 2. AI 优化 (with market intelligence)
                market_intel = None
                try:
                    market_intel = qwen.fetch_market_intelligence(product.title)
                    if market_intel:
                        logger.info(f"  📊 Market intel: {market_intel.get('total_listings', 0)} listings")
                except Exception as mi_err:
                    logger.warning(f"  ⚠️ Market intel failed: {mi_err}")
                
                max_retries = 2
                previous_errors = None
                for attempt in range(max_retries):
                    opt_data = optimize_product_full_with_timeout(
                        api_key=QWEN_KEY,
                        original_title=product.title,
                        original_description=product.description or '',
                        attributes=attributes,
                        specs=specs,
                        images=product.images or [],
                        market_intel=market_intel,
                        previous_errors=previous_errors
                    )
                    if draft_origin == MI_DRAFT_ORIGIN:
                        opt_data = apply_mi_draft_origin(
                            opt_data,
                            detected_at=_utcnow_naive().isoformat(),
                        )
                    opt_data = normalize_generated_listing(
                        opt_data,
                        source_title=product.title,
                        source_description=product.description or '',
                        attributes=attributes,
                        specs=specs,
                        images=product.images or [],
                        videos=product.videos or [],
                        category_matcher=CATEGORY_MATCHER,
                    )
                    qc_result = run_listing_qc(
                        sku=product.sku,
                        candidate=opt_data,
                        source_title=product.title,
                        source_description=product.description or '',
                        source_attributes=attributes,
                        source_specs=specs,
                        images=product.images or [],
                        videos=product.videos or [],
                        category_matcher=CATEGORY_MATCHER,
                        fact_sheet_conn=fact_sheet_conn,
                    )
                    blockers = qc_result["blockers"]
                    
                    if not blockers:
                        break  # Passed gate
                        
                    is_pure_missing_measurement = all("missing measurement aspect" in b for b in blockers)
                    if is_pure_missing_measurement or attempt == max_retries - 1:
                        raise ValueError("Listing QC failed: " + "; ".join(blockers[:8]))
                        
                    logger.warning(f"  ⚠️ Quality gate failed, retrying ({attempt+1}/{max_retries-1}). Errors: {blockers}")
                    previous_errors = blockers
                
                # 3. 保存
                # Preserve authoritative vehicle fitment across regeneration.
                # motorsCompatibility (structured Year/Make/Model from the source
                # / GIGA) is NOT LLM-generated copy — the optimizer never
                # re-derives it, so without this a re-analysis silently drops all
                # Motors fitment. No-op for furniture (prior has none).
                prior_opt = product.optimization if isinstance(product.optimization, dict) else {}
                prior_mc = prior_opt.get("motorsCompatibility") or {}
                prior_cp = prior_mc.get("compatibleProducts") or []
                new_cp = (opt_data.get("motorsCompatibility") or {}).get("compatibleProducts") or []
                # The regenerated analysis re-derives fitment from LLM-rewritten
                # text and routinely loses the authoritative structured entries
                # (77 Year/Make/Model from GIGA -> 0). Keep the richer prior set.
                if len(prior_cp) > len(new_cp):
                    opt_data["motorsCompatibility"] = prior_mc
                opt_data["_listing_qc"] = qc_result
                product.optimization = opt_data
                product.cost_breakdown = dajian_costs
                product.suggested_price = safe_price['selling_price']
                product.status = "READY"
                product.logs = (product.logs or []) + [
                    f"Auto-analyzed at {_utcnow_naive().isoformat()}"
                ]
                if draft_origin == MI_DRAFT_ORIGIN:
                    product.logs = append_mi_draft_log(
                        product.logs,
                        detected_at=_utcnow_naive().isoformat(),
                    )
                
                flag_modified(product, 'optimization')
                flag_modified(product, 'cost_breakdown')
                flag_modified(product, 'logs')
                
                db.commit()
                logger.info(f"  ✅ 完成")
                success += 1
                prepared_skus.append(product.sku)
                
            except Exception as e:
                logger.error(f"  ❌ 失败: {e}")
                failed += 1
                db.rollback()
                product = db.merge(product)
                product.status = "ERROR"
                product.logs = (product.logs or []) + [
                    f"Auto-analysis error at {_utcnow_naive().isoformat()}: {e}"
                ]
                flag_modified(product, 'logs')
                db.commit()
        
        logger.info(f"\n分析完成: 成功 {success}, 失败 {failed}")
        return {
            'success': success,
            'failed': failed,
            'requested': len(normalized_skus) if normalized_skus else len(products),
            'matched': len(products),
            'prepared_skus': prepared_skus,
        }
    finally:
        if fact_sheet_conn is not None:
            fact_sheet_conn.close()
        db.close()


def sync_inventory() -> dict:
    """同步库存"""
    from src.plugins.inventory_sync.sync_service import InventorySyncService
    
    logger.info("="*60)
    logger.info("开始库存同步")
    logger.info("="*60)
    
    try:
        service = InventorySyncService()
        scope_count = len(service.get_published_products())

        if not service.test_dajian_connection():
            reason = service.last_dajian_connection_error
            error_msg = f"大建 API 连接失败: {reason}" if reason else "大建 API 连接失败"
            logger.error(error_msg)
            return {
                **empty_inventory_audit(
                    AUDIT_SCOPE_INCREMENTAL,
                    scope_count=scope_count,
                    error_count=1,
                    error=error_msg,
                ),
                'error': error_msg,
                'checked': 0,
                'errors': 1,
                'out_of_stock': [],
                'price_changed': [],
                'data_missing': [],
                'no_change': 0,
            }
        
        # 执行同步
        results = service.sync_all(skip_ebay_check=True, skip_synced_today=True)
        summary = summarize_incremental_sync_results(
            results,
            scope_count=getattr(service, 'last_sync_scope_count', scope_count),
            skipped_count=getattr(service, 'last_sync_skipped_count', 0),
        )
        
        out_of_stock = [r.sku for r in results if r.action == 'out_of_stock']
        # 保留完整价格变动详情（旧价/新价/消息）
        price_changed_details = [
            {
                'sku': r.sku,
                'old_value': r.old_value or '',
                'new_value': r.new_value or '',
                'message': r.message or ''
            }
            for r in results if r.action == 'price_updated'
        ]
        # 幽灵产品 / 数据异常
        data_missing = [r.sku for r in results if r.action == 'data_missing']
        no_change = sum(1 for r in results if r.action == 'no_change')
        errors = summary['error_count']
        
        logger.info(f"同步完成:")
        logger.info(f"  已检查: {len(results)}")
        logger.info(f"  缺货: {len(out_of_stock)}")
        logger.info(f"  价格变动: {len(price_changed_details)}")
        logger.info(f"  数据异常: {len(data_missing)}")
        logger.info(f"  无变化: {no_change}")
        logger.info(f"  错误: {errors}")
        
        if out_of_stock:
            logger.warning(f"  缺货SKU: {out_of_stock}")
        if data_missing:
            logger.warning(f"  ⚠️ 数据异常SKU (有库存但无价格): {data_missing}")
        if price_changed_details:
            for pc in price_changed_details:
                logger.info(f"  💰 {pc['sku']}: {pc['old_value']} → {pc['new_value']}")
        
        return {
            **summary,
            # Compatibility aliases for existing scheduler/result consumers.
            'checked': summary['checked_count'],
            'out_of_stock': out_of_stock,
            'price_changed': price_changed_details,
            'data_missing': data_missing,
            'no_change': no_change,
            'errors': errors,
        }
        
    except Exception as e:
        logger.error(f"库存同步失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        error_msg = str(e)
        return {
            **empty_inventory_audit(
                AUDIT_SCOPE_INCREMENTAL,
                error_count=1,
                error=error_msg,
            ),
            'error': error_msg,
            'checked': 0,
            'errors': 1,
            'out_of_stock': [],
            'price_changed': [],
            'data_missing': [],
            'no_change': 0,
        }


def run_ghost_oos_recovery(auto_fix: bool = True) -> dict:
    """扫描 eBay qty=0 但大建有货的幽灵缺货，并在日间主任务里先行恢复。"""
    logger.info("=" * 60)
    logger.info("开始幽灵缺货恢复检查")
    logger.info("=" * 60)

    try:
        sys.path.insert(0, str(PROJECT_ROOT / 'scripts'))
        from sales_health_check import SalesHealthChecker

        checker = SalesHealthChecker()
        audit = checker._check_quantity_integrity(auto_fix=auto_fix)
        if not isinstance(audit, dict):
            audit = getattr(checker, 'quantity_audit', None)
        if not isinstance(audit, dict):
            audit = empty_inventory_audit(
                AUDIT_SCOPE_FULL_OOS,
                error_count=1,
                error='全量缺货审核未返回标准结果',
            )
        restocked = audit.get('restocked_items', [])

        logger.info("幽灵缺货恢复检查完成:")
        logger.info(f"  eBay 库存为0: {audit.get('qty_zero_count', 0)}")
        logger.info(f"  供应商无货: {audit.get('supplier_oos_count', 0)}")
        logger.info(f"  已恢复: {audit.get('restocked_count', 0)}")
        logger.info(f"  错误: {audit.get('error_count', 0)}")
        if restocked:
            logger.warning(f"  幽灵缺货 SKU: {[item.get('sku') for item in restocked]}")

        return {
            **audit,
            'status': 'ok',
            'auto_fix': auto_fix,
            'qty_zero_restocked': restocked,
        }
    except Exception as e:
        logger.error(f"幽灵缺货恢复检查失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {
            **empty_inventory_audit(
                AUDIT_SCOPE_FULL_OOS,
                error_count=1,
                error=str(e),
            ),
            'status': 'error',
            'error': str(e),
            'qty_zero_restocked': [],
        }


def run_smart_reprice(market_mode: str | None = None) -> dict:
    """将智能调价纳入每日主流程，避免再依赖单独周任务报告。"""
    logger.info("=" * 60)
    logger.info("开始智能调价")
    logger.info("=" * 60)

    try:
        sys.path.insert(0, str(PROJECT_ROOT / 'scripts'))
        from batch_smart_reprice import run_batch_reprice

        send_reprice_email = os.getenv("SMART_REPRICE_EMAIL_ENABLED", "1").strip().lower() not in {"0", "false", "no"}

        report = run_batch_reprice(
            dry_run=False,
            send_email=send_reprice_email,
            market_mode=market_mode,
        )
        if not isinstance(report, dict):
            raise RuntimeError("智能调价返回了未知结果格式")

        summary = report.get('summary', {}) or {}
        logger.info("智能调价完成:")
        logger.info(f"  检查: {summary.get('total', 0)}")
        logger.info(f"  调价: {summary.get('price_changes', 0)}")
        logger.info(f"  涨价: {summary.get('price_up', 0)}")
        logger.info(f"  降价: {summary.get('price_down', 0)}")
        logger.info(f"  错误: {summary.get('errors', 0)}")
        logger.info(f"  独立邮件: {'开启' if send_reprice_email else '关闭'}")
        if report.get('path'):
            logger.info(f"  报告: {report.get('path')}")
        return report
    except Exception as e:
        logger.error(f"智能调价失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {
            'status': 'error',
            'error': str(e),
            'changed_rows': [],
            'summary': {},
        }


def generate_terapeak_report() -> dict:
    """生成每日 Terapeak 市场调研报告"""
    logger.info("="*60)
    logger.info("生成 Terapeak 市场调研报告")
    logger.info("="*60)
    
    try:
        from scripts.daily_terapeak_report import DailyTerapeakReport
        
        reporter = DailyTerapeakReport()
        report_data = reporter.generate_report()
        html_path = reporter.save_report()
        
        # 尝试发送邮件
        reporter.send_email(html_path)
        
        summary = report_data.get('summary', {})
        logger.info(f"报告已生成: {html_path}")
        logger.info(f"  品类: {summary.get('categories_with_data', 0)}")
        logger.info(f"  高利润产品: {summary.get('high_margin_products', 0)}")
        
        return {
            'html_path': str(html_path),
            'categories': summary.get('categories_with_data', 0),
            'opportunities': summary.get('inventory_products_with_opportunity', 0),
            'high_margin': summary.get('high_margin_products', 0)
        }
    except Exception as e:
        logger.error(f"Terapeak 报告生成失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {'error': str(e)}


def run_listing_audit(
    auto_fix: bool = False,
    *,
    use_live: bool | None = None,
    record_clean_state: bool = False,
    ignore_clean_freeze: bool = False,
) -> dict:
    """运行刊登质量审计。

    默认每日任务使用 live 只读复核，并记录本地 clean freeze 元数据。
    只有显式 auto_fix=True 时，才允许写回 eBay live listing。
    """
    logger.info("=" * 60)
    logger.info("开始刊登质量审计 (Listing Quality Audit)")
    logger.info("=" * 60)

    try:
        import sqlite3
        from scripts.audit_fix_active_listings import (
            _fetch_live_listing_context,
            audit_single_product,
            build_audit_fingerprint,
            build_live_audit_payload,
            build_live_listing_opt_snapshot,
            build_source_audit_payload,
            fix_listing_on_ebay,
            get_quality_gate_meta,
            is_listing_frozen_clean,
            parse_json,
            persist_listing_audit_state,
            QUALITY_GATE_META_VERSION,
            QUALITY_GATE_RULESET_VERSION,
            split_transport_issues,
        )

        if use_live is None:
            use_live = auto_fix

        db_path = PROJECT_ROOT / "ebay_collection.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        rows = conn.execute(
            "SELECT sku, title, attributes, specs, optimization, description, "
            "images, videos, price, suggested_price, listing_id "
            "FROM collected_products WHERE status = 'PUBLISHED'"
        ).fetchall()

        total = len(rows)
        issues_found = 0
        critical_count = 0
        fixed_count = 0
        error_count = 0
        skipped_clean_frozen = 0
        clean_state_recorded = 0
        issue_skus = []
        consecutive_live_fetch_failures = 0
        network_failure_limit = max(
            1,
            int(os.getenv("LISTING_AUDIT_NETWORK_FAILURE_LIMIT", "3")),
        )
        aborted_network_failures = False
        attempted_live_fetches = 0
        audited_live_listings = 0
        skipped_incremental_scope = 0
        transport_failures = 0
        transport_issue_skus = []

        ebay_client = None
        if use_live or auto_fix:
            from src.clients.real_ebay_client import create_real_ebay_client
            ebay_client = create_real_ebay_client(
                os.getenv("EBAY_ENVIRONMENT", "PRODUCTION")
            )

        for row in rows:
            stored_opt = parse_json(row["optimization"])
            meta = get_quality_gate_meta(stored_opt)
            source_fingerprint = build_audit_fingerprint(
                build_source_audit_payload(
                    row["title"],
                    row["description"] or "",
                    row["attributes"],
                    row["specs"],
                    images_raw=row["images"],
                    videos_raw=row["videos"],
                )
            )
            audit_opt_raw = row["optimization"]
            live_inventory = None
            live_offer = None
            current_listing_id = row["listing_id"]
            preflight_issues = []
            should_audit_live = True
            if use_live and not auto_fix and not ignore_clean_freeze:
                should_audit_live = (
                    meta.get("version") != QUALITY_GATE_META_VERSION
                    or meta.get("ruleset_version") != QUALITY_GATE_RULESET_VERSION
                    or meta.get("status") != "clean"
                    or meta.get("source_fingerprint") != source_fingerprint
                )
                if not should_audit_live:
                    skipped_incremental_scope += 1
                    continue
            if use_live and ebay_client:
                attempted_live_fetches += 1
                audited_live_listings += 1
                try:
                    live_inventory, live_offer = _fetch_live_listing_context(
                        ebay_client,
                        row["sku"],
                        expected_listing_id=row["listing_id"],
                    )
                    consecutive_live_fetch_failures = 0
                    if not live_inventory:
                        preflight_issues.append({
                            "type": "live_inventory_missing",
                            "severity": "HIGH",
                            "detail": "Could not read live eBay inventory item for this SKU",
                        })
                    if not live_offer:
                        preflight_issues.append({
                            "type": "live_offer_missing",
                            "severity": "HIGH",
                            "detail": "Could not read live eBay offer/listingDescription for this SKU",
                        })
                    current_listing_id = (
                        ((live_offer or {}).get("listing") or {}).get("listingId")
                        or (live_offer or {}).get("listingId")
                        or row["listing_id"]
                    )
                    audit_opt_raw = json.dumps(
                        build_live_listing_opt_snapshot(
                            stored_opt,
                            inventory_item=live_inventory,
                            offer=live_offer,
                        ),
                        ensure_ascii=False,
                    )
                except Exception as exc:
                    error_count += 1
                    consecutive_live_fetch_failures += 1
                    logger.error(f"[AUDIT LIVE FETCH ERROR] {row['sku']}: {exc}")
                    preflight_issues.append({
                        "type": "live_fetch_failed",
                        "severity": "HIGH",
                        "detail": f"Failed to fetch live eBay listing data: {exc}",
                    })
                    if consecutive_live_fetch_failures >= network_failure_limit:
                        aborted_network_failures = True
                        logger.error(
                            "[AUDIT CIRCUIT BREAKER] 连续 %s 次 live 获取失败，"
                            "停止剩余 %s 个 SKU，避免日报被网络超时拖住",
                            consecutive_live_fetch_failures,
                            max(0, total - attempted_live_fetches),
                        )
                        break

            live_fingerprint = None
            if use_live:
                live_fingerprint = build_audit_fingerprint(
                    build_live_audit_payload(
                        audit_opt_raw,
                        listing_id=current_listing_id,
                        live_inventory=live_inventory,
                    )
                )
                if (
                    not preflight_issues
                    and
                    not ignore_clean_freeze
                    and is_listing_frozen_clean(
                        stored_opt,
                        source_fingerprint=source_fingerprint,
                        live_fingerprint=live_fingerprint,
                        listing_id=current_listing_id,
                    )
                ):
                    skipped_clean_frozen += 1
                    continue

            issues, fixes = audit_single_product(
                row["sku"],
                row["title"],
                row["attributes"],
                row["specs"],
                audit_opt_raw,
                row["description"] or "",
                ebay_client=ebay_client if (auto_fix or use_live) else None,
                images_raw=row["images"],
                videos_raw=row["videos"],
                live_inventory=live_inventory if use_live else None,
            )
            issues = preflight_issues + issues
            transport_issues, content_issues = split_transport_issues(issues)
            issue_types = sorted(
                {
                    str(issue.get("type", "")).strip()
                    for issue in issues
                    if str(issue.get("type", "")).strip()
                }
            )
            if not issues and not fixes:
                if use_live and record_clean_state:
                    if persist_listing_audit_state(
                        conn,
                        row["sku"],
                        state="clean",
                        source_fingerprint=source_fingerprint,
                        live_fingerprint=live_fingerprint,
                        listing_id=current_listing_id,
                    ):
                        clean_state_recorded += 1
                continue
            if issues:
                severity_order = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
                if content_issues:
                    issues_found += 1
                    severity_max = min(
                        severity_order.index(i["severity"])
                        for i in content_issues
                        if i.get("severity") in severity_order
                    )
                    if severity_max == 0:
                        critical_count += 1
                    issue_skus.append({
                        "sku": row["sku"],
                        "listing_id": current_listing_id,
                        "issues": len(content_issues),
                        "max_severity": severity_order[severity_max],
                    })
                if transport_issues:
                    transport_failures += 1
                    severity_max = min(
                        severity_order.index(i["severity"])
                        for i in transport_issues
                        if i.get("severity") in severity_order
                    )
                    transport_issue_skus.append({
                        "sku": row["sku"],
                        "listing_id": current_listing_id,
                        "issues": len(transport_issues),
                        "max_severity": severity_order[severity_max],
                    })

                if auto_fix and fixes and ebay_client:
                    import time
                    results_fix = fix_listing_on_ebay(
                        row["sku"],
                        dict(row),
                        fixes,
                        ebay_client,
                        conn,
                        base_opt_raw=audit_opt_raw,
                    )
                    if any("ERROR" in r for r in results_fix):
                        error_count += 1
                        logger.error(f"[AUDIT FIX ERROR] {row['sku']}: {' | '.join(results_fix)}")
                        if use_live:
                            persist_listing_audit_state(
                                conn,
                                row["sku"],
                                state="dirty",
                                source_fingerprint=source_fingerprint,
                                live_fingerprint=live_fingerprint,
                                listing_id=current_listing_id,
                                issue_types=issue_types,
                            )
                    elif results_fix and all("ERROR" not in r for r in results_fix):
                        fixed_count += 1
                        logger.info(f"[AUDIT FIX OK] {row['sku']}: {' | '.join(results_fix)}")
                        if use_live:
                            persist_listing_audit_state(
                                conn,
                                row["sku"],
                                state="pending_verify",
                                source_fingerprint=source_fingerprint,
                                live_fingerprint=live_fingerprint,
                                listing_id=current_listing_id,
                                issue_types=issue_types,
                            )
                    time.sleep(1)  # Rate limit
                elif use_live:
                    persist_listing_audit_state(
                        conn,
                        row["sku"],
                        state="dirty",
                        source_fingerprint=source_fingerprint,
                        live_fingerprint=live_fingerprint,
                        listing_id=current_listing_id,
                        issue_types=issue_types,
                    )

        conn.close()

        summary = {
            "source": "live_ebay" if use_live else "local_db_optimization",
            "total_published": total,
            "issues_found": issues_found,
            "critical": critical_count,
            "fixed": fixed_count if auto_fix else 0,
            "errors": error_count,
            "transport_failures": transport_failures,
            "skipped_clean_frozen": skipped_clean_frozen if use_live else 0,
            "clean_state_recorded": clean_state_recorded if use_live and record_clean_state else 0,
            "aborted_network_failures": aborted_network_failures,
            "audited_live_listings": audited_live_listings if use_live else 0,
            "skipped_incremental_scope": skipped_incremental_scope if use_live and not auto_fix else 0,
            "unprocessed": max(0, total - attempted_live_fetches)
            if aborted_network_failures else 0,
            "top_issues": issue_skus[:10],
            "top_transport_failures": transport_issue_skus[:10],
        }

        logger.info(f"刊登审计完成:")
        logger.info(f"  活跃链接: {total}")
        logger.info(f"  有问题: {issues_found} (严重: {critical_count})")
        if use_live:
            logger.info(f"  抓取/availability 异常: {transport_failures}")
            logger.info(f"  增量 live 审计: {audited_live_listings}")
            if not auto_fix:
                logger.info(f"  增量范围跳过: {skipped_incremental_scope}")
            logger.info(f"  clean freeze 跳过: {skipped_clean_frozen}")
            if record_clean_state:
                logger.info(f"  clean 状态刷新: {clean_state_recorded}")
        if auto_fix:
            logger.info(f"  已修复: {fixed_count}, 失败: {error_count}")

        if critical_count > 0:
            logger.warning(
                f"⚠️ 发现 {critical_count} 个严重问题! 请检查日志中的 CRITICAL 项。"
            )

        return summary
    except Exception as e:
        logger.error(f"刊登审计失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {"error": str(e)}


def run_sales_health_check(
    auto_fix: bool = False,
    *,
    run_quantity_audit: bool = True,
) -> dict:
    """运行销售健康诊断（每日汇总默认只读，不自动改价）"""
    logger.info("="*60)
    logger.info("开始销售健康诊断")
    logger.info("="*60)

    try:
        sys.path.insert(0, str(PROJECT_ROOT / 'scripts'))
        from sales_health_check import SalesHealthChecker

        checker = SalesHealthChecker()
        report = checker.run(
            auto_fix=auto_fix,
            run_quantity_audit=run_quantity_audit,
        )

        s = report.get('summary', {})
        logger.info(f"健康诊断完成:")
        logger.info(f"  总链接: {s.get('total_listings', 0)}")
        logger.info(f"  需关注: {s.get('problems', 0)}")
        logger.info(f"  优质链接: {s.get('high_performers', 0)}")

        # 保存 email HTML 片段到 report 用于嵌入汇总邮件
        report['email_html'] = checker.generate_email_html()

        return report
    except Exception as e:
        logger.error(f"健康诊断失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {'error': str(e)}


def _load_product_thumbnails(skus: list) -> dict:
    """从数据库加载产品缩略图 URL (取 images 列表第一张)"""
    if not skus:
        return {}

    def _normalize_thumb_url(url: str) -> str:
        return normalize_thumbnail_url(url)

    def _extract_first_image(raw_images) -> str:
        if not raw_images:
            return ''
        try:
            images = json.loads(raw_images) if isinstance(raw_images, str) else raw_images
        except Exception:
            images = raw_images
        if isinstance(images, list) and images:
            return _normalize_thumb_url(images[0])
        if isinstance(images, str) and images.strip().startswith('http'):
            return _normalize_thumb_url(images)
        return ''

    import sqlite3
    conn = sqlite3.connect(str(PROJECT_ROOT / 'ebay_collection.db'))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    placeholders = ','.join('?' * len(skus))
    cur.execute(f"SELECT sku, images FROM collected_products WHERE sku IN ({placeholders})", skus)
    result = {}
    for row in cur.fetchall():
        image_url = _extract_first_image(row['images'])
        if image_url:
            result[row['sku']] = image_url
    conn.close()
    return result


def _load_today_smart_reprice_report(target_date: datetime | None = None) -> dict:
    """加载最近一份调价报告，允许跨日衔接，避免只按当天文件名误判未执行。"""
    target_date = target_date or datetime.now()
    reports_dir = PROJECT_ROOT / 'reports'
    candidates = sorted(
        reports_dir.glob('reprice_report_*.json'),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    cutoff = target_date - timedelta(hours=36)

    for latest in candidates:
        try:
            if datetime.fromtimestamp(latest.stat().st_mtime) < cutoff:
                continue
            data = json.loads(latest.read_text(encoding='utf-8'))
        except Exception as e:
            return {
                'status': 'error',
                'error': f'读取调价报告失败: {e}',
                'path': str(latest),
                'changed_rows': [],
                'summary': {},
            }

        changed_rows = data.get('changed_rows')
        if changed_rows is None:
            changed_rows = []
            for row in data.get('results', []):
                if row.get('status') != 'updated' or row.get('current_price') is None:
                    continue
                changed_rows.append({
                    'sku': row.get('sku', ''),
                    'title': row.get('title', ''),
                    'image_url': row.get('image_url', ''),
                    'old_price': float(row.get('current_price') or 0),
                    'new_price': float(row.get('new_price') or 0),
                    'price_diff': float(row.get('price_diff') or 0),
                    'pct_change': float(row.get('pct_change') or 0),
                    'strategy': row.get('strategy', ''),
                    'market_source': row.get('market_source', ''),
                    'market_avg': float(row.get('market_avg') or 0),
                    'market_sample_size': int(row.get('market_sample_size') or 0),
                    'market_sold_count': int(row.get('market_sold_count') or 0),
                    'market_fallback_reason': row.get('market_fallback_reason') or '',
                    'price_reason': row.get('price_reason') or '',
                })
            changed_rows.sort(key=lambda r: (abs(r['price_diff']), r['sku']), reverse=True)

        return {
            'status': data.get('status', 'ok'),
            'path': str(latest),
            'timestamp': data.get('timestamp', ''),
            'mode': data.get('mode', ''),
            'market_mode': data.get('market_mode', ''),
            'marketplace_insights_access': data.get('marketplace_insights_access', {}),
            'summary': data.get('summary', {}),
            'changed_rows': changed_rows,
        }

    return {
        'status': 'not_run',
        'date': target_date.strftime('%Y-%m-%d'),
        'changed_rows': [],
        'summary': {},
    }


def _format_smart_reprice_reason(row: dict) -> str:
    """将智能调价策略和市场数据拼成可读原因。"""
    explicit_reason = str(row.get('price_reason') or '').strip()
    if explicit_reason:
        return explicit_reason

    strategy_labels = {
        'ANTI_LOSS': '防亏损保护',
        'FLOOR_PRICE': '利润底价保护',
        'CEILING_PRICE': '市场上限约束',
        'COMPETITIVE': '按市场竞争调价',
        'STANDARD': '标准利润率定价',
    }
    source_labels = {
        'MARKETPLACE_INSIGHTS': 'Insights 成交',
        'BROWSE_FALLBACK': 'Browse 回退',
        'BROWSE_API': 'Browse 在售',
        'TERAPEAK': 'Terapeak',
        'UNKNOWN': '未知来源',
    }

    parts = [strategy_labels.get(row.get('strategy'), row.get('strategy') or '智能调价')]
    source = row.get('market_source') or 'UNKNOWN'
    parts.append(source_labels.get(source, source))

    market_avg = float(row.get('market_avg') or 0)
    if market_avg > 0:
        parts.append(f"市场均价 ${market_avg:.2f}")

    sold_count = int(row.get('market_sold_count') or 0)
    sample_size = int(row.get('market_sample_size') or 0)
    if sold_count > 0:
        parts.append(f"成交样本 {sold_count}")
    elif sample_size > 0:
        parts.append(f"样本数 {sample_size}")

    fallback_reason = str(row.get('market_fallback_reason') or '').strip()
    if fallback_reason:
        parts.append(f"回退: {fallback_reason}")

    return ' · '.join(parts)


def send_daily_summary_email(results: dict):
    """发送每日任务汇总邮件（使用统一邮件模块）"""
    from src.utils.email_sender import send_email

    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    date_str = datetime.now().strftime('%Y-%m-%d')

    # --- 构建 HTML ---
    analyze = results.get('analyze', {})
    inv = results.get('inventory', {})
    smart_reprice = results.get('smart_reprice') or _load_today_smart_reprice_report()
    health = results.get('health_check', {})
    cro = results.get('cro') or {}
    cro_summary = (cro.get('summary') or {}) if isinstance(cro, dict) else {}
    cro_delta = (cro.get('delta_vs_yesterday') or {}) if isinstance(cro, dict) else {}

    # 销售健康诊断 HTML 片段
    if health.get('error'):
        health_html = f'<h3>4️⃣ 销售健康诊断</h3><p style="color:red;">⚠️ {health["error"]}</p>'
    elif health.get('email_html'):
        health_html = health['email_html']
    else:
        health_html = ''

    # 财务摘要（F1：订单 PnL + GIGA 履约阶段；读本地表，非阻塞）
    try:
        from src.services.finance_orders import render_finance_email_html

        finance_html = render_finance_email_html()
    except Exception as _fin_exc:
        logger.warning(f"渲染财务摘要失败 (非阻塞): {_fin_exc}")
        finance_html = f'<h3>💰 财务摘要</h3><p style="color:red;">⚠️ 渲染失败: {html_escape(str(_fin_exc))}</p>'

    # 库存同步详情
    oos_skus = inv.get('out_of_stock', [])
    price_items = inv.get('price_changed', [])
    data_missing_skus = inv.get('data_missing', [])
    ghost_restocked = inv.get('ghost_restocked', [])
    ghost_restock_error = inv.get('ghost_restock_error', '')
    full_oos_audit = inv.get('full_oos_audit') or empty_inventory_audit(AUDIT_SCOPE_FULL_OOS)
    # 缺货 SKU 表格（含缩略图）
    if oos_skus:
        oos_thumbs = _load_product_thumbnails(oos_skus)
        oos_rows = ''
        for s in oos_skus:
            thumb_url = oos_thumbs.get(s, '')
            thumb_html = build_thumbnail_img_html(thumb_url, width=40, height=40)
            oos_rows += f'<tr><td style="padding:4px 6px;border:1px solid #ddd;text-align:center;">{thumb_html}</td><td style="padding:4px 6px;border:1px solid #ddd;color:red;">{s}</td></tr>'
        oos_html = f'<table style="border-collapse:collapse;font-size:13px;margin-top:4px;"><tr style="background:#ffebee;"><th style="padding:4px 6px;border:1px solid #ddd;">图片</th><th style="padding:4px 6px;border:1px solid #ddd;">SKU</th></tr>{oos_rows}</table>'
    else:
        oos_html = '<p style="color:#999;">无</p>'
    data_missing_html = ''.join(f'<li style="color:#cc6600;">{s}</li>' for s in data_missing_skus) if data_missing_skus else ''
    ghost_restock_html = ''.join(
        f"<li style=\"color:#067647;\">{html_escape(item.get('sku', ''))} - {html_escape(item.get('status', ''))}</li>"
        for item in ghost_restocked
    ) if ghost_restocked else ''
    
    # 价格变动明细表（含缩略图/旧价/新价）
    if price_items:
        # 预加载产品缩略图
        sku_thumbs = _load_product_thumbnails(
            [p.get('sku', '') if isinstance(p, dict) else str(p) for p in price_items]
        )
        price_rows = ''
        for p in price_items:
            sku = p.get('sku', '') if isinstance(p, dict) else str(p)
            old_val = p.get('old_value', '') if isinstance(p, dict) else ''
            new_val = p.get('new_value', '') if isinstance(p, dict) else ''
            msg = p.get('message', '') if isinstance(p, dict) else ''
            thumb_url = sku_thumbs.get(sku, '')
            thumb_html = build_thumbnail_img_html(thumb_url, width=50, height=50)
            # 判断是否成功（message 里有"已更新"或非空 new_value 表示成功）
            success = bool(new_val) and 'error' not in msg.lower()
            status_icon = '✅' if success else '⚠️'
            price_rows += f'''<tr>
                <td style="padding:6px 8px;border:1px solid #ddd;text-align:center;">{thumb_html}</td>
                <td style="padding:6px 8px;border:1px solid #ddd;">{sku}</td>
                <td style="padding:6px 8px;border:1px solid #ddd;">{old_val}</td>
                <td style="padding:6px 8px;border:1px solid #ddd;font-weight:bold;">{new_val}</td>
                <td style="padding:6px 8px;border:1px solid #ddd;text-align:center;">{status_icon}</td>
            </tr>'''
        price_html = f'''<table style="border-collapse:collapse;width:100%;margin-top:8px;font-size:13px;">
            <tr style="background:#fff3e0;">
                <th style="padding:6px 8px;border:1px solid #ddd;text-align:center;">图片</th>
                <th style="padding:6px 8px;border:1px solid #ddd;text-align:left;">SKU</th>
                <th style="padding:6px 8px;border:1px solid #ddd;text-align:left;">调整前</th>
                <th style="padding:6px 8px;border:1px solid #ddd;text-align:left;">调整后</th>
                <th style="padding:6px 8px;border:1px solid #ddd;text-align:center;">状态</th>
            </tr>{price_rows}</table>'''
    else:
        price_html = '<p style="color:#999;">无价格变动</p>'

    smart_reprice_summary = smart_reprice.get('summary', {}) if isinstance(smart_reprice, dict) else {}
    smart_reprice_items = smart_reprice.get('changed_rows', []) if isinstance(smart_reprice, dict) else []
    smart_reprice_status = smart_reprice.get('status') if isinstance(smart_reprice, dict) else 'not_run'

    # 价格守门员当日活动 (P2): 拒改 / 关广告 / 开广告 汇总
    try:
        from src.services.guard_activity import collect_guard_activity, render_guard_activity_html
        guard_activity_html = render_guard_activity_html(collect_guard_activity())
    except Exception as _ga_exc:
        logger.warning(f"渲染守门员活动章节失败 (非阻塞): {_ga_exc}")
        guard_activity_html = ''

    # 价格 / Bid 历史快照 (P9): 当日 smart_reprice 涉及的 SKU 落入 pricing_history
    try:
        from src.services.pricing_history import record_daily_snapshot
        from src.services import ad_blacklist as _bl
        _bl_set = set(_bl.list_all().keys())
        _snap_rows = []
        for _it in smart_reprice_items:
            _sku = _it.get('sku')
            if not _sku:
                continue
            _snap_rows.append({
                'sku': _sku,
                'listing_id': _it.get('listing_id') or _it.get('item_id'),
                'live_price': _it.get('new_price') or _it.get('current_price'),
                'total_cost': _it.get('total_cost') or _it.get('cost'),
                'current_bid_pct': _it.get('current_bid_pct'),
                'max_safe_ad_rate': _it.get('max_safe_ad_rate'),
                'ad_status': _it.get('ad_status'),
                'blacklisted': _sku in _bl_set,
            })
        if _snap_rows:
            record_daily_snapshot(_snap_rows)
    except Exception as _ph_exc:
        logger.warning(f"写 pricing_history 快照失败 (非阻塞): {_ph_exc}")

    # 成本异动检测 (P13): 全量 SKU cost snapshot + ≥10% 涨幅告警
    try:
        import sqlite3 as _sq
        from src.services.cost_history import (
            record_cost_snapshot, detect_anomalies, render_alert_html as _cost_alert_html,
        )
        _conn = _sq.connect('ebay_collection.db')
        _cost_rows = []
        for _sku, _cb in _conn.execute(
            "SELECT sku, cost_breakdown FROM collected_products "
            "WHERE cost_breakdown IS NOT NULL AND cost_breakdown != ''"
        ).fetchall():
            try:
                import json as _j
                _tc = float((_j.loads(_cb) or {}).get('total_cost') or 0)
                if _tc > 0:
                    _cost_rows.append({'sku': _sku, 'total_cost': _tc})
            except Exception:
                pass
        _conn.close()
        if _cost_rows:
            record_cost_snapshot(_cost_rows)
            # P17: 联动 — 附加新死线 + 现价 + underwater 标记
            try:
                from src.clients.real_ebay_client import RealEbayClient as _RC
                _rc = _RC()
                def _lp(_sku):
                    try:
                        _o = _rc.get_offer_by_sku(_sku)
                        return float((_o or {}).get('pricingSummary', {})
                                      .get('price', {}).get('value') or 0) or None
                    except Exception:
                        return None
            except Exception:
                _lp = None
            _anomalies = detect_anomalies(enrich_with_floor=True, live_price_fn=_lp)
            if _anomalies:
                _uw = sum(1 for a in _anomalies if a.get('underwater'))
                logger.warning(f"💰 成本异动: {len(_anomalies)} 条 SKU 涨价 ≥ 10%"
                                f" (其中 {_uw} 条已跌入新死线)")
                try:
                    from src.utils.email_sender import send_email as _send_em
                    _send_em(f"💰 成本异动告警 - {len(_anomalies)} SKU"
                              f"{f' ({_uw} 跌破死线)' if _uw else ''}",
                             _cost_alert_html(_anomalies))
                except Exception as _em_exc:
                    logger.warning(f"成本异动邮件失败: {_em_exc}")
    except Exception as _ch_exc:
        logger.warning(f"成本异动检测失败 (非阻塞): {_ch_exc}")

    if smart_reprice_status == 'ok':
        report_time = str(smart_reprice.get('timestamp', '') or '').replace('T', ' ')[:19]
        shown_reprice_items = smart_reprice_items[:8]
        extra_reprice_count = max(0, len(smart_reprice_items) - len(shown_reprice_items))
        sku_thumbs = _load_product_thumbnails([item.get('sku', '') for item in shown_reprice_items])
        reprice_rows = ''
        for item in shown_reprice_items:
            sku = item.get('sku', '')
            thumb_url = item.get('image_url') or sku_thumbs.get(sku, '')
            thumb_html = build_thumbnail_img_html(thumb_url, width=42, height=42)
            diff = float(item.get('pct_change') or 0)
            diff_color = '#067647' if diff >= 0 else '#b42318'
            reason = html_escape(_format_smart_reprice_reason(item))
            title = html_escape(str(item.get('title') or ''))
            product_label = html_escape(sku)
            if title:
                product_label = f'<b>{html_escape(sku)}</b><br><span style="color:#667085;font-size:11px;line-height:1.25;">{title}</span>'
            reprice_rows += f'''<tr>
                <td style="padding:5px 6px;border:1px solid #ddd;text-align:center;">{thumb_html}</td>
                <td style="padding:5px 6px;border:1px solid #ddd;">{product_label}</td>
                <td style="padding:5px 6px;border:1px solid #ddd;text-align:right;white-space:nowrap;">${float(item.get('old_price') or 0):.2f}</td>
                <td style="padding:5px 6px;border:1px solid #ddd;text-align:right;font-weight:bold;white-space:nowrap;">${float(item.get('new_price') or 0):.2f}</td>
                <td style="padding:5px 6px;border:1px solid #ddd;text-align:right;color:{diff_color};font-weight:bold;white-space:nowrap;">{diff:+.1f}%</td>
                <td style="padding:5px 6px;border:1px solid #ddd;font-size:12px;line-height:1.35;">{reason}</td>
            </tr>'''
        reprice_details_html = (f'''<details><summary>查看调价明细（显示 {len(shown_reprice_items)} / {len(smart_reprice_items)}）</summary>
            <table style="border-collapse:collapse;width:100%;margin-top:8px;font-size:12px;table-layout:fixed;">
                <tr style="background:#eef6ff;">
                    <th style="padding:5px 6px;border:1px solid #ddd;width:52px;text-align:center;">图</th>
                    <th style="padding:5px 6px;border:1px solid #ddd;width:150px;text-align:left;">Product</th>
                    <th style="padding:5px 6px;border:1px solid #ddd;width:72px;text-align:right;">前</th>
                    <th style="padding:5px 6px;border:1px solid #ddd;width:72px;text-align:right;">后</th>
                    <th style="padding:5px 6px;border:1px solid #ddd;width:62px;text-align:right;">变动</th>
                    <th style="padding:5px 6px;border:1px solid #ddd;text-align:left;">原因</th>
                </tr>{reprice_rows}
            </table>
            {f'<p style="margin:8px 0 0;color:#666;font-size:12px;">其余 {extra_reprice_count} 个 SKU 未在邮件展开，详见当天调价报告。</p>' if extra_reprice_count else ''}
            </details>''') if smart_reprice_items else ''
        if smart_reprice_items:
            status_banner = '<div style="margin:10px 0 12px;padding:10px 12px;border-radius:8px;background:#ecfdf3;border:1px solid #a6f4c5;color:#067647;font-weight:bold;">✅ 本次每日主任务已执行智能调价，并产生调价结果</div>'
        else:
            status_banner = '<div style="margin:10px 0 12px;padding:10px 12px;border-radius:8px;background:#f8f9fc;border:1px solid #d0d5dd;color:#344054;font-weight:bold;">ℹ️ 本次每日主任务已执行智能调价，但没有需要改价的 SKU</div>'
        summary_badges_html = f'''
        <div style="margin:10px 0 12px;display:flex;flex-wrap:wrap;gap:8px;font-size:12px;">
            <span style="background:#f2f4f7;border:1px solid #d0d5dd;border-radius:999px;padding:5px 10px;">检查 {smart_reprice_summary.get('total', 0)}</span>
            <span style="background:#ecfdf3;border:1px solid #a6f4c5;border-radius:999px;padding:5px 10px;color:#067647;font-weight:bold;">调价 {smart_reprice_summary.get('price_changes', len(smart_reprice_items))}</span>
            <span style="background:#f0f9ff;border:1px solid #b9e6fe;border-radius:999px;padding:5px 10px;">涨价 {smart_reprice_summary.get('price_up', 0)}</span>
            <span style="background:#fff1f3;border:1px solid #fecdd6;border-radius:999px;padding:5px 10px;">降价 {smart_reprice_summary.get('price_down', 0)}</span>
            <span style="background:#f9fafb;border:1px solid #eaecf0;border-radius:999px;padding:5px 10px;">不变 {smart_reprice_summary.get('no_change', 0)}</span>
            <span style="background:#fef3f2;border:1px solid #fecdca;border-radius:999px;padding:5px 10px;">错误 {smart_reprice_summary.get('errors', 0)}</span>
        </div>'''
        smart_reprice_html = f'''
        <h3>3️⃣ 智能调价结果</h3>
        {status_banner}
        <p style="margin:0 0 10px;color:#667085;font-size:12px;">报告时间: {html_escape(report_time or '未知')} | 模式: {html_escape(str(smart_reprice.get('mode', '') or 'unknown'))} | Market Mode: {html_escape(str(smart_reprice.get('market_mode', '') or 'unknown'))} | 报告: {html_escape(Path(str(smart_reprice.get('path', ''))).name if smart_reprice.get('path') else 'unknown')}</p>
        {summary_badges_html}
        {reprice_details_html}
        '''
    elif smart_reprice_status == 'error':
        smart_reprice_html = f'''<h3>3️⃣ 智能调价结果</h3>
        <div style="margin:10px 0 12px;padding:10px 12px;border-radius:8px;background:#fef3f2;border:1px solid #fecdca;color:#b42318;font-weight:bold;">⚠️ 每日主任务中的智能调价步骤失败</div>
        <p style="color:red;">{html_escape(smart_reprice.get("error", "读取智能调价报告失败"))}</p>'''
    elif smart_reprice_status == 'scheduled_skip':
        smart_reprice_html = f'''<h3>3️⃣ 智能调价结果</h3>
        <div style="margin:10px 0 12px;padding:10px 12px;border-radius:8px;background:#f8f9fc;border:1px solid #d0d5dd;color:#344054;font-weight:bold;">📅 今天不是智能调价执行日</div>
        <p style="margin:0;color:#344054;font-size:13px;font-weight:600;">智能调价已并入每日主任务框架，但只在 {html_escape(str(smart_reprice.get("schedule_label") or SMART_REPRICE_RUN_DAY_LABELS))} 的 daily full 中执行。</p>'''
    else:
        smart_reprice_html = '''<h3>3️⃣ 智能调价结果</h3>
        <div style="margin:10px 0 12px;padding:14px 16px;border-radius:10px;background:#fff7ed;border:2px solid #fdba74;color:#9a3412;font-weight:800;font-size:16px;line-height:1.4;">⏰ 本次每日主任务未执行智能调价</div>
        <p style="margin:0;color:#9a3412;font-size:13px;font-weight:600;">未发现最近一次智能调价结果，说明该步骤没有执行完成。</p>'''

    def _audit_table(title, audit, *, show_supplier_skus=False):
        details = ''
        if show_supplier_skus and audit.get('supplier_oos_skus'):
            details += (
                '<details><summary>供应商无货 SKU</summary><ul>'
                + ''.join(f'<li>{html_escape(str(sku))}</li>' for sku in audit['supplier_oos_skus'])
                + '</ul></details>'
            )
        if audit.get('qty_zero_skus'):
            details += (
                '<details><summary>eBay 库存为 0 SKU</summary><ul>'
                + ''.join(f'<li>{html_escape(str(sku))}</li>' for sku in audit['qty_zero_skus'])
                + '</ul></details>'
            )
        if audit.get('error_count'):
            details += f'<p style="color:#b42318;">错误数: {audit.get("error_count", 0)}</p>'
        if audit.get('error'):
            details += f'<p style="color:#b42318;">审核异常: {html_escape(str(audit["error"]))}</p>'
        return f'''
        <h4>{title}</h4>
        <table style="border-collapse:collapse;width:100%;font-size:13px;margin-bottom:8px;">
          <tr style="background:#f5f5f5;"><td style="padding:7px;border:1px solid #ddd;">检查范围</td><td style="padding:7px;border:1px solid #ddd;">{html_escape(audit_scope_label(audit.get('audit_scope')))}</td></tr>
          <tr><td style="padding:7px;border:1px solid #ddd;">范围总数</td><td style="padding:7px;border:1px solid #ddd;">{audit.get('scope_count', audit.get('checked_count', 0))}</td></tr>
          <tr><td style="padding:7px;border:1px solid #ddd;">本次跳过</td><td style="padding:7px;border:1px solid #ddd;">{audit.get('skipped_count', 0)}</td></tr>
          <tr><td style="padding:7px;border:1px solid #ddd;">检查数</td><td style="padding:7px;border:1px solid #ddd;font-weight:bold;">{audit.get('checked_count', 0)}</td></tr>
          <tr style="background:#ffebee;"><td style="padding:7px;border:1px solid #ddd;">eBay 库存为 0</td><td style="padding:7px;border:1px solid #ddd;color:#b42318;font-weight:bold;">{audit.get('qty_zero_count', 0)}</td></tr>
          <tr style="background:#fff8e1;"><td style="padding:7px;border:1px solid #ddd;">供应商无货</td><td style="padding:7px;border:1px solid #ddd;color:#b54708;font-weight:bold;">{audit.get('supplier_oos_count', 0)}</td></tr>
          <tr style="background:#ecfdf3;"><td style="padding:7px;border:1px solid #ddd;">恢复数</td><td style="padding:7px;border:1px solid #ddd;color:#067647;font-weight:bold;">{audit.get('restocked_count', 0)}</td></tr>
          <tr><td style="padding:7px;border:1px solid #ddd;">错误数</td><td style="padding:7px;border:1px solid #ddd;">{audit.get('error_count', 0)}</td></tr>
        </table>{details}'''

    inventory_html = (
        _audit_table('增量库存同步', inv, show_supplier_skus=True)
        + _audit_table('全量缺货审核', full_oos_audit)
        + (f'<details><summary>🔴 增量同步缺货 SKU 列表</summary>{oos_html}</details>' if oos_skus else '')
        + (
            f'<details><summary>⚠️ 数据异常 SKU (有库存但无价格，需人工检查)</summary><ul>{data_missing_html}</ul></details>'
            if data_missing_skus
            else ''
        )
        + (f'<details><summary>🟢 幽灵下架恢复明细</summary><ul>{ghost_restock_html}</ul></details>' if ghost_restocked else '')
        + (f'<p style="color:#b42318;">幽灵下架恢复检查失败: {html_escape(ghost_restock_error)}</p>' if ghost_restock_error else '')
        + (f'<details open><summary>💰 价格变动明细</summary>{price_html}</details>' if price_items else '')
    )

    html = f"""
    <html><body style="font-family:Arial,sans-serif;padding:20px;max-width:700px;">
    <h2 style="color:#1a73e8;">📋 {get_store_profile().brand_name} 每日任务汇总 - {date_str}</h2>
    <p style="color:#666;">执行时间: {now}</p>
    <hr style="border:1px solid #e0e0e0;">

    <h3>1️⃣ 产品分析</h3>
    <table style="border-collapse:collapse;width:100%;">
      <tr style="background:#f5f5f5;"><td style="padding:8px;border:1px solid #ddd;">成功</td>
          <td style="padding:8px;border:1px solid #ddd;color:green;font-weight:bold;">{analyze.get('success', 0)}</td></tr>
      <tr><td style="padding:8px;border:1px solid #ddd;">失败</td>
          <td style="padding:8px;border:1px solid #ddd;color:red;">{analyze.get('failed', 0)}</td></tr>
    </table>
    {'<p style="color:red;">⚠️ ' + analyze.get('error', '') + '</p>' if analyze.get('error') else ''}

    <h3>2️⃣ 库存与缺货审核</h3>
    {inventory_html}

        {smart_reprice_html}

    {health_html}

    {finance_html}

    {guard_activity_html}

    <h3>5️⃣ CRO 转化率诊断 (主线)</h3>
    {('<p style="color:red;">⚠️ ' + html_escape(str(cro.get('error',''))) + '</p>') if cro.get('error') else (
        '<table style="border-collapse:collapse;width:100%;font-size:13px;">'
        f'<tr style="background:#eef6ff;"><td style="padding:6px 10px;border:1px solid #ddd;">诊断 SKU</td>'
        f'<td style="padding:6px 10px;border:1px solid #ddd;font-weight:bold;">{cro_summary.get("total", 0)}</td>'
        f'<td style="padding:6px 10px;border:1px solid #ddd;">平均 CRO 分</td>'
        f'<td style="padding:6px 10px;border:1px solid #ddd;font-weight:bold;color:#1a73e8;">{cro_summary.get("avg_cro_score", 0):.0f}/100</td></tr>'
        f'<tr><td style="padding:6px 10px;border:1px solid #ddd;">健康 SKU</td>'
        f'<td style="padding:6px 10px;border:1px solid #ddd;color:#067647;font-weight:bold;">{cro_summary.get("healthy_count", 0)}</td>'
        f'<td style="padding:6px 10px;border:1px solid #ddd;">P1 入队</td>'
        f'<td style="padding:6px 10px;border:1px solid #ddd;color:#b42318;font-weight:bold;">{cro.get("p1_queued", 0)}</td></tr>'
        f'<tr style="background:#f9fafb;"><td style="padding:6px 10px;border:1px solid #ddd;">vs 昨日 改善</td>'
        f'<td style="padding:6px 10px;border:1px solid #ddd;color:#067647;">{len(cro_delta.get("improved",[]))}</td>'
        f'<td style="padding:6px 10px;border:1px solid #ddd;">vs 昨日 恶化</td>'
        f'<td style="padding:6px 10px;border:1px solid #ddd;color:#b42318;">{len(cro_delta.get("worsened",[]))}</td></tr>'
        '</table>'
        '<p style="color:#667085;font-size:12px;margin-top:6px;">P1 改价建议已写入 <code>logs/cro_action_queue.jsonl</code>，10:00 自动消费 (batch_smart_reprice --from-cro-queue)。</p>'
    )}

    <hr style="border:1px solid #e0e0e0;">
    <p style="color:#999;font-size:12px;">此邮件由 Dajian Listing Tool 自动发送</p>
    </body></html>
    """

    health_problems = health.get('summary', {}).get('problems', 0) if isinstance(health, dict) else 0
    health_icon = '🔴' if health_problems > 5 else ('🟡' if health_problems > 0 else '🟢')
    smart_reprice_count = len(smart_reprice_items) if smart_reprice_status == 'ok' else 0
    subject = (f"📋 {get_store_profile().brand_name} 每日汇总 - {date_str} | "
               f"增量同步{inv.get('checked_count', inv.get('checked', 0))}个 全量审核{full_oos_audit.get('checked_count', 0)}个 "
               f"调价{smart_reprice_count} "
               f"{health_icon}健康{health_problems}问题 "
               f"🎯CRO {cro_summary.get('avg_cro_score', 0):.0f}分/P1队{cro.get('p1_queued', 0)}")
    return send_email(subject, html)


def run_cro_diagnose(enqueue_p1: bool = True) -> dict:
    """CRO 主线: 漏斗诊断 → 落 cro_snapshots → diff 昨日 → 自动入队 P1 改价.

    数据源复用 competition_monitor 的 load/merge 帮手, 不直接调 eBay (用缓存的 perf_data).
    """
    logger.info("=" * 60)
    logger.info("开始 CRO 转化率诊断")
    logger.info("=" * 60)
    try:
        from src.web.pages.competition_monitor import (
            load_products_from_db, load_performance_data,
            merge_performance_into_products, get_market_data_from_report,
        )
        from src.services.cro_daily_runner import run_cro_daily

        products = load_products_from_db()
        if not products:
            logger.warning("CRO: 没有 PUBLISHED 产品, 跳过")
            return {'status': 'no_products', 'summary': {'total': 0}}

        perf = load_performance_data(force_refresh=False)
        # 数据质量守门: 断网/Analytics API 失败时 traffic_meta.api_ok=False,
        # 此时全部 listing 会被误判为 no_impression, 若照常落 cro_snapshots
        # 会污染当日快照并令次日 diff 冒出大批假 "improved". 直接跳过诊断,
        # 保留昨日快照不动. (is_truncated=True 是正常的 top-200 覆盖, 不算降级.)
        traffic_meta = (perf or {}).get('traffic_meta') or {}
        if not perf or not traffic_meta.get('api_ok'):
            logger.warning(
                "CRO: 流量数据不可用 (perf=%s, api_ok=%s), 跳过诊断以免污染快照",
                bool(perf), traffic_meta.get('api_ok'),
            )
            return {
                'status': 'degraded_skipped',
                'reason': 'traffic data unavailable (api_ok=False)',
                'summary': {'total': 0},
            }
        merge_performance_into_products(products, perf or {})
        # images 字段补 list
        for p in products:
            p['images'] = [p['image_url']] if p.get('image_url') else []

        market_data = get_market_data_from_report(None)

        rep = run_cro_daily(
            products,
            market_data=market_data,
            enqueue_p1=enqueue_p1,
            enqueue_action_types=('price_drop', 'image_refresh',
                                  'fill_specifics', 'promote', 'send_offer'),
            # 每日 listing 活跃维护总量 50: 避免把"活跃"等同于频繁改标题.
            # 标题关键词增强保留在 scripts/cro_title_rewrite.py 的手动 dry-run/apply 通道.
            action_quotas={
                'price_drop': 10,
                'image_refresh': 5,
                'fill_specifics': 15,
                'promote': 10,
                'send_offer': 10,
            },
            # image_refresh / fill_specifics 诊断恒为 P2; 不放宽到 2 时
            # 10:15/10:20 的执行器只会消费空队列
            enqueue_max_priority=2,
        )
        s = rep['summary']
        delta = rep['delta_vs_yesterday']
        logger.info(
            f"CRO 完成: 诊断 {s['total']} · 平均 {s.get('avg_cro_score', 0):.0f}/100 · "
            f"健康 {s.get('healthy_count', 0)} · "
            f"改善 {len(delta['improved'])} · 恶化 {len(delta['worsened'])} · "
            f"入队 {rep['p1_queued']} {rep.get('queued_by_action') or ''}"
        )
        return rep
    except Exception as e:
        logger.error(f"CRO 诊断异常: {e}", exc_info=True)
        return {'error': str(e)}


def run_mi_snapshot(min_margin: float = 0.20, max_results: int = 30) -> dict:
    """F17 — 每日运行 MI 自动机会发现并落盘快照，使 KPI 历史走势有连续数据。

    输出：
    - reports/mi_opportunities_YYYYMMDD_HHMMSS.json
    - 自动清理 >30 天的旧快照（与 UI 一致的策略）
    - 触发 F18 KPI 异常告警（如果出现机会数大幅下降或 audit_blocked 占比过高）
    """
    logger.info("=" * 60)
    logger.info("开始 MI 自动机会发现 (Market Intelligence Snapshot)")
    logger.info("=" * 60)
    result: dict = {"status": "ok", "opportunities": 0, "snapshot": None}
    try:
        from src.plugins.terapeak_research.intelligence_service import IntelligenceService
        from src.plugins.terapeak_research.history import (
            cleanup_old_snapshots, load_mi_snapshots, summarize_recent_history,
        )

        intel = IntelligenceService()
        from src.utils.mi_opportunity_flow import lookup_supplier_stock
        try:
            from src.utils.mi_unpublished_pool import recycle_ended_for_mi

            try:
                recycle_limit = max(0, int(os.getenv("MI_ENDED_RECYCLE_LIMIT", "20")))
            except ValueError:
                recycle_limit = 20
            result["ended_recycled"] = recycle_ended_for_mi(
                str(PROJECT_ROOT / "ebay_collection.db"),
                limit=recycle_limit,
                store_kind=getattr(get_store_profile(), "store_kind", "furniture"),
                blacklist=intel._load_mi_blacklist(),
                stock_lookup=lookup_supplier_stock,
            )
            recycled = result["ended_recycled"].get("reactivated") or []
            if recycled:
                logger.info(
                    f"[MI] 已把 {len(recycled)} 条 ENDED 库存回收为 PENDING 供发掘"
                )
        except Exception as recycle_error:
            logger.warning(f"[MI] ENDED 回收失败: {recycle_error}")
            result["ended_recycled"] = {
                "reactivated": [],
                "skipped": {"error": 1},
                "requested": 0,
            }

        try:
            from src.plugins.terapeak_research.external_discovery import (
                ingest_uncollected_for_mi,
            )

            try:
                giga_limit = max(0, int(os.getenv("MI_GIGA_INGEST_LIMIT", "10")))
            except ValueError:
                giga_limit = 10
            result["giga_ingested"] = ingest_uncollected_for_mi(
                intel,
                limit=giga_limit,
                store_kind=getattr(get_store_profile(), "store_kind", "furniture"),
                stock_lookup=lookup_supplier_stock,
                db_path=str(PROJECT_ROOT / "ebay_collection.db"),
            )
            ingested = result["giga_ingested"].get("inserted") or []
            if ingested:
                logger.info(
                    f"[MI] 已从 GigaCloud 入库 {len(ingested)} 条未采集新品为 PENDING"
                )
        except Exception as ingest_error:
            logger.warning(f"[MI] GigaCloud 新品入库失败: {ingest_error}")
            result["giga_ingested"] = {
                "inserted": [],
                "skipped": {"error": 1},
                "requested": 0,
            }

        opportunities = intel.auto_discover_opportunities(
            min_margin=min_margin, max_results=max_results
        )
        result["opportunities"] = len(opportunities)

        result["auto_prepared"] = auto_prepare_mi_opportunity_drafts(
            opportunities,
            analyze_collected_products=analyze_collected_products,
        ) if opportunities else empty_auto_prepare_result()

        snap_dir = PROJECT_ROOT / "reports"
        snap_dir.mkdir(exist_ok=True)
        snap_path = snap_dir / f"mi_opportunities_{datetime.now():%Y%m%d_%H%M%S}.json"
        # F30 — 同时持久化按品类的精简摘要，便于后续切片趋势
        try:
            from src.plugins.terapeak_research.aggregation import aggregate_by_category
            by_category = aggregate_by_category(opportunities)
        except Exception:
            by_category = []
        snap_path.write_text(
            json.dumps({
                "generated_at": datetime.now().isoformat(),
                "min_margin": min_margin,
                "max_results": max_results,
                "opportunities": opportunities,
                "by_category": by_category,
            }, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        result["snapshot"] = snap_path.name
        logger.info(f"[MI] 已保存 {len(opportunities)} 条机会到 {snap_path.name}")

        purged = cleanup_old_snapshots(keep_days=30, project_root=PROJECT_ROOT)
        if purged:
            logger.info(f"[MI] 已清理 {purged} 份超过 30 天的旧快照")
        result["purged_snapshots"] = purged

        # F18 — KPI 异常告警
        try:
            snaps = load_mi_snapshots(window_days=14, project_root=PROJECT_ROOT)
            kpi = summarize_recent_history(snaps, opportunities)
            result["kpi"] = kpi
            alerts = _check_mi_alerts(kpi, opportunities)
            if alerts:
                # F21 — 24h 去重抑制
                alerts_to_send = _filter_suppressed_alerts(alerts, window_hours=24)
                result["alerts"] = alerts
                result["alerts_suppressed"] = len(alerts) - len(alerts_to_send)
                if alerts_to_send:
                    _send_mi_alert_email(alerts_to_send, kpi, snap_path.name, opportunities)
                    _record_alerts_sent(alerts_to_send)
                    logger.warning(f"[MI ALERT] 已发送 {len(alerts_to_send)} 条告警: {alerts_to_send}")
                if result["alerts_suppressed"]:
                    logger.info(f"[MI ALERT] 抑制重复告警 {result['alerts_suppressed']} 条（24h 内已发过）")

            # F23 — 长周期透视（用更宽窗口快照）
            try:
                long_snaps = load_mi_snapshots(window_days=60, project_root=PROJECT_ROOT)
                from src.plugins.terapeak_research.history import summarize_long_window
                long_window = summarize_long_window(long_snaps, opportunities,
                                                    short_days=14, long_days=30)
                result["long_window"] = long_window
            except Exception as lw_err:
                logger.error(f"[MI] 长周期透视失败: {lw_err}")
                long_window = None

            # F27 — 持续衰退告警（基于长周期 trend_label 历史）
            try:
                trend_history = _record_long_window_trend(long_window)
                pf_alert = _check_persistent_falling_trend(trend_history, min_days=3)
                if pf_alert:
                    pf_to_send = _filter_suppressed_alerts([pf_alert], window_hours=24)
                    if pf_to_send:
                        _send_mi_alert_email(pf_to_send, kpi, snap_path.name, opportunities)
                        _record_alerts_sent(pf_to_send)
                        logger.warning(f"[MI ALERT] 持续衰退告警已发送: {pf_alert['message']}")
                        result.setdefault("alerts", []).append(pf_alert)
            except Exception as pf_err:
                logger.error(f"[MI] 持续衰退检测失败: {pf_err}")

            # F24 — 每日 MI 摘要（无论是否触发告警，每日固定 1 封）
            try:
                # F28 — 昨日 vs 今日对比：剔除今日快照
                from src.plugins.terapeak_research.history import compare_kpi_day_over_day
                today_str = datetime.now().strftime("%Y-%m-%d")
                snaps_prior = [s for s in snaps
                               if s["generated_at"].strftime("%Y-%m-%d") != today_str]
                dod = compare_kpi_day_over_day(snaps_prior, opportunities)
                _send_mi_daily_digest(opportunities, kpi, snap_path.name, long_window,
                                       dod_compare=dod)
            except Exception as digest_err:
                logger.error(f"[MI DIGEST] 失败: {digest_err}")
        except Exception as kpi_err:
            logger.error(f"[MI] KPI 告警检查失败: {kpi_err}")
            result["kpi_error"] = str(kpi_err)

    except Exception as e:
        logger.error(f"MI 快照生成异常: {e}")
        result["status"] = "error"
        result["error"] = str(e)
    return result


def _check_mi_alerts(kpi: dict, opportunities: list) -> list:
    """F18 — 返回触发的告警列表。

    规则：
    - delta_vs_avg <= -5：今日机会数较 14d 均值大幅下降
    - real_str_coverage_pct < 50：真实 STR 覆盖率不足，数据可信度低
    - audit_blocked 占比 > 50%（统计屏蔽名单中 audit_blocked 的占比）
    """
    alerts: list = []
    if not isinstance(kpi, dict):
        return alerts
    try:
        delta = kpi.get("delta_vs_avg")
        if delta is not None and delta <= -5:
            alerts.append({
                "type": "opportunity_drop",
                "severity": "high",
                "message": f"机会数较 14d 均值下降 {abs(delta):.1f}（当前 {kpi.get('current_count')} 条，均值 {kpi.get('avg_recent_count')} 条）",
            })
        cov = kpi.get("real_str_coverage_pct")
        if cov is not None and cov < 50 and len(opportunities) >= 5:
            alerts.append({
                "type": "low_str_coverage",
                "severity": "medium",
                "message": f"真实 STR 覆盖率仅 {cov:.1f}%，机会评分基础不可靠",
            })
        # 屏蔽名单 audit_blocked 占比
        bl_path = PROJECT_ROOT / "reports" / "mi_blacklist.json"
        if bl_path.exists():
            try:
                bl = json.loads(bl_path.read_text(encoding="utf-8"))
                if isinstance(bl, dict) and len(bl) >= 5:
                    audit_blocked = sum(
                        1 for v in bl.values()
                        if isinstance(v, dict) and v.get("reason") == "audit_blocked"
                    )
                    ratio = audit_blocked / len(bl)
                    if ratio > 0.5:
                        alerts.append({
                            "type": "audit_blocked_dominant",
                            "severity": "high",
                            "message": f"屏蔽名单 {len(bl)} 条中 {audit_blocked} 条因审计失败屏蔽（{ratio*100:.0f}%），需排查发布流程",
                        })
            except Exception:
                pass
    except Exception:
        pass
    return alerts


# F27 — 长周期持续衰退告警
_MI_LONG_TREND_PATH = PROJECT_ROOT / "reports" / "mi_long_window_history.json"


def _record_long_window_trend(long_window: dict | None,
                               history_path: Path | None = None,
                               now: datetime | None = None,
                               max_entries: int = 14) -> list:
    """记录每日长周期 trend_label，返回更新后的历史列表（按日期升序）。

    同一天多次调用时覆盖（保留最新 trend_label）。"""
    if not isinstance(long_window, dict):
        return []
    label = long_window.get("trend_label")
    if label not in ("rising", "falling", "stable"):
        return []
    path = history_path or _MI_LONG_TREND_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    today = (now or datetime.now()).strftime("%Y-%m-%d")
    history: list = []
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                history = [d for d in data if isinstance(d, dict) and d.get("date") != today]
        except Exception:
            history = []
    history.append({
        "date": today,
        "trend_label": label,
        "drift": long_window.get("drift"),
        "short_avg_count": long_window.get("short_avg_count"),
        "long_avg_count": long_window.get("long_avg_count"),
    })
    history.sort(key=lambda x: x["date"])
    history = history[-max_entries:]
    try:
        path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return history


def _check_persistent_falling_trend(history: list, min_days: int = 3) -> dict | None:
    """F27 — 若最近 min_days 个不同日期的 trend_label 全为 'falling'，返回告警。"""
    if not isinstance(history, list) or len(history) < min_days:
        return None
    recent = history[-min_days:]
    if all(isinstance(e, dict) and e.get("trend_label") == "falling" for e in recent):
        days = [e.get("date") for e in recent]
        return {
            "type": "persistent_falling_trend",
            "severity": "high",
            "message": (
                f"长周期机会数已连续 {min_days} 天判定为衰退（{days[0]} → {days[-1]}），"
                f"建议立刻复核竞品策略与品类布局"
            ),
        }
    return None


def format_mi_digest_subject(brand: str, count: int, when: datetime | None = None) -> str:
    """MI daily digest subject; brand first so multi-store inboxes stay distinguishable."""
    stamp = (when or datetime.now()).strftime("%Y-%m-%d")
    return f"📋 {brand} MI 日报 - {stamp} - {count} 条机会"


def format_mi_alert_subject(brand: str, count: int, *, high: bool, when: datetime | None = None) -> str:
    """MI alert subject; brand first, same inbox-splitting rule as the digest."""
    stamp = (when or datetime.now()).strftime("%Y-%m-%d")
    icon = "🚨" if high else "⚠️"
    return f"{icon} {brand} MI 告警 - {stamp} - 共 {count} 项"


def _send_mi_alert_email(alerts: list, kpi: dict, snap_name: str,
                         opportunities: list | None = None) -> bool:
    """F18 — 发送 MI 异常告警邮件，复用 send_email 通道。
    F21 — 中文内容 + 当涉及产品时附 Top 推荐缩略图。
    """
    try:
        sev_max = "high" if any(a.get("severity") == "high" for a in alerts) else "medium"
        icon = "🚨" if sev_max == "high" else "⚠️"
        rows = "".join(
            f"<tr><td style='padding:6px;border:1px solid #ddd;'>{a.get('type')}</td>"
            f"<td style='padding:6px;border:1px solid #ddd;'>{a.get('severity')}</td>"
            f"<td style='padding:6px;border:1px solid #ddd;'>{a.get('message')}</td></tr>"
            for a in alerts
        )

        # F21 — 缩略图区块（涉及产品的告警必带）
        thumbs_html = _build_opportunities_thumbnails_html(opportunities or [], limit=6)

        html = f"""
        <html><body style="font-family: 'Microsoft YaHei', Arial, sans-serif;">
        <h2>{icon} {get_store_profile().brand_name} 市场情报 异常告警</h2>
        <p>快照文件: <code>{snap_name}</code></p>
        <p><b>当前 KPI</b>: 机会数={kpi.get('current_count')} ·
           14d 均值={kpi.get('avg_recent_count')} ·
           偏离均值={kpi.get('delta_vs_avg')} ·
           真实 STR 覆盖率={kpi.get('real_str_coverage_pct')}%</p>
        <table style="border-collapse:collapse;margin-bottom:16px;">
        <tr style="background:#f5f5f5;">
          <th style='padding:6px;border:1px solid #ddd;'>类型</th>
          <th style='padding:6px;border:1px solid #ddd;'>严重度</th>
          <th style='padding:6px;border:1px solid #ddd;'>详情</th>
        </tr>
        {rows}
        </table>
        {thumbs_html}
        <hr style="border:1px solid #e0e0e0;">
        <p style="color:#999;font-size:12px;">
          由 daily_tasks.run_mi_snapshot 自动触发 · 同一类型告警 24 小时内不重复发送
        </p>
        </body></html>
        """
        subject = format_mi_alert_subject(
            get_store_profile().brand_name, len(alerts), high=(sev_max == "high")
        )
        return send_email(subject, html)
    except Exception as e:
        logger.error(f"MI 告警邮件发送失败: {e}")
        return False


def _build_opportunities_thumbnails_html(opportunities: list, limit: int = 6) -> str:
    """F21 — 渲染 Top 机会的缩略图栅格，便于邮件中直观看到涉及哪些产品。"""
    if not opportunities:
        return ""
    top = opportunities[:limit]
    cells = []
    for opp in top:
        if not isinstance(opp, dict):
            continue
        sku = opp.get("sku", "")
        title = (opp.get("title") or "")[:60]
        score = opp.get("opportunity_score", 0)
        price = opp.get("suggested_price", 0)
        # 取 image_url 优先，否则 images[0]
        img_url = opp.get("image_url", "") or ""
        if not img_url:
            imgs = opp.get("images") or []
            if isinstance(imgs, list) and imgs:
                img_url = imgs[0]
        try:
            img_html = build_thumbnail_img_html(img_url, alt=sku, size=120) if img_url else \
                       "<div style='width:120px;height:120px;background:#f0f0f0;display:flex;align-items:center;justify-content:center;color:#999;font-size:12px;'>无图</div>"
        except Exception:
            img_html = ""
        cells.append(
            f"<td style='padding:8px;border:1px solid #eee;vertical-align:top;width:140px;'>"
            f"{img_html}"
            f"<div style='font-size:11px;color:#333;margin-top:4px;'><b>{sku}</b></div>"
            f"<div style='font-size:11px;color:#666;'>分数 {score} · ${price}</div>"
            f"<div style='font-size:10px;color:#999;line-height:1.3;'>{title}</div>"
            f"</td>"
        )
    if not cells:
        return ""
    # 每行 3 列
    rows_html = ""
    for i in range(0, len(cells), 3):
        rows_html += "<tr>" + "".join(cells[i:i+3]) + "</tr>"
    return f"""
    <h3 style="margin-top:20px;">📦 当前 Top {len(cells)} 未刊登候选</h3>
    <table style="border-collapse:collapse;">{rows_html}</table>
    """


# ---------------------------------------------------------------------------
# F21 — Alert suppression state (reports/mi_alerts_state.json)
# ---------------------------------------------------------------------------

_MI_ALERTS_STATE_PATH = PROJECT_ROOT / "reports" / "mi_alerts_state.json"


def _load_alerts_state() -> dict:
    if not _MI_ALERTS_STATE_PATH.exists():
        return {}
    try:
        return json.loads(_MI_ALERTS_STATE_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _save_alerts_state(state: dict) -> None:
    try:
        _MI_ALERTS_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _MI_ALERTS_STATE_PATH.write_text(
            json.dumps(state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        logger.error(f"保存告警状态失败: {e}")


def _filter_suppressed_alerts(alerts: list, window_hours: int = 24,
                              now: datetime | None = None) -> list:
    """F21 — 过滤掉同 type 在 window_hours 内已发过的告警。"""
    state = _load_alerts_state()
    cutoff = (now or datetime.now()) - timedelta(hours=window_hours)
    out = []
    for a in alerts:
        if not isinstance(a, dict):
            continue
        a_type = a.get("type")
        if not a_type:
            continue
        last_iso = (state.get(a_type) or {}).get("last_sent_at")
        if last_iso:
            try:
                last_dt = datetime.fromisoformat(last_iso)
                if last_dt > cutoff:
                    continue  # 抑制
            except Exception:
                pass
        out.append(a)
    return out


def _record_alerts_sent(alerts: list, now: datetime | None = None) -> None:
    """F21 — 记录已发送的告警时间戳。"""
    state = _load_alerts_state()
    ts = (now or datetime.now()).isoformat()
    for a in alerts:
        if isinstance(a, dict) and a.get("type"):
            state[a["type"]] = {"last_sent_at": ts, "severity": a.get("severity")}
    _save_alerts_state(state)


def _send_mi_daily_digest(opportunities: list, kpi: dict, snap_name: str,
                          long_window: dict | None = None,
                          dod_compare: dict | None = None) -> bool:
    """F24 — 每日 MI 摘要。

    与 F18 告警邮件解耦：
    - F18 仅在异常时发送；
    - F24 每日固定归档一份 digest；有机会时发送邮件，无机会时只归档不发空邮件。
    F28 — 可选 dod_compare（昨日 vs 今日）渲染在顶部。
    全中文 + 缩略图。
    """
    opportunities = opportunities or []
    kpi = kpi or {}
    try:
        if opportunities:
            from src.plugins.terapeak_research.aggregation import aggregate_by_category
            cat_rows = aggregate_by_category(opportunities)
            # 按 count 倒序，仅展示前 8 类
            cat_rows = cat_rows[:8]
            cat_html_rows = "".join(
                f"<tr>"
                f"<td style='padding:6px 10px;border:1px solid #ddd;'>{r['category']}</td>"
                f"<td style='padding:6px 10px;border:1px solid #ddd;text-align:right;'>{r['count']}</td>"
                f"<td style='padding:6px 10px;border:1px solid #ddd;text-align:right;'>{r['avg_score']}</td>"
                f"<td style='padding:6px 10px;border:1px solid #ddd;text-align:right;'>${r['median_price']}</td>"
                f"<td style='padding:6px 10px;border:1px solid #ddd;text-align:right;'>{r['avg_margin_rate']}%</td>"
                f"<td style='padding:6px 10px;border:1px solid #ddd;text-align:right;'>${r['total_potential_profit']:,.0f}</td>"
                f"<td style='padding:6px 10px;border:1px solid #ddd;'>{r['top_sku']} ({r['top_score']})</td>"
                f"</tr>"
                for r in cat_rows
            )
            cat_html = (f"""
            <h3>🗂️ 品类聚合（Top {len(cat_rows)}）</h3>
            <table style="border-collapse:collapse;font-size:13px;">
            <tr style="background:#f5f5f5;">
              <th style='padding:6px 10px;border:1px solid #ddd;'>品类</th>
              <th style='padding:6px 10px;border:1px solid #ddd;'>机会数</th>
              <th style='padding:6px 10px;border:1px solid #ddd;'>均分</th>
              <th style='padding:6px 10px;border:1px solid #ddd;'>中位价</th>
              <th style='padding:6px 10px;border:1px solid #ddd;'>均利润率</th>
              <th style='padding:6px 10px;border:1px solid #ddd;'>潜在利润</th>
              <th style='padding:6px 10px;border:1px solid #ddd;'>Top SKU</th>
            </tr>
            {cat_html_rows}
            </table>
            """) if cat_rows else ""

            thumbs_html = _build_opportunities_thumbnails_html(opportunities, limit=10)
        else:
            cat_html = """
            <h3>🗂️ 品类聚合</h3>
            <p style="color:#666;">今日没有达到阈值的市场机会。</p>
            """
            thumbs_html = """
            <h3>🧾 未刊登候选</h3>
            <p style="color:#666;">今日无候选产品。</p>
            """

        # F28 — 昨日 vs 今日对比块
        dod_html = ""
        if isinstance(dod_compare, dict) and dod_compare.get("has_yesterday"):
            def _arrow(d):
                if d > 0:
                    return f"<span style='color:#2e7d32;'>▲ +{d}</span>"
                if d < 0:
                    return f"<span style='color:#c62828;'>▼ {d}</span>"
                return "<span style='color:#888;'>— 0</span>"
            new_skus = dod_compare.get("new_skus") or []
            exited_skus = dod_compare.get("exited_skus") or []
            new_html = ("、".join(new_skus[:10]) + ("…" if len(new_skus) > 10 else "")
                        ) if new_skus else "无"
            exit_html = ("、".join(exited_skus[:10]) + ("…" if len(exited_skus) > 10 else "")
                         ) if exited_skus else "无"
            dod_html = f"""
            <h3>📅 昨日 vs 今日（基准日 {dod_compare.get("yesterday_date")}）</h3>
            <table style="border-collapse:collapse;font-size:13px;">
              <tr style="background:#f5f5f5;">
                <th style='padding:6px 10px;border:1px solid #ddd;'>指标</th>
                <th style='padding:6px 10px;border:1px solid #ddd;'>昨日</th>
                <th style='padding:6px 10px;border:1px solid #ddd;'>今日</th>
                <th style='padding:6px 10px;border:1px solid #ddd;'>变化</th>
              </tr>
              <tr>
                <td style='padding:6px 10px;border:1px solid #ddd;'>机会数</td>
                <td style='padding:6px 10px;border:1px solid #ddd;text-align:right;'>{dod_compare["yesterday_count"]}</td>
                <td style='padding:6px 10px;border:1px solid #ddd;text-align:right;'>{dod_compare["today_count"]}</td>
                <td style='padding:6px 10px;border:1px solid #ddd;text-align:right;'>{_arrow(dod_compare["count_delta"])}</td>
              </tr>
              <tr>
                <td style='padding:6px 10px;border:1px solid #ddd;'>平均分</td>
                <td style='padding:6px 10px;border:1px solid #ddd;text-align:right;'>{dod_compare["yesterday_avg_score"]}</td>
                <td style='padding:6px 10px;border:1px solid #ddd;text-align:right;'>{dod_compare["today_avg_score"]}</td>
                <td style='padding:6px 10px;border:1px solid #ddd;text-align:right;'>{_arrow(dod_compare["score_delta"])}</td>
              </tr>
              <tr>
                <td style='padding:6px 10px;border:1px solid #ddd;'>真实 STR 覆盖率%</td>
                <td style='padding:6px 10px;border:1px solid #ddd;text-align:right;'>{dod_compare["yesterday_real_str_pct"]}</td>
                <td style='padding:6px 10px;border:1px solid #ddd;text-align:right;'>{dod_compare["today_real_str_pct"]}</td>
                <td style='padding:6px 10px;border:1px solid #ddd;text-align:right;'>{_arrow(dod_compare["real_str_pct_delta"])}</td>
              </tr>
            </table>
            <p style='font-size:12px;color:#555;'>🆕 今日新增（{len(new_skus)}）：{new_html}<br>
            👋 今日掉出（{len(exited_skus)}）：{exit_html}</p>
            """

        # 长周期文案（如果可用）
        long_html = ""
        if isinstance(long_window, dict) and long_window.get("snapshots_long", 0) >= 3:
            label_map = {"rising": "📈 回暖", "falling": "📉 衰退",
                         "stable": "➖ 平稳", "insufficient": "❔ 数据不足"}
            drift = long_window.get("drift", 0)
            drift_str = f"+{drift}" if drift > 0 else f"{drift}"
            long_html = f"""
            <p>🔭 <b>长周期透视</b>：14d 均值 {long_window['short_avg_count']} ·
               30d 均值 {long_window['long_avg_count']} ·
               漂移 {drift_str} ·
               趋势 <b>{label_map.get(long_window.get('trend_label'), long_window.get('trend_label'))}</b>
               （基于 {long_window['snapshots_long']} 份 30d 快照）</p>
            """

        brand = get_store_profile().brand_name
        html = f"""
        <html><body style="font-family: 'Microsoft YaHei', Arial, sans-serif;">
        <h2>📋 {brand} 市场情报 每日摘要 - {datetime.now():%Y-%m-%d}</h2>
        <p>快照文件: <code>{snap_name}</code> · 共发现 <b>{len(opportunities)}</b> 条机会</p>
        <p><b>当前 KPI</b>:
           14d 均值={kpi.get('avg_recent_count')} ·
           本次={kpi.get('current_count')} ·
           偏离均值={kpi.get('delta_vs_avg')} ·
           真实 STR 覆盖率={kpi.get('real_str_coverage_pct')}%</p>
        {dod_html}
        {long_html}
        {cat_html}
        {thumbs_html}
        <hr style="border:1px solid #e0e0e0;">
        <p style="color:#999;font-size:12px;">
          由 daily_tasks.run_mi_snapshot 每日自动发送 ·
          异常告警邮件由 F18 单独发送（同 type 24h 内不重复）
        </p>
        </body></html>
        """
        subject = format_mi_digest_subject(brand, len(opportunities))
        # F25 — 归档 HTML 到 reports/，保留 3 天
        try:
            _archive_mi_digest_html(html, keep_days=3)
        except Exception as ar_err:
            logger.error(f"[MI DIGEST] 归档失败: {ar_err}")
        if not opportunities:
            logger.info("[MI DIGEST] 无机会，已归档日报，不发送空日报邮件")
            return False
        return send_email(subject, html)
    except Exception as e:
        logger.error(f"MI 日报邮件发送失败: {e}")
        return False


def _archive_mi_digest_html(html: str, keep_days: int = 3,
                             reports_dir: Path | None = None,
                             now: datetime | None = None) -> Path:
    """F25 — 把 MI 日报 HTML 落盘到 reports/mi_digest_YYYYMMDD.html，
    并清理超过 keep_days 的旧归档。返回新写入的路径。"""
    target_dir = reports_dir or (PROJECT_ROOT / "reports")
    target_dir.mkdir(parents=True, exist_ok=True)
    ts = now or datetime.now()
    out = target_dir / f"mi_digest_{ts:%Y%m%d}.html"
    out.write_text(html, encoding="utf-8")
    logger.info(f"[MI DIGEST] 已归档: {out.name}")

    removed = purge_named_artifacts(
        target_dir,
        ("mi_digest_*.html",),
        keep_days,
        now=ts,
    )
    if removed:
        logger.info(f"[MI DIGEST] 清理 {removed} 份过期归档（>{keep_days}d）")
    return out


def main():
    parser = argparse.ArgumentParser(description='每日自动任务')
    parser.add_argument('--analyze-only', action='store_true', help='仅分析采集产品')
    parser.add_argument('--sync-only', action='store_true', help='仅同步库存')
    parser.add_argument('--audit-only', action='store_true', help='仅运行刊登质量审计')
    parser.add_argument('--mi-only', action='store_true',
                        help='仅运行 MI 自动机会发现 + 快照 (供 scheduler 独立定时任务解耦调用)')
    args = parser.parse_args()

    assert_runtime_not_in_maintenance(PROJECT_ROOT / "logs" / "_maintenance.lock")
    database_report = validate_runtime_database(PROJECT_ROOT / "ebay_collection.db")
    logger.info(
        "数据库启动检查通过: integrity=%s, links=%s, pages=%s",
        database_report.integrity_check,
        database_report.link_count,
        database_report.page_count,
    )

    # 运行期产物清理只做 housekeeping，不进入 task results，避免清理单文件失败
    # 触发每日业务任务重跑或被判定为业务失败。
    run_report_retention_cleanup()
    
    logger.info("="*60)
    logger.info(f"每日任务开始 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("="*60)
    
    results = {}
    
    if args.sync_only:
        results['inventory'] = sync_inventory()
    elif args.analyze_only:
        results['analyze'] = analyze_collected_products()
    elif args.mi_only:
        # F17 解耦: MI 快照独立于 45 分钟全量流程, 由 scheduler 单独定时,
        # 自带超时, daily_tasks 卡顿/失败不再拖累 MI 每日产出.
        results['mi_snapshot'] = run_mi_snapshot()
    elif args.audit_only:
        results['listing_audit'] = run_listing_audit(
            auto_fix=False,
            use_live=True,
            record_clean_state=True,
        )
    else:
        # 执行全部任务（每个任务独立 try/except，避免一个失败阻塞其他）
        try:
            results['analyze'] = analyze_collected_products()
        except Exception as e:
            logger.error(f"分析任务异常: {e}")
            results['analyze'] = {'error': str(e)}
        
        try:
            results['inventory'] = sync_inventory()
        except Exception as e:
            logger.error(f"库存同步异常: {e}")
            results['inventory'] = {
                **empty_inventory_audit(
                    AUDIT_SCOPE_INCREMENTAL,
                    error_count=1,
                    error=str(e),
                ),
                'error': str(e),
                'checked': 0,
                'errors': 1,
                'out_of_stock': [],
                'price_changed': [],
                'data_missing': [],
                'no_change': 0,
            }

        try:
            ghost_recovery = run_ghost_oos_recovery(auto_fix=True)
            results.setdefault('inventory', {})
            results['inventory']['full_oos_audit'] = ghost_recovery
            results['inventory']['ghost_restocked'] = ghost_recovery.get('qty_zero_restocked', [])
            if ghost_recovery.get('error'):
                results['inventory']['ghost_restock_error'] = ghost_recovery.get('error')
        except Exception as e:
            logger.error(f"幽灵缺货恢复异常: {e}")
            results.setdefault('inventory', {})
            results['inventory']['full_oos_audit'] = {
                **empty_inventory_audit(
                    AUDIT_SCOPE_FULL_OOS,
                    error_count=1,
                    error=str(e),
                ),
                'status': 'error',
            }
            results['inventory']['ghost_restock_error'] = str(e)

        try:
            if should_run_smart_reprice():
                results['smart_reprice'] = run_smart_reprice()
            else:
                logger.info("今天不是智能调价执行日，跳过智能调价（仅周一/周四执行）")
                results['smart_reprice'] = {
                    'status': 'scheduled_skip',
                    'schedule': ['monday', 'thursday'],
                    'schedule_label': SMART_REPRICE_RUN_DAY_LABELS,
                    'changed_rows': [],
                    'summary': {},
                }
        except Exception as e:
            logger.error(f"智能调价异常: {e}")
            results['smart_reprice'] = {'error': str(e)}

        # 4. 销售健康诊断 (每日汇总启用自动修复)
        try:
            results['health_check'] = run_sales_health_check(
                auto_fix=True,
                run_quantity_audit=False,
            )
        except Exception as e:
            logger.error(f"健康诊断异常: {e}")
            results['health_check'] = {'error': str(e)}

        # 5. 刊登质量审计 (live 只读复核 + clean freeze 刷新，不自动改写生产数据)
        try:
            results['listing_audit'] = run_listing_audit(
                auto_fix=False,
                use_live=True,
                record_clean_state=True,
            )
        except Exception as e:
            logger.error(f"刊登审计异常: {e}")
            results['listing_audit'] = {'error': str(e)}

        # 6. F17 — MI 自动机会发现 + 快照存档已解耦为 scheduler 独立每日任务
        #    (task_mi_snapshot → daily_tasks.py --mi-only), 不再随 45 分钟全量
        #    流程执行, 避免 daily_tasks 卡顿/超时拖累 MI 每日产出.

        # 7. CRO 转化率诊断 (主线 — 漏斗诊断 + P1 改价入队)
        try:
            results['cro'] = run_cro_diagnose(enqueue_p1=True)
        except Exception as e:
            logger.error(f"CRO 诊断异常: {e}")
            results['cro'] = {'error': str(e)}

        # 8. 财务数字刷新 (eBay 订单 PnL → 本地表; 日报「💰 财务摘要」读此表)
        #    scheduler 09:15 / 每 4h 也会跑; 这里再刷一次保证邮件用最新数.
        try:
            from src.services.finance_orders import sync_orders_from_ebay, query_finance_summary
            import sqlite3 as _sq_fin

            fin_report = sync_orders_from_ebay(days=30)
            _conn_fin = _sq_fin.connect(str(PROJECT_ROOT / "ebay_collection.db"))
            fin_summary = query_finance_summary(_conn_fin)
            _conn_fin.close()
            results['finance'] = {
                'status': 'ok',
                'sync': {
                    'orders_pulled': fin_report.get('orders_pulled'),
                    'upserted': fin_report.get('upserted'),
                    'errors': fin_report.get('errors'),
                    'giga_links_updated': fin_report.get('giga_links_updated'),
                },
                'summary': fin_summary,
            }
            logger.info(
                "财务同步: pulled=%s upserted=%s GMV=$%.2f net(est)=$%.2f not_pushed=%s",
                fin_report.get('orders_pulled'),
                fin_report.get('upserted'),
                fin_summary.get('gmv', 0),
                fin_summary.get('net', 0),
                fin_summary.get('not_pushed', 0),
            )
        except Exception as e:
            logger.error(f"财务同步异常 (非阻塞): {e}")
            results['finance'] = {'error': str(e)}
    
    logger.info("\n" + "="*60)
    logger.info("任务完成汇总")
    logger.info("="*60)
    
    for task, result in results.items():
        logger.info(f"{task}: {result}")
    
    logger.info(f"日志已保存到: {log_file}")
    
    # 发送汇总邮件（全量任务模式下）— --mi-only 有自带 digest, 不发每日汇总
    if not (args.analyze_only or args.sync_only or args.mi_only):
        try:
            send_daily_summary_email(results)
        except Exception as e:
            logger.error(f"汇总邮件发送失败: {e}")
    
    return results


if __name__ == "__main__":
    exit_code = 0
    try:
        task_results = main()
        outcome = classify_daily_task_outcome(task_results)
        if outcome == 'partial_success':
            logger.warning(
                "任务部分完成：仅存在逐 SKU 级失败（改价/库存同步）；"
                "禁止全量补跑，应走定向重试"
            )
            exit_code = 2
        elif outcome == 'failed':
            logger.error("任务结果包含业务失败，进程将返回非零退出码")
            exit_code = 1
    except Exception as e:
        logger.error(f"脚本异常退出: {e}")
        import traceback
        logger.error(traceback.format_exc())
        exit_code = 1
    finally:
        logging.shutdown()
    sys.exit(exit_code)
