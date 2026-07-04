# Script And Plugin Responsibility Matrix

这份文档用来回答三个问题：

1. 哪些脚本是主入口
2. 哪些脚本只是手动运维工具
3. 哪些入口已经过时，不应再作为默认路径使用

## 1. 主入口

| 入口 | 类型 | 用途 | 状态 |
|------|------|------|------|
| `daily_tasks.py` | 主调度 | 日常分析、同步、调价、审计、健康检查、邮件 | 主入口 |
| `scheduler_daemon.py` | 调度守护进程 | 长驻后台调度 daily tasks / live listing audit / promotion rotate | 主入口 |
| `server.py` | API 服务 | 采集、发布 API、OAuth、后台任务 | 主入口 |
| `app.py` | Streamlit UI | 人工查看、发布、授权、管理 | 主入口 |
| `batch_publish.py` | 发布工具 | READY 草稿 dry-run / live publish | 主入口 |
| `qwen_optimizer.py` | AI 引擎 | 标题、描述、属性、尺寸抽取 | 核心依赖 |

## 2. `scripts/` 责任矩阵

| 文件 | 责任 | 触发方式 | 状态 | 备注 |
|------|------|----------|------|------|
| `scripts/audit_fix_ready_drafts.py` | READY 草稿审计与修复 | 手动 / 发布前 | 主路径 | 发布前必须跑；复用 listing quality gate |
| `scripts/audit_fix_active_listings.py` | 已刊登链接审计与修复 | `scheduler_daemon.py` `11:30` / 手动 | 主路径 | 定时任务当前跑 `--live --email`，detect-only；显式 `--live --fix` 才做修复。报告按唯一 listing 行计数，不按根因去重 |
| `scripts/repair_published_taxonomy.py` | 历史 taxonomy 修复 | 手动 | 主路径 | 替代旧类目修复思路 |
| `scripts/batch_smart_reprice.py` | 智能调价 | `daily_tasks.py` / 手动 | 主路径 | 改价后必须回读验证 |
| `scripts/sales_health_check.py` | 销售健康诊断与部分自动修复 | `daily_tasks.py` / 手动 | 主路径 | 负责 ghost delist 等诊断 |
| `scripts/build_motors_compatibility.py` | 兼容性数据处理 | 手动 | 专用工具 | 用于 eBay Motors 相关能力 |
| `scripts/auto_rotate_promotions.py` | 促销轮换 | 手动 / 专项 | 专用工具 | 不在主发布链路内 |
| `scripts/retry_failed_categories.py` | 重试类目失败项 | 手动 | 专用工具 | 用于补救 |
| `scripts/remove_supplier_brand_from_published_titles.py` | 清洗已发布标题中的供应商品牌 | 手动 | 专用工具 | 标题维护场景 |
| `scripts/fix_measurement_quality_issues.py` | 修复测量质量问题 | 手动 | 专用工具 | 数据清洗辅助 |
| `scripts/fix_listing_categories.py` | 旧版类目修复脚本 | 不建议使用 | 旧入口 | 已被 `repair_published_taxonomy.py` 和 `audit_fix_active_listings.py` 取代 |
| `scripts/audit_active_listing_dimensions.py` | 维度专项审计 | 手动 | 辅助工具 | 作为专项排查工具保留 |
| `scripts/ad_blacklist_cleanup.py` | P7 — 清扫不可救 SKU 加入广告黑名单 | `scheduler_daemon.py` 09:45 | 主路径 | 与 `cap_bid_by_floor` 联动, 自动剔除水下连续 N 天 SKU |
| `scripts/guard_anomaly_alert.py` | P8 — 守门员异常率监控 + 邮件 | `scheduler_daemon.py` 09:50 | 主路径 | reject_pct ≥ 阈值告警, 拒改原因 Top-N |
| `scripts/campaign_capacity_audit.py` | P10 — PLA Campaign 容量审计 | 手动 / 仪表盘 | 主路径 | 输出 saturated/available 列表, 仪表盘消费 |
| `scripts/batch_smart_bid.py` | 智能 Bid 调价 (P14: A/B 实验感知) | `scheduler_daemon.py` Tue 10:00 | 主路径 | 命中实验 SKU 用 arm_bid 覆盖 tier 默认 |
| `scripts/bid_rollback.py` | Bid 异动回滚 | `scheduler_daemon.py` Tue 11:00 | 主路径 | 守门员告警的快速复原通道 |
| `scripts/cro_sentinel.py` | CRO 告警与恶化 SKU 输出 | `scheduler_daemon.py` 10:30 / 手动 | 主路径 | 7d 下滑或恶化比例触发 |
| `scripts/cro_effect_audit.py` | 已完成动作效果验证与月报输入 | `scheduler_daemon.py` 09:55 / 手动 | 主路径 | 为 threshold feedback 提供依据 |
| `scripts/cro_image_refresh.py` | CRO 队列主图刷新执行器 | `scheduler_daemon.py` 10:15 / 手动 | 主路径 | 消费 `cro_action_queue` 的 `image_refresh` |
| `scripts/cro_fill_specifics.py` | CRO 队列 specifics 修复执行器 | `scheduler_daemon.py` 10:20 / 手动 | 主路径 | 消费 `fill_specifics` |
| `scripts/cro_promote.py` | CRO promote 执行器 | `scheduler_daemon.py` 10:25 / 手动 | 主路径 | 已推广提 bid，未推广通过 `create_ad_safe` 安全开广告 |
| `scripts/cro_send_offer.py` | low_cvr → interested buyers 保本限时 offer | `scheduler_daemon.py` 10:35 / 手动 | 主路径 | 地板价 = PricingEngine 费率推导最低净利率 5%; 折扣 ≤10%; 30 天频控; 禁止还价 |
| `scripts/cro_delist.py` | CRO 下架候选与确认链路 | 周一邮件 / 手动 | 主路径 | 保持 magic-link 人工确认 |
| `scripts/cro_title_rewrite.py` | 零曝光+广告打满 SKU 的标题关键词增强 | 仅手动 (不进调度) | 主路径 | 默认 dry-run; `--apply` 需 `--yes`; 只用 SKU 自身 aspects 真实值, 30 天重写防抖 |
| `scripts/cro_threshold_feedback.py` | 将效果反馈写入 pending thresholds | 手动 | 专用工具 | 与 shadow / promote 配套 |
| `scripts/cro_threshold_shadow.py` | threshold promote 前的 shadow gate | 手动 / 调度配套 | 主路径 | 先比对再决定 promote |
| `scripts/cro_promote_thresholds.py` | 周日阈值推广 | `scheduler_daemon.py` Sun 02:30 | 主路径 | 只推广通过 shadow 的候选 |
| `scripts/cro_ops_snapshot.py` | S131-S140 运维治理快照 / 容量样本 / DB DR drill | `scheduler_daemon.py` Sun 03:00 / 手动 | 主路径 | 输出 `logs/cro_ops_snapshot.json`, UI 只读消费 |

