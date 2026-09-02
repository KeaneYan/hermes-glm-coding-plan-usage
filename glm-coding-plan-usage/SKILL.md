---
name: glm-coding-plan-usage
description: 查询智谱 GLM Coding Plan 按 API Key 的 token 用量与额度占比，生成周报/当日实时报表。用于 coding plan 用量分析、额度耗尽归因、按 key 拆分成本。
---

# GLM Coding Plan 按 key 用量查询与额度分析

数据源：智谱开放平台**控制台内部接口**（非公开 API）。核心结论：公开用量接口（`/api/monitor/usage/quota/limit`、usage-detail、model-usage）**只有账号级数据，apiKey 参数会被忽略**；**按 key 的 token 用量在「财务 → 费用账单 → 费用明细」里**，底层接口 `expenseBillListByDay` / `expenseBillList` 直接返回每行账单的完整 apiKey ID。

## 环境要求

- macOS + Google Chrome，且 Chrome 已登录 `open.bigmodel.cn`（微信扫码登录态）
- Chrome 开启「视图 → 开发者 → 允许 Apple Events 中的 JavaScript」
- 系统设置 → 隐私与安全性 → 自动化：允许终端/agent 向 Chrome 发送 Apple Events

## 接口与认证（2026-08 验证）

- 登录态凭证在 cookie **`bigmodel_token_production`**（非 HttpOnly，JS 可读）。
- 在 bigmodel.cn 页面内用 `fetch` 调 `/api/...`，请求头：
  - `Authorization: <cookie 值>`（**原始值，不加 Bearer**）
  - `Bigmodel-Organization` / `Bigmodel-Project`：从页面 `localStorage` 同名键读取，**不要硬编码**
