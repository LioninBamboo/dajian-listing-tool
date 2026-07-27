# 交接任务书 — 参数错与幻觉收尾（2026-07-27）

分支：`codex/live-audit-semantic-guard`（不要切换、不要新建）
前序任务书：`docs/HANDOFF_CRITICAL_BACKLOG_20260726.md` —— **红线部分仍然全部有效**

---

## 0. 背景：存量清到哪一步了

存量批量清理已完成，CRITICAL **142 → 83 行**（-42%）。剩下 83 行的构成：

| 性质 | 行 | SKU | 本次处理 |
|---|---|---|---|
| **参数错**（数值/标志与源对不上） | 51 | 46 | ✅ 本任务 |
| **幻觉**（源里根本没有的声明） | 14 | 12 | ✅ 本任务 |
| 类目错 | 18 | 18 | ❌ 用户明确要求往后放，**本次不碰** |

> 🔴 **不要动 `category_mismatch` 的任何一条。**
> 已知其中 4 条是帐篷被误判成 "Ice Chests & Coolers"（`cooler weather` 触发），
> 匹配器已修但审计报告是修之前生成的。这类历史上出过事故
> （床头柜被判成床架、控制台桌被判成沙发），必须人工过。

另：容量同义词表已由我方修好（`two-seater`≡`2 person`、`queen size`≡`queen`），
原 12 条 `semantic_capacity` 里 **8 条误报会自动消失**，只剩 4 条需人工。

---

## 1. 三档工作，清单已生成

| 档 | 文件 | 量 | 处理方式 |
|---|---|---|---|
| A | `logs/fix_assembly16.txt` | 16 SKU | **可自动修** |
| B | `logs/review_dimensions21.csv` | 21 行 | **人工圈选后再改** |
| C | `logs/review_human18.csv` | 18 行 | **纯人工，只出建议** |

---

## 2. A 档 — 组装标志 16 条（自动修）

问题形如：

> `Assembly Required must be Yes because supplier source says Assembly Required Yes`

live 标"免安装"而源说"需组装"。**确定性、有明确源依据、退款风险最高**
（买家收到一箱零件）。

```bash
PYTHONPATH="C:\Users\poonx\Dajian_Listing_Tool" .venv/Scripts/python.exe scripts/audit_fix_active_listings.py --sku-file logs/fix_assembly16.txt --live --issue-type assembly_required_mismatch
```

先看 dry-run 输出，确认每条都是 `No → Yes`（或反向）且方向与源一致，再加 `--fix`。

> 🔴 若出现任何一条方向与源相反、或源本身没有 Assembly Required 值 → 停止报告。

---

## 3. B 档 — 尺寸 21 行（先给人看，不许整批覆盖）

`logs/review_dimensions21.csv` 已按差异百分比排序，附我方标注。**你的任务是补全
"决定"列并交回，不是直接改。**

已识别的三类，直接写进表里供参考：

**① 长宽对调（3 条）** — `N719P383161B/D/K`
live 长=78.7 宽=25.8，源长=25.8 宽=78.7。**数值都对，只是位置调换**。
这类改起来最安全，但仍需人工确认是"我们填反了"而不是"源填反了"。

**② 差异 <5%（2 条）** — `W2699P504456`(4.9%)、`W2700S00160`(3.7%)
可能是单位换算或四舍五入，不一定是错。

**③ 显著差异（16 条）** — 例如：

| SKU | 字段 | live | 源 | 差 |
|---|---|---|---|---|
| WF319383AAK | height | **350.0** | 21.1 | 1559% |
| W3098P470272/69 | weight | 57.87 | **300.0** | 81% |
| W5368P497703 | length | 18.9 | 64.57 | 71% |

350 英寸高（近 9 米）显然是 live 错。但 **不能反推"源永远对"**：

> 🔴 历史事故：供应商把**包装尺寸**填进了整装字段，我方照抄源值反而把
> 正确的 live 数据改坏了（71 英寸沙发那次）。已有 `suspect_source_dimensions`
> 守卫，但它不覆盖全部情形。
>
> **凡是"采用源值会让产品尺寸变得不合常理"的（比如沙发长度变成 25 寸），
> 一律标存疑，不许改。**