## 3. `tools/` 运维工具

| 文件 | 责任 | 触发方式 | 状态 | 备注 |
|------|------|----------|------|------|
| `tools/refresh_token.py` | 刷新 eBay OAuth token | 手动 / 排障 | 主工具 | 发布前授权异常时先跑 |
| `tools/fix_specific_images.py` | 单 SKU 图片恢复 | 手动 | 应急工具 | 必须保留 GigaB2B 签名参数，只删除 `x-oss-process` |
| `tools/fix_all_images.py` | 批量图片恢复 | 手动 | 应急工具 | 仅用于明确确认 ACTIVE 链接线上图片少于本地源图时 |
| `tools/root_structure_audit.py` | root 目录结构审计 | 手动 | 维护工具 | 不移动/删除文件，先生成迁移清单 |
| `tools/probe_*.py` | live API / 集成探针 | 手动 | 探针 | 不属于 root `pytest`，详情见 `docs/TESTING.md` |

### `tools/local_diagnostics/`

| 路径 | 责任 | 触发方式 | 状态 | 备注 |
|------|------|----------|------|------|
| `tools/local_diagnostics/_*.py` | 本地一次性诊断 / 修复脚本 | 手动 | 本地诊断 | 已从仓库根目录下沉，默认不作为运维入口，也不纳入自动测试 |

补充说明:

- 这些脚本保留前导 `_` 命名，继续作为本地忽略文件处理。
- 运行时缓存现已集中到 `cache/`，包括 `cache/ad_data_cache.json` 和 `cache/performance_cache.json`。
- 服务端仍兼容读取旧的根目录缓存文件名，但新的缓存写入不应再落回仓库根目录。

## 4. `src/plugins/` 责任矩阵

