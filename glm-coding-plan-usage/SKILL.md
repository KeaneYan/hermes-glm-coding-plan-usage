---
name: glm-coding-plan-usage
description: 查询智谱 GLM Coding Plan 按 API Key 的 token 用量并生成周报。用于每周用量报告 cron 或手动查询。
---

# GLM Coding Plan 按 key 用量查询与周报

数据源：智谱开放平台**控制台内部接口**（非公开 API）。核心结论：公开用量接口（`/api/monitor/usage/quota/limit`、usage-detail、model-usage）**只有账号级数据，apiKey 参数会被忽略**；**按 key 的 token 用量在「财务 → 费用账单 → 费用明细」里**，底层接口 `expenseBillListByDay` 直接返回每行账单的完整 apiKey ID。

## 接口与认证（已验证 2026-08-28）

- 登录态：本机 Chrome 已登录 `open.bigmodel.cn`（微信扫码登录）。会话凭证在 cookie **`bigmodel_token_production`**（非 HttpOnly，JS 可读）。
- 调用方式：在 bigmodel.cn 页面内用 `fetch` 调 `/api/...`，请求头：
  - `Authorization: <cookie 值>`（**原始值，不加 Bearer**）
  - `Bigmodel-Organization` / `Bigmodel-Project`：取自页面 `localStorage` 同名键（当前值 org-bbc882A57Ab04a52A5AA2B126973B983 / proj_944e488d414b4011987035Ce1f213724，**运行时不要硬编码，从 localStorage 读**）
