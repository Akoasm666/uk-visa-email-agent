# 错误分类（W00）

模块间传递错误时使用稳定 `code`。HTTP 映射仅作参考。

| code | 含义 | 默认处置 | HTTP |
|---|---|---|---|
| `VALIDATION_ERROR` | 输入不符合契约 | 拒绝，返回字段级原因 | 400 |
| `UNAUTHORIZED` | 无参与人/角色权限 | 拒绝并审计 | 403 |
| `NOT_FOUND` | 资源不存在 | 拒绝 | 404 |
| `CONFLICT_VERSION` | 案件版本冲突 / 丢失更新 | 重读案件，不覆盖 | 409 |
| `IDEMPOTENCY_REPLAY` | 重复事件，已处理 | 返回原结果，不新建副作用 | 200 |
| `EMAIL_UNATTRIBUTED` | 无法安全归案 | 人工归档队列 | 202 |
| `EMAIL_DUPLICATE` | 重复邮件/回调 | 忽略副作用 | 200 |
| `ATTACHMENT_REJECTED` | 类型/大小/恶意/损坏 | 隔离并通知支持格式 | 422 |
| `OUTBOUND_NOT_ALLOWLISTED` | 收件人不在白名单 | **禁止发送** | 403 |
| `OUTBOUND_STATUS_UNKNOWN` | 发送结果不明 | 待核实队列，禁止盲重发 | 409 |
| `TAKEOVER_ACTIVE` | 人工接管中禁止自动动作 | 仅人工通道 | 409 |
| `CUSTOMER_CONFIRMATION_REQUIRED` | 缺少绑定当前版本的客户确认 | 拒绝进入 final_review / ready_to_deliver | 409 |
| `REVIEW_APPROVAL_REQUIRED` | 缺少绑定当前版本的人工最终审批 | 拒绝 ready_to_deliver / 最终包发送 | 409 |
| `REOPEN_UNAUTHORIZED` | 缺少重开授权 | 拒绝 closed/delivered 重开 | 403 |
| `FINAL_PACKAGE_APPROVAL_MISMATCH` | 最终包审批与版本/包/收件人不一致 | 拒绝发送最终包 | 409 |
| `DELIVERY_RECEIPT_REQUIRED` | 缺少绑定本次交付的收件确认 | 拒绝进入 delivered | 409 |

说明：未知出站类型、普通回复夹带材料包等仍返回 **`VALIDATION_ERROR`**（不另增未使用的枚举别名）。
| `RULE_UNKNOWN` | 规则不适用或未知 | 暂停相关结论，转规则负责人 | 422 |
| `FACT_CONFLICT` | 关键事实冲突未解 | 建冲突项，停依赖判断 | 422 |
| `MODEL_OUTPUT_INVALID` | 结构/枚举/缺来源 | 有限修复或转人工 | 422 |
| `MODEL_GROUNDEDNESS` | 引入无依据事实 | 退回，限制重生次数 | 422 |
| `DELIVERY_GATE_FAILED` | G0–G6 质量门失败 | 阻止封存/发件 | 422 |
| `SYSTEM_BLOCKED` | 需运维/人工对账 | 设 `system_blocked` | 503 |
| `NOT_IMPLEMENTED` | 本期未实现（如 OCR） | 明确提示 | 501 |

## 重试策略（摘要）

| 故障 | 策略 |
|---|---|
| 短暂网络/限流 | 有上限退避（初值 3） |
| 输出结构错误 | 带错修复 1 次 → 备用或人工 |
| 事实冲突/依据不足 | **不**换模型投票；补证或人工 |
| 发件结果不明 | 查询/等待/人工对账，禁止盲重试 |