交回时每行必须填 `采用源` / `保留live` / `存疑` 三者之一。**等验收人放行后才改。**

---

## 4. C 档 — 幻觉 14 + 容量 4 = 18 行（纯人工，只出建议）

`logs/review_human18.csv`。

**幻觉 14 行**主要是**部件材质**：沙发的 `foam`（坐垫填充）、帐篷的 `steel`（骨架）、
`plywood`、`canvas`、`chenille`。判断标准只有一条：

> **该材质在源描述全文（不只是材质属性字段）里能不能找到依据？**
> - 能找到 → 提取器把部件材质当成了主材质，属误报，标"误报"
> - 找不到 → 真幻觉，标"需删除"，并写明改成什么

已核实过一个真幻觉的例子：`W3636P456625` 帐篷，源全文只有 cotton/oxford，
live 却 claim 钢架 —— 这种必须删。

**容量 4 行**：

| SKU | live | 源 |
|---|---|---|
| W714S01590 | 6 seater | 4 person |
| W1885S00407/08/09 | king size | 4 person / 4 seater |

后三条是 3-in-1 沙发床，"king size"说的是床面尺寸、"4 person"说的是坐卧人数，
可能两者都对。**这是判断题，不是计算题，交人工。**

> 🔴 C 档**只许出建议清单，不许写入 live**。

---

## 5. 红线（只能触发停止，执行方无权重新解释）

历史教训：曾有执行方在正确触发"批量回滚>2条则停止"后，自行把它重新解释成
"批次栅栏——结束本批、继续下一批"。**红线的唯一合法反应是：停下来，报告，等指示。**

- 动了任何 `category_mismatch` → 停止
- B 档未经放行就写入 → 停止
- C 档写入 live → 停止
- 采用源值后尺寸变得不合常理却仍写入 → 停止
- 单批回滚 > 2 条 → 停止
- 任何 end-listing / relist（必须原位更新 listing_id/offer_id）→ 停止
- 修改或删除既有测试断言 → 禁止（只能新增）
- `git add -A` / `git add .` → 禁止。工作区有并行 session 的改动，只能 `git add <具体路径>`
- 修改 `.env` / 杀重启调度守护进程 / `git push` → 禁止
- 绕过 PricingEngine / RepricingGuard / EbayPublisher → 禁止

---

## 6. 已知情况，不要顺手改

- **定时任务每天 12:30 会自己推 40 条**，且会**用推送后的状态覆盖回滚快照**。
  若某条的 backup 时间戳晚于你的推送时间 → **不要 rollback 那一条**，
  会把推送后状态当"原状"写回去。停下来报告。
- **提取器把颜色和表面处理当材质**（`glossy`、`green`）—— 已知技术债，
  本次不处理，别顺手改材质词表。
- 验证描述一律看 **offer 的 `listingDescription`**，不是 inventory 那份
  （后者截断到 4000 字符，验它会误判）。
- `EbayOAuthService()` 默认是 **SANDBOX**。生产脚本一律传
  `os.getenv('EBAY_ENVIRONMENT','PRODUCTION')`，否则 401。
- 测试基线 **2137 passed**：
  `$env:PYTHONPATH='C:\Users\poonx\Dajian_Listing_Tool'; .\.venv\Scripts\python.exe -m pytest tests/ -q`

---

## 7. 交付物

1. A 档：dry-run 输出 + 实际修复的 SKU 清单 + 逐条 live 复核结果
2. B 档：填好"决定"列的 CSV（**先交回等放行，不要改**）
3. C 档：18 行的建议清单，每行写明"误报"或"需删除+改成什么"，附源依据
4. 期末重跑审计，给出 CRITICAL 前后对比（当前基线 83 行 / 73 SKU）
5. 全量测试结果

**不要自评"任务完成"。** 每档报完数据等放行；遇到红线或判断不了的，停下来问。
