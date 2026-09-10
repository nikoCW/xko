# OKX Live Trader 2.0

面向个人账户的 OKX MCP 服务：**行情筛选、交易规则、账户检查、确认后下单、网格规划、后台价格提醒**。

默认 `demo` 模拟环境。未配置 OKX 凭据时仍可分析公开行情。代码提供实盘适配，但本次改写没有连接私人账户、运行真实交易或验证策略收益；启用前必须完成模拟账户验收。

## 功能与边界

| 功能 | 当前实现 |
|---|---|
| 现货买入 / 卖出 | USDT 现货，现金交易，FOK 限价全成或全撤；禁止卖空 |
| 合约开多 / 开空 / 平多 / 平空 | USDT 线性永续及交割，逐仓、单向持仓；开仓1–3倍，平仓 `reduceOnly` |
| 入场与风控 | 已收盘突破及回踩、放量、4H趋势、仓位计算、成本后盈亏比、现金储备、当日回撤 |
| 下单与撤单 | 精确预览 → 用户确认 → 重验 → 单次提交 → 按原ID核对；普通和策略撤单 |
| 交易所止盈止损 | 与入场一起提交附带保护；成交后核对实际策略单；失败则暂停新增风险并提醒 |
| 网格 | 无杠杆现货网格规划：区间、每格价格/数量、成本与库存最坏损失；**不启动交易所机器人，不自动补单** |
| 合约网格 | **规则说明与人工审核范围，当前不支持启动或执行** |
| 自动提醒 | 常驻进程轮询上穿/下穿价格；持久化、去重、冷却、订单/保护状态事件；可配置 HTTPS webhook |
| 无人值守自动交易 | **未开放**；后台只读，所有账户变更逐次确认 |

## 交易规则

[完整中文规则](skills/okx-live-trader/references/trading-rules.md) · [代码审查与限制](docs/REVIEW.md) · [部署及模拟验收](docs/DEPLOYMENT.md)

默认单笔止损风险 ≤ 净值 **0.5%**，单标的名义本金 ≤ **20%**，交易后保留 **30%** 可用 USDT，成本后盈亏比 ≥ **2:1**，当日观测净值回撤达到 **3%** 暂停新增风险。同标的开仓冷却15分钟。

首版采用保守的账户隔离：有未平合约、普通/策略挂单、机器人资金、负债或同币库存时，不新增重叠仓位；仍可预览减仓、撤单和核对状态。`derivative_risk=3%` 是总体政策上限；当前一次仅允许一个新增合约风险，实际限制更严格。

## 常用工具

- 分析：`get_rules`、`market_scan`、`compare_symbols`、`get_candles`、`run_live_trader`、`analyze_trade`
- 账户与执行：`account_overview`、`preview_order`、`execute_preview`、`reconcile_order`、`preview_cancel_order`
- 网格：`plan_spot_grid`
- 提醒：`create_price_alert`、`list_price_alerts`、`set_price_alert_enabled`、`get_events`
- 控制：`pause_new_entries`（只改变本服务新开仓开关，不关闭现有仓位）

价格、数量以十进制字符串传入。现货数量单位为基础币；合约数量为张，按 `ctVal × ctMult` 换算，不能把张数当作币数。

## 本地启动

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/uvicorn server:app --host 127.0.0.1 --port 8000
```

健康检查 `/health`；MCP `/mcp`。环境变量参考 `.env.example`，由进程管理器注入，文件不会自动加载。

私有账户必须配置至少32字符的 `MCP_AUTH_TOKEN`，所有 MCP 请求带 `Authorization: Bearer <token>`。支持 Bearer Header 的客户端可直接连接；只支持 OAuth 的客户端需要合适的 OAuth 网关，本仓库**没有 OAuth 授权服务器**。不能继续使用原版 README 的公开“无身份验证”配置连接私有账户。

部署配置包含持久化磁盘。模拟与实盘应分开部署，使用不同密钥、令牌和数据库。不要把 API Key 发到聊天或提交到 Git。服务只允许交易/撤单写接口，不提供转账或提现。