| 路径 | 责任 | 触发方式 | 状态 | 备注 |
|------|------|----------|------|------|
| `src/plugins/inventory_sync/sync_service.py` | 大建库存与价格同步 | `daily_tasks.py` | 主路径 | 推荐从主调度进入 |
| `src/plugins/inventory_sync/daily_sync.py` | 库存同步独立脚本 | 手动调试 | 调试入口 | 保留，但不再作为默认调度入口 |
| `src/plugins/active_listing_optimizer/daily_optimize.py` | 已刊登标题优化 | 手动 / 显式启用 | 受控入口 | 默认停用；只有 `ENABLE_SCHEDULED_TITLE_OPTIMIZATION=1` 时才允许定时运行 |
| `src/plugins/terapeak_research/research_client.py` | 市场研究客户端 | 被改价 / 报告调用 | 核心依赖 | 不是直接入口 |
| `src/plugins/terapeak_research/research_cli.py` | 市场研究 CLI | 手动 | 专用工具 | 用于独立研究 |
| `src/plugins/terapeak_research/intelligence_service.py` | 市场情报服务 | 内部调用 | 核心依赖 | 不作为一线入口 |
| `src/plugins/title_optimizer/` | 示例标题优化插件 | UI 插件 | 示例 / 非主路径 | 当前不是主流程依赖 |

## 4b. `src/utils/` 发布质量边界

| 文件 | 责任 | 触发方式 | 状态 | 备注 |
|------|------|----------|------|------|
| `src/utils/listing_quality_gate.py` | 生成链接质量门 | 分析后 / READY 审计 / dry-run / live publish 前 | 主路径 | 产品画像、草稿规范化、质量阻断 |
| `src/utils/title_sanitizer.py` | 标题清洗与 80 字符安全裁剪 | READY 审计 / publish / active revise | 主路径 | 禁止再用原始 `[:80]` 硬截断 |
| `src/utils/publish_validation.py` | 测量和类目基础校验 | READY 审计 / publish | 主路径 | 不接受占位长宽高重量 |
| `src/utils/publish_autofix.py` | 占位 item specifics 和单值字段清洗 | publish / audit | 主路径 | 不为测量字段生成占位值 |
| `src/utils/publish_aspect_completion.py` | required aspects 补全 | publish | 主路径 | `Set Includes` 等推断必须受产品族约束 |
| `src/utils/dimension_helpers.py` | 产品尺寸、重量、描述测量同步和 packageWeightAndSize | 全链路 | 主路径 | package dims 不得伪装为 item dims |

这些工具是发布质量边界，不应在临时脚本里复制同一套类目、测量或 item specifics 规则。

## 5. 价格守门员 / Bid / 成本生态 (P1-P14)

| 模块 | 责任 | 触发 | 依赖表 |
|------|------|------|--------|
| `src/services/pricing_engine.py` | 死线计算 (P1) | 全链路 | — |
| `src/services/repricing_guard.py` | 跨脚本守门员 + 拒改原因日志 (P2/P6) | smart_reprice / smart_bid / batch_publish | `logs/_guard_decisions.jsonl` |
| `src/services/pricing_history.py` | 每日 price/bid 历史 (P9) | `daily_tasks.py` | `pricing_history` |
| `src/services/cost_history.py` | 成本快照 + 异动 (P13) + 死线联动 (P17) | `daily_tasks.py` | `cost_history` |
| `src/services/bid_experiment.py` | A/B 实验 (P12) + smart_bid 接入 (P14) | 手动 / smart_bid | `bid_experiments`, `bid_experiment_assignments` |
| `src/services/platform_fee_profile.py` | 跨平台 fee profile + Walmart 品类 (P15) | 跨平台调用 | — |
| `scripts/ad_blacklist_cleanup.py` | 不可救 SKU 黑名单 (P7) | 09:45 | `logs/ad_blacklist.json` |
| `scripts/guard_anomaly_alert.py` | 守门员异常率告警 (P8) | 09:50 | `logs/_guard_decisions.jsonl` |
| `scripts/campaign_capacity_audit.py` | Campaign 容量审计 (P10) | 手动 | `logs/campaign_capacity_*.json` |
| `src/web/pages/pricing_guard_dashboard.py` | 仪表盘 — Guard / 历史 / Campaign / 实验 (P16) | Streamlit | 上述全部 |

## 5b. 竞争监控 → 提高转化率 (CRO 主线)