- 关键接口：
  - Key 清单：`GET /api/biz/v1/organization/{org}/projects/{proj}/api_keys?keyType=1` → `data[]`（apiKey 全量 ID、name、lastUseTime、createTime）
  - **套餐版本**：`GET /api/biz/subscription/list` → `data[]` 里 `status=VALID` 条目含 `productName` + `version`（如 "V2"）
  - 费用明细（**按天**）：`GET /api/finance/expenseBill/expenseBillListByDay?billingMonth=YYYY-MM&billStatus=&modelProductName=&paymentType=&pageNum=1&pageSize=500` → 顶层 `{total, rows[]}`（**无 code 字段**，失败时无 rows）。按天出账，**当天数据不出现**（T+1 延迟）。
  - 费用明细（**按明细**）：`GET /api/finance/expenseBill/expenseBillList?billingMonth=YYYY-MM&pageNum=1&pageSize=500` → 同样 `{total, rows[]}`，**分钟级粒度、当天实时可见**。行字段比按天版多 `timeWindow`（如 "2026-09-02 17:10:00~17:11:00"）、`billingNo`、`billingStatus`、`tokenResourceName` 等。查"今天用了多少"必须用这个接口。
  - 每行：`billingDate`、`apiKey`（全量）、`modelCode`、`tokenType`（输入/输出/缓存命中/**不区分输入输出**——search-prime、web-reader 等工具按次计费走后者）、`usageCount`（tokens）、`apiUsage`（调用次数）、`costPrice`（刊例单价）、`tokenResourceName`（套餐名，含版本字样，可佐证 subscription 检测结果）

## 套餐版本自动检测

**不要问用户套餐版本，直接查** `subscription/list` 的 `version` 字段，零用量账号也可查。周窗口锚点 = `purchaseTime` 的时刻（下单时间起每 7 天）。遇到非 "V2" 取值时按新版积分制口径并如实标注假设。

## 额度口径：旧版 V2 套餐（2026-09 实测验证）

V2 规则（官网「老用户权益说明」+ 账单反推双重验证）：

- **额度单位 = 加权 token**：`消耗 = (输入+缓存命中+输出) × 模型系数 × 时段系数`。**缓存命中 1:1 计入、不打折**
- 模型/时段系数：**glm-5.3 非高峰 ×1 / 高峰 ×3**；**glm-5.3-flash 非高峰 ×0.4 / 高峰 ×1.2**；高峰 = 周一至五 14:00-18:00 UTC+8（周末全天按非高峰）
- 实测封顶（Max V2）：cap_5h ≈ **2.5 亿加权 token**（文档表述"约 1600 prompts/5h"），cap_week ≈ **12.4 亿**（恰为 5×）
- 窗口机制：5h 与周均为**固定窗口**——5h 窗从重置后首笔请求起算 5 小时整窗重置；周窗从下单时刻起 7 天。配额接口 `quota/limit` 的 `nextResetTime` 即窗口终点
- 验证方法：明细 `timeWindow` 按窗口过滤 → ΣusageCount×系数 与官方百分比（100%/29%）互推 cap → 比值恰为 5。账单明细比实时配额滞后约 45 分钟（或被限流后无新行）

## 额度口径：新版积分套餐（2026-07-30 后新购）

- `积分 = (输入×In系数 + 缓存×Cache系数 + 输出×Out系数) / 10000`；系数：glm-5.3 = 6.9/1.7/24；glm-5.3-flash = 2.3/0.56/8；MCP 工具 1.2/次；非高峰 50% 抵扣
- Max 档 5h = 28,000 分、周 = 140,000 分；5h 滚动刷新（每笔消耗满 5h 归还）

## 费用刊例价（账单 costPrice）

订阅行 settlementAmount=0，costPrice 是刊例价，仅用于"按刊例价折算价值消耗"，不是实付：glm-5.3 输入 ¥0.008 / 缓存 ¥0.002 / 输出 ¥0.028 每千 token；flash 约其 1/20；search-prime、web-reader ¥0.05/次。

**禁止把原始 token 量直接相加当"用量占比"**（缓存占比约 97%，严重失真）。三种口径分开：额度占比（V2 加权或新版积分，回答"谁吃额度"）、费用占比（¥ 刊例价，回答"谁烧钱多"）、原始 token 量（无意义，不要报）。

## 聚合口径（重要）

- 每行 = 账期 × key × 模型 × token 类型。tokenType 显式分类：输入/输出/缓存命中分别累加，未知新类型单列并在报告标注，**禁止 else 归输入**。
- **`apiUsage` 在同一 (key, 账期, 模型) 的多行上重复出现**：先按 (key, day, model) 取 max，再对 (key, day) 求和。
- 接口分页返回 `{total, rows[]}`：必须逐页拉取并校验累计行数 == total，取不齐报错退出，**绝不输出缩水报告**（静默漏报是最危险的失败模式）。
- 聚合一律按 **apiKey 全量 ID** 为键，名称仅用于展示（同名 key 附 ID 尾 4 位消歧）。
- 「按天」接口 T+1 出账当天不出现；查当天用量用「按明细」接口。周日无出账记录时不得无条件断言「均已出账」，如实说明。
- 周跨月时（如周一在 3 月、周日在 4 月）要分别拉两个 `billingMonth`。

## 一键脚本

```
python3 scripts/glm_weekly_usage.py [--monday YYYY-MM-DD]
```

- 默认统计「上一个完整周」（周一~周日，Asia/Shanghai），输出聊天工具友好的 Markdown 报告到 stdout。
- 自动：找/开 Chrome 的 bigmodel 标签页（钉住 window id + tab index 执行 JS，poll 前校验 tab URL）、读 cookie+localStorage、分页拉 key 清单和费用明细、按 apiKey ID 聚合。
- 报告列：输入 / 输出 / 缓存命中 / 调用次数 / 占比（占比按输入+输出计）。
- 退出码：0 成功；1 参数错误；**2 = NEED_LOGIN**（登录过期）；3 Chrome/页面/权限不可用（含 Apple Events JS 未开启、tab 被切走、轮询超时）；4 接口/解析错误。

## 登录过期处理（NEED_LOGIN 流程）

1. `open -a "Google Chrome" "https://open.bigmodel.cn/login"`（或复用已有标签页导航）。
2. 页面含 220×220 的 base64 微信登录二维码 `<img>`：用 AppleScript `execute javascript` 取 `img[width>=200][src^="data:image"]` 的 src，base64 解码存本地文件，作为图片发给用户，提示「长按 → 识别图中二维码」。
3. 每 6 秒轮询标签页 URL，离开 `/login` 即登录成功，重跑脚本。二维码约 2 分钟过期，过期重新抠图。
4. AppleScript 注意：`execute javascript` **不 await Promise** —— 一律 `window.__var=null; fetch(...).then(...存 window.__var)` 发射后，轮询 `window.__var || "pending"` 取值。

## 报告格式纪律

- 聊天工具投递时编号用「1、」或「第N条｜」，**避免 `1.` 开头**（部分 IM 的 Markdown 会把编号渲染错乱）。
- 报告发正文全文，不要只发「已生成」。
- 异常（登录过期/接口失败）只发一行说明，**不得编造用量数据**。

## 背景事实（排障时有用）

- 配额接口：`/api/monitor/usage/quota/limit`（5h token 窗 + 周 token 窗 + MCP 月额度，账号级百分比 + nextResetTime）。
- 典型 coding agent 负载缓存命中率 ~95%（上下文每轮重发），属于正常现象。
- key 可能被用户动态增删分发给多人使用，报告里 key→名称映射必须实时从 key 清单拉，不要缓存旧名单。
