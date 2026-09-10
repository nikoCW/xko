# 部署与模拟验收

## 配置

| 环境变量 | 用途 |
|---|---|
| OKX_MODE | 默认demo；read_only禁止写；live用于单独部署的实盘环境 |
| OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE | OKX模拟或对应实盘账户的密钥，仅服务端秘密变量 |
| MCP_AUTH_TOKEN | 至少32字符的随机令牌；有任一私有凭据但配置不完整/无令牌时启动失败 |
| OKX_API_BASE | www.okx.com、eea.okx.com或us.okx.com的HTTPS地址；按注册地区选择，不任意填代理地址 |
| MCP_ALLOWED_HOSTS | 逗号分隔的实际Host；Render会自动加入RENDER_EXTERNAL_HOSTNAME |
| STATE_DB | 持久化磁盘上的SQLite路径；不能使用重启丢失的临时目录 |
| MONITOR_ENABLED | true启用后台只读监控，false关闭；关闭时不接受新提醒 |
| MONITOR_INTERVAL_SECONDS | 最小60秒，不含接口耗时 |
| ALERT_WEBHOOK_URL / ALERT_WEBHOOK_TOKEN | 可选接收提醒的HTTPS接口及Bearer令牌；无配置时只写本地事件 |

不将密钥写入代码、Git或聊天。OKX密钥选择专用子账户的读取/交易用途并限制IP，不启用提现。此代码不调用资金转账、提现或账户模式修改接口。部署环境和账户真实权限仍由你管理。

## Render

使用 `render.yaml` 创建服务会提供持久化磁盘、随机MCP令牌、demo模式和后台监控。另在服务端添加OKX模拟密钥；本仓库不包含任何凭据。使用单个实例/worker；不要在两台独立磁盘的实例运行同一账户。

`/health` 只证明服务进程可响应，不证明OKX连接、风控、监控和webhook正常。认证后调用 `get_rules` 检查模式和monitorLastSuccess，再调用 `account_overview` 核对账户。

支持自定义Header的MCP客户端向 `/mcp` 发送 `Authorization: Bearer <MCP_AUTH_TOKEN>`。客户端如果只能OAuth连接，需要先配置匹配的OAuth授权网关。不能为绕开客户端认证限制而移除服务认证。

## 使用顺序

1. `get_rules` → `account_overview`，核对demo、账户余额、持仓模式及未完成订单。
2. `analyze_trade` 或 `run_live_trader`；不满足规则时等待，不凑条件。
3. `preview_order` 传十进制字符串。开仓必须给结构止损/目标，可不填size让风控算上限；平仓必须填size；合约leverage须与OKX当前逐仓设置一致。
4. 显示完整预览，用户回复 `确认执行 <previewId>` 后调用 `execute_preview`。
5. 继续 `reconcile_order` 直到实际订单状态和保护被核实。unknown不能重发；protection_pending先检查OKX原始订单并处理保护问题。
6. `plan_spot_grid` 给无杠杆规划，`create_price_alert` 可监控边界；不会创建网格机器人。
7. `get_events` 检查事件及delivered字段。配置webhook前不会向外发送提醒。

## 模拟验收（上线前仍需实际完成）

- 用独立模拟密钥核对余额、USDT现货/合约规格和当前杠杆；检查后台日志无密钥。
- 先只读分析。有效信号出现后，使用满足最小数量的小额单独预览并确认，核对FOK全成/全撤、实际数量和附带保护。
- 在OKX模拟账户核对止损/目标触发结果、合约平多平空方向与reduceOnly效果。若FOK附带保护不被交易所接受，保持拒绝，不去掉保护重试。
- 网络超时测试应先用Mock环境，不人为破坏真实持仓保护。核对重启后原预览不会重发，状态日志不丢失。
- 以价格提醒验证运行和webhook，接收端用事件id去重；故障投递为至少一次，不保证恰好一次。
- 检查取消保护的警告和暂停开仓行为。解除暂停只放行规则检查，不会自动重启机器人或补单。

策略回测和模拟验收未完成前，不能从离线测试推断实盘收益或生产可靠性。本次代码交付没有完成以上私人账户验收步骤。