- 关键接口：
  - Key 清单：`GET /api/biz/v1/organization/{org}/projects/{proj}/api_keys?keyType=1` → `data[]`（apiKey 全量 ID、name、lastUseTime、createTime）
  - 费用明细（**按天**）：`GET /api/finance/expenseBill/expenseBillListByDay?billingMonth=YYYY-MM&billStatus=&modelProductName=&paymentType=&pageNum=1&pageSize=500` → 顶层 `{total, rows[]}`（**无 code 字段**，失败时无 rows）。**按天出账，当天数据不出现**（T+1 延迟）。
  - 费用明细（**按明细**，2026-09-02 实测发现）：`GET /api/finance/expenseBill/expenseBillList?billingMonth=YYYY-MM&pageNum=1&pageSize=500` → 同样 `{total, rows[]}`，**分钟级粒度、当天实时可见**。行字段比按天版多 `timeWindow`（如 "2026-09-02 17:10:00~17:11:00"）、`billingNo`、`billingStatus`、`tokenResourceName` 等；tokenType 含 输入/输出/缓存命中/**不区分输入输出**（search-prime、web-reader 等工具类计费走后者）。查"今天用了多少"必须用这个接口。
  - 每行：`billingDate`、`apiKey`（全量）、`modelCode`、`tokenType`（**输入/输出/缓存命中/不区分输入输出**，2026-08 实测缓存命中占全量约 97%，旧版只认输入/输出会把缓存静默归入输入，严重虚高）、`usageCount`（tokens）、`apiUsage`（调用次数）、`tokenResourceName`（套餐名）

## 套餐版本自动检测（2026-09-02 验证）

**不要问用户套餐版本，直接查**：`GET /api/biz/subscription/list` → `data[]` 里 `status=VALID` 的条目含 `productName`（如 "GLM Coding Max"）+ **`version` 字段（"V2" 等）**，零用量账号也可查。旁证：账单行 `tokenResourceName`（"GLM Coding Max V2 - 季"）。周窗口锚点 = `purchaseTime` 的时刻（下单时间起每 7 天）。新版积分套餐的 version 取值未实测（可能为新标识）。脚本判定映射（2026-09-02 ZCode 复审定稿）：`version==="V2"`→V2 加权口径；version 为非 V2 值→按新版积分制口径并在报告标注「假设，未实测」；version 缺失或无 VALID 订阅→版本未知，只报原始量并标注口径不确定；subscription/quota 接口失败→报错退出，不套用任何公式。

## 单价与额度口径（2026-09-02 实测验证）

**裤哥账号 = 旧版 V2 套餐**（tokenResourceName="GLM Coding Max V2 - 季"），**不适用**官网新版积分制。V2 规则（官网"老用户权益说明"+账单反推双重验证）：

- **额度单位 = 加权 token**：`消耗 = (输入+缓存命中+输出) × 模型系数 × 时段系数`。**缓存命中 1:1 计入、不打折**
- 模型/时段系数：**glm-5.3 非高峰 ×1 / 高峰 ×3**；**glm-5.3-flash 非高峰 ×0.4 / 高峰 ×1.2**；高峰 = 周一至五 14:00-18:00（周末全天按非高峰）
- 实测封顶：cap_5h ≈ **2.5 亿加权 token**（文档表述"约 1600 prompts/5h"），cap_week ≈ **12.4 亿**（恰为 5×，实测 12.1 亿吻合，文档"约 8000 prompts/周"）
- 窗口机制：5h 与周均为**固定窗口**——5h 窗从重置后首笔请求起算 5 小时整窗重置（实测 15:18→20:18）；周窗从下单时刻起 7 天（实测 10:01 锚点）。配额接口 `quota/limit` 的 nextResetTime 即窗口终点
- 验证方法：明细 `timeWindow` 按窗口过滤 → ΣusageCount×系数 = 100%/29% 反推 cap → 比值恰为 5。账单明细比实时配额滞后 ~45min（或被限流后无新行）
- 新版积分制（2026-07-30 后新购用户，裤哥不用）：积分=(入×6.9+缓存×1.7+出×24)/10000，Max 5h=2.8万分。遇到别人的账号注意区分

**费用刊例价**（账单 costPrice，订阅行 settlementAmount=0，仅作价值折算）：glm-5.3 输入 ¥0.008/缓存 ¥0.002/输出 ¥0.028 每千 token；flash 约其 1/20；search-prime、web-reader ¥0.05/次

**禁止把原始 token 量直接相加当"用量占比"**。三种口径分开：额度占比（V2 加权，回答"谁吃额度"）、费用占比（¥刊例价，回答"谁烧钱多"）、原始 token 量（无意义，不要报）。

## 聚合口径（重要）

- 每行 = 账期 × key × 模型 × token 类型。tokenType 显式分类：输入/输出/缓存命中分别累加，未知新类型单列并在报告标注，**禁止 else 归输入**。
- **`apiUsage` 在同一 (key, 账期, 模型) 的多行上重复出现**：先按 (key, day, model) 取 max，再对 (key, day) 求和（同 key 同天多模型时直接按 (key,day) 取 max 会低估）。
- 接口分页返回 `{total, rows[]}`：必须逐页拉取并校验累计行数 == total，取不齐报错退出，**绝不输出缩水报告**（cron 静默漏报是最危险的失败模式）。
- 聚合一律按 **apiKey 全量 ID** 为键，名称仅用于展示（同名 key 附 ID 尾 4 位消歧——实测存在两个「默认项目」）。
- 账单「按天」接口 T+1 出账，当天数据不出现；查当天用量改用「按明细」接口 `expenseBillList`（实时）。周日无出账记录时不得无条件断言「均已出账」，如实说明。
- 周跨月时（如周一在 3 月、周日在 4 月）要分别拉两个 `billingMonth`。

## 一键脚本（首选）

```
python3 ~/.hermes/skills/research/glm-coding-plan-usage/scripts/glm_weekly_usage.py [--monday YYYY-MM-DD]
python3 ~/.hermes/skills/research/glm-coding-plan-usage/scripts/glm_weekly_usage.py --today
```

- 默认统计「上一个完整周」（周一~周日，Asia/Shanghai），输出微信格式 Markdown 报告到 stdout。
- `--today`：当日实时报告（按明细接口，分钟级）——自动检测套餐版本并套用对应额度口径（V2 加权 token / 新版积分），输出今日按 key 用量+费用折算、当前 5h 窗口按 key 加权占比与隐含上限、周窗口进度。5h/周窗口边界取自配额接口 nextResetTime 反推。
- 自动：找/开 Chrome 的 bigmodel 标签页（钉住 window id + tab index 执行 JS，poll 前校验 tab URL）、读 cookie+localStorage、分页拉 key 清单和费用明细、按 apiKey ID 聚合。
- 报告列：输入 / 输出 / 缓存命中 / 调用次数 / 占比（占比按输入+输出计）。
- 退出码：0 成功；1 参数错误；**2 = NEED_LOGIN**（登录过期）；3 Chrome/页面/权限不可用（含 Apple Events JS 未开启、tab 被切走、轮询超时）；4 接口/解析错误。

## 登录过期处理（NEED_LOGIN 流程）

1. `open -a "Google Chrome" "https://open.bigmodel.cn/login"`（或复用已有标签页导航）。
2. 页面含 220×220 的 base64 微信登录二维码 `<img>`：用 AppleScript `execute javascript` 取 `img[width>=200][src^="data:image"]` 的 src，base64 解码存 `/tmp/glm_login_qr.png`，作为图片发给裤哥，提示「长按 → 识别图中二维码」。
3. 每 6 秒轮询标签页 URL，离开 `/login` 即登录成功，重跑脚本。二维码约 2 分钟过期，过期重新抠图。
4. AppleScript 注意：`execute javascript` **不 await Promise** —— 一律 `window.__var=null; fetch(...).then(...存 window.__var)` 发射后，轮询 `window.__var || "pending"` 取值。

## 报告格式纪律（微信投递）

- 编号用「1、」或「第N条｜」，**禁止 `1.` 开头**（微信 Markdown 会把编号渲染错乱）。
- 报告发正文全文，不要只发「已生成」。
- 异常（登录过期/接口失败）只发一行说明，**不得编造用量数据**。

## 背景事实（排障时有用）

- 账号 level=max（Coding Plan Max 季付），配额接口：`/api/monitor/usage/quota/limit`（5h token 窗 + 周 token 窗 + MCP 月额度，账号级百分比）。
- 用量集中在工作日 8–18 点（午休空窗）、周末近零；GLM-5.3 占 99.9%+，缓存命中率 ~95%。
- key 由裤哥动态增删（人名命名，分给他人用），报告里 key→名称映射必须实时从 key 清单拉，不要缓存旧名单。