| 模块 | 责任 | 触发 | 依赖 |
|------|------|------|------|
| `src/services/conversion_diagnoser.py` | 漏斗诊断 (no_imp/low_ctr/low_cvr/healthy) + 动作建议 (price_drop/title_refresh/image_refresh/fill_specifics/promote/delist) | 纯逻辑, 由 UI / batch 调用 | — |
| `src/web/pages/competition_monitor.py` Tab 1「🎯 转化率诊断」 | KPI / 漏斗饼图 / P1 动作清单 / 单 SKU 深入 | Streamlit | `conversion_diagnoser` |
| `tests/test_conversion_diagnoser.py` | 15 用例覆盖各漏斗段 + 价格分位 + 排序 | pytest | — |

诊断阈值: CTR≥1.5%, CVR≥2%, STR≥0.05%, 展示≥50 才进入诊断. 价格分位 overpriced≥115% 中位.

## 5c. CRO 运维 / 治理闭环 (S131-S140)

| 模块 | 责任 | 触发 | 备注 |
|------|------|------|------|
| `src/services/cro_canary_release.py` | 灰度 stage 状态机与回滚 | service-layer | 10/30/50/100 分阶段 promote |
| `src/services/cro_ops_runbook.py` | 标准化 runbook 注册与执行 | service-layer | 支持 critical abort / jsonl log |
| `src/services/cro_ab_finalize.py` | A/B 收口决策 | service-layer | 根据显著性 / 样本量 / 运行时长决策 |
| `src/services/cro_postmortem_generator.py` | 事故复盘 Markdown | service-layer | LLM 不可用时模板回退 |
| `src/services/cro_oncall_rotation.py` | 值班轮转 | service-layer | 支持 skip_dates / 通知封装 |
| `src/services/cro_data_retention.py` | JSONL 数据保留与归档 | service-layer | 保守处理坏行，不丢数据 |
| `src/services/cro_capacity_planning.py` | 容量耗尽预测 | service-layer | 输出 severity / critical components |
| `src/services/cro_disaster_recovery.py` | DB 备份 / 校验 / 恢复 / drill | service-layer | 公开接口以单文件数据库备份为主 |
| `src/services/cro_compliance_audit.py` | 合规访问日志 / DSAR 导出 | service-layer | 公开接口要求 lawful basis |
| `src/services/cro_continuous_improvement.py` | 改进建议收集 / 排序 /周报 | service-layer | advisory-only，不直接改生产配置 |
| `src/services/cro_ops_control_plane.py` | 汇总 S131-S140 为 ops 快照 | `scripts/cro_ops_snapshot.py` / UI | 默认只读，DR drill 与容量样本需显式启用 |

## 6. 推荐入口替代关系

### 已刊登类目修复

- 推荐: `python scripts/repair_published_taxonomy.py`
- 配合: `python scripts/audit_fix_active_listings.py`
- 不推荐: `python scripts/fix_listing_categories.py`

原因:

- 新路径已经包含 canonical taxonomy、plausibility、inventory-first revise 等安全逻辑
- 旧脚本依赖过时的固定映射思路，风险更高

### 已刊登图片恢复

- 优先确认: 本地 `collected_products.images` 是否有多张图
- 推荐检查: 直接 GET eBay Inventory `product.imageUrls`
- 只处理: live offer 仍为 `ACTIVE` 的链接
- 不要使用: 会删除全部 query 参数的历史图片清洗逻辑
- 全库扫描: `python tools/scan_image_collapse.py`
- 恢复入口: `python tools/restore_listing_images.py SKU`

原因:

- GigaB2B 图片 URL 的 `x-cc` / `x-cu` / `x-ct` / `x-cs` 是访问签名
- 刊登或 revise inventory 时删除这些参数，会造成 eBay 线上图片列表退化为单图

## 7. 维护建议

新增脚本前先判断它属于哪类：

- 主入口: 会被日常流程长期依赖
- 专用工具: 只在专项修复或运维时手动运行
- 调试入口: 为单独排查保留，不应在 UI 或主文档里当默认命令

如果脚本已被替代：

- 不必马上删除
- 但必须在文档和帮助文案里标明“已过时 / 已替代”
- 不应继续出现在默认操作指引里

如果脚本只是本地排查工具：

- 优先放在 `tools/local_diagnostics/`
- 不要再放回仓库根目录
- 若它依赖 repo root 路径，显式解析仓库根，而不是假设 `__file__` 就在根目录
