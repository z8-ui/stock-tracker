# 复发问题清单（REVIEW_CHECKLIST）

> 规则：同一问题在审查中出现 **≥2 次**才录入；录入后不再讨论问题本身，只核对是否执行。
> 每条格式：现象 → 为什么危险 → 现在由哪段代码/测试守着 → 检查方式。
> 配套：`docs/REVIEW_PROTOCOL.md`。

---

## 1. 空值被吃成 0（伪零）

- **现象**：后端把"源没给"表示为 `None`，前端某处（`Math.abs(null)`、`|| 0`、`.get(k, 0)`）
  又把它变成 0，页面显示 `0.00 亿`。
- **为什么危险**：把"没有数据"伪装成"真实零值"，正是本项目最重要的红线之一。
- **守在哪**：
  - `data_service.yuan_to_yi()` 缺失即抛，不返回 0；
  - `get_market_flow()` 缺中/小单时置 `None` + `_flow_partial`；
  - `static/js/format.js`：`chartValue()` / `pieItems()` 保留 null，`pieTip()` 输出"暂无数据"；
  - `chart_builder`：`items` 允许 `None`，颜色比较前 `(i["value"] or 0)` 保护。
- **检查方式**：
  - `python3 -m unittest tests.test_pie_null_js tests.test_market_flow_fields`
  - 源码断言：`app.js` 中不得出现 `Math.abs(i.value)`。
- **首次出现**：S2 前置 hotfix（前端 null）｜**复发次数**：2

---

## 2. 改了口径但没处理旧缓存

- **现象**：字段含义修正后，磁盘缓存（`_data_cache.json`）里仍是旧口径的值，页面继续展示错数。
- **为什么危险**：代码"改对了"但用户看到的还是错的，且只在重启/换缓存后才消失，
  这类问题在截图验收里看不出来。
- **守在哪**：
  - `get_market_flow()` 返回 `_flow_fields_v = 2`；
  - 读到缺该标记的缓存 → 自动 `force=True` 重取一次并覆盖。
- **检查方式**：
  - `python3 -m unittest tests.test_market_flow_fields.TestLegacyCacheSelfHeal`
  - 手工：把旧缓存写回 → 首次请求应触发一次真实抓取，返回值带 `_flow_fields_v=2`。
- **首次出现**：S2 前置 hotfix（中单/小单字段）｜**复发次数**：2

---

## 3. 文档状态与代码/证据不同步

- **现象**：实现与采集都完成了，但文档另一节还写着"未完成/BLOCKED"；
  或状态写 `VERIFIED` 但证据只有"公开约定"。
- **为什么危险**：审查者按文档给结论，过期描述会直接导致误判；
  证据不足却标 VERIFIED 会让整份报告失去可信度。
- **守在哪**：
  - `docs/V2.1_S2_EASTMONEY_FLOW_DESIGN.md` §2.3 / §2.5 / §6 三处口径必须一致；
  - 证据分级：强（数值恒等/命名字段对齐）> 中（多标的复现）> 仅约定（公开字典）。
- **检查方式**：
  - `grep -n "BLOCKED\|VERIFIED\|PARTIAL" docs/V2.1_S2_EASTMONEY_FLOW_DESIGN.md`
    人工确认每一处与当前实现一致；
  - 每轮交付前把"已完成 / 仍未完成 / 状态"三行贴进报告开头。
- **首次出现**：S2 前置 hotfix（§6 过期）｜**复发次数**：2

---

## 待观察（尚未复发，暂不录入）

- 超时口径不统一（`data_service` 里 5/6/8/10 秒四套）→ 若 S2 迁移时再次各写一套，则录入。
- `except: pass` 吞异常 → 若新 Provider 里再出现，则录入。
- 发布包误含本地文件（`config_local.py` 曾因 `.gitignore` 的 `data/` 规则误伤而暴露过同类风险）→
  由 `tests/test_package_scanner.py` 守着，复发即录入。
