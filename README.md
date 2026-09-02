# glm-coding-plan-usage

Hermes Agent skill：查询智谱 **GLM Coding Plan** 的按 API Key token 用量，生成周报 / 当日实时报表，并按套餐规则计算**额度占比**——回答"谁把 5 小时额度吃光了"这类问题。

## 能做什么

- **按 key 拆分用量**：控制台费用明细接口直接返回每行账单的完整 apiKey ID（公开监控接口只有账号级数据，apiKey 参数会被忽略）
- **当日实时报表**：「按明细」接口分钟级粒度、当天可见；「按天」接口 T+1 延迟
- **额度占比归因**：自动检测套餐版本（`subscription/list` 的 `version` 字段），按对应规则加权：
  - **旧版 V2**：`(输入+缓存命中+输出) × 模型系数 × 时段系数`（缓存 1:1 计入；glm-5.3 高峰 ×3，flash 高峰 ×1.2）
  - **新版积分制**：`(入×6.9 + 缓存×1.7 + 出×24) / 10000`
- **费用折算**：按账单 `costPrice` 刊例价折算各 key 的价值消耗（订阅内实付为 0）
- 一键生成 Markdown 周报，可直接投递到聊天工具

## 核心方法

不碰任何公开 API 额度猜测，直接复用本机 Chrome 里 open.bigmodel.cn 的登录态（cookie `bigmodel_token_production` + localStorage 的 org/project），通过 AppleScript `execute javascript` 在页面内 fetch 控制台内部接口，分页拉全 `{total, rows[]}` 并校验行数完整，按 apiKey 全量 ID 聚合。

为什么缓存命中不能忽略：coding agent 每轮把完整对话历史重发给模型，缓存命中约占 token 总量 97%。只统计"输入+输出"会把额度消耗低估一个数量级；而原始 token 直接相加又会把缓存单价低 4 倍的事实抹掉——所以本 skill 严格区分**额度口径**（加权 token / 积分）、**费用口径**（¥刊例价）、**原始量**（不使用）三种占比。

## 示例输出

```
GLM Coding Plan 周用量报告（2026-08-31 ~ 2026-09-06）

| Key | 输入 | 输出 | 缓存命中 | 调用次数 | 占比 |
|---|---|---|---|---|---|
| alice | 4.3M | 324.8k | 100.5M | 536 | 30.9% |
| bob   | 3.0M | 219.1k | 68.8M  | 567 | 21.1% |
| 合计  | 13.7M | 1.3M | 292.4M | 2076 | 100% |
```

## 环境要求

- macOS + Google Chrome，Chrome 已登录 open.bigmodel.cn
- Chrome 开启「视图 → 开发者 → 允许 Apple Events 中的 JavaScript」
- 终端/agent 有向 Chrome 发送 Apple Events 的自动化权限

## 安全边界

- **只读**：只调用 GET 接口，不修改账号任何配置
- 凭证不出本机：cookie/localStorage 仅在本地页面内使用，输出只有统计数字
- 登录过期时会引导用户重新扫码，不会尝试任何凭证爆破/绕过

## 安装与验证

```bash
hermes skills tap add KeaneYan/hermes-glm-coding-plan-usage
hermes skills install glm-coding-plan-usage
# 或用 skills CLI 查看
npx skills add KeaneYan/hermes-glm-coding-plan-usage --list
```

## 文件说明

- `SKILL.md`：接口清单、认证方式、V2/积分双口径公式、聚合纪律、登录过期处理流程
- `scripts/glm_weekly_usage.py`：周报一键脚本（`--monday YYYY-MM-DD` 指定周）

## 免责声明

本 skill 使用智谱控制台未公开的内部接口，接口字段与套餐规则可能随时变动；所有额度系数以官方文档与账单实际数据为准。本工具仅用于查看与分析自己账号的用量，不构成任何计费争议依据。
