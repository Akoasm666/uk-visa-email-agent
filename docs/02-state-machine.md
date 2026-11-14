# 案件主状态与正交控制状态（W00）

## 主状态（`CaseStatus`）

线性服务进度。任一非终态收到相关新材料时，先处理输入并重算受影响检查，不强制整案重开。

| 状态 | 进入 | 离开 | S1 系统动作 |
|---|---|---|---|
| `lead` | 新咨询邮件 | 范围与联系对象初步确认 | 建临时联系，少要敏感材料 |
| `intake_authorization` | 客户接受服务范围 | 权限足以处理 | 确认申请人、代表、隐私告知 |
| `route_scope_confirmed` | 关键背景可用 | 路线包与服务边界确定 | 确认 Visitor 等可服务场景 |
| `collect_and_verify` | 个性化要求已生成 | 适用材料已收且无未处理输入 | 分批提问、收附件、更新缺口 |
| `issue_resolution` | 缺件/矛盾/未知 | 回到原阶段或经原阶段前向边继续 | 精准补件或人工 |
| `draft_preparation` | 达到起草门槛 | 草稿可供确认 | 生成说明/填写参考（可人工） |
| `customer_confirmation` | 有待确认版本 | 明确确认绑定版本 | 含糊回复不算通过 |
| `final_review` | 客户确认有效 | 有效审批绑定当前版本 | **人工放行** |
| `ready_to_deliver` | 确认+审批均有效 | 发件任务入队 | 封存包、发送前再校验 |
| `sent_awaiting_ack` | 邮件进入发送管线 | 收件确认绑定本交付 / 重发 / 异常 | 跟踪退信 |
| `delivered` | 本交付收件确认有效 | 关闭或因新事实重开 | 售后说明 |
| `closed` | 结束或取消 | 仅授权新请求可重开 | 停提醒、归档 |

## 正交控制状态（`CaseControlFlags`）

| 标志 | 含义 |
|---|---|
| `waiting_customer` | 等待客户回复 |
| `waiting_third_party` | 等待第三方 |
| `human_takeover` | 人工接管；阻止自动发送 |
| `system_blocked` | 系统阻塞 |
| `paused` | 暂停 |
| `cancelled` | 取消 |

## 迁移校验职责

| 职责 | 负责层 |
|---|---|
| 解析并校验 TransitionContext | **`case_service`** |
| 迁移前调用 `validate_transition(current, target, ctx)` | **`workflow`** |
| 写入确认 / 审批 / 重开授权 / 进入 issue 时的 resume_status / 收件确认 | **`human_workbench`** 与受控入站流程 |

### 按目标状态统一门控（单一规则源）

实现中 `_requirements_for(current, target)` **同时**决定「是否需要 TransitionContext」与「校验哪些前置条件」，禁止两处分开维护。

| 目标状态 | 前置条件（均相对当前案件版本 / 当前交付） | 失败码 |
|---|---|---|
| `final_review`（**任意**来源，含从 `ready_to_deliver` / `sent_awaiting_ack` 回退） | 当前版本客户确认 | `CUSTOMER_CONFIRMATION_REQUIRED` |
| `ready_to_deliver`（**任意**来源） | 当前版本客户确认 **且** 当前版本人工最终审批 | `CUSTOMER_CONFIRMATION_REQUIRED` / `REVIEW_APPROVAL_REQUIRED` |
| `delivered` | 收件确认绑定**本次** `delivery_id` | `DELIVERY_RECEIPT_REQUIRED` |
| `closed`→`lead`/`intake`；`delivered`→收集/问题 | 重开授权 | `REOPEN_UNAUTHORIZED` |

旧确认 + 新审批 **不能** 进入 `ready_to_deliver`（确认与审批都必须等于当前 `case_version`）。

### 问题处理保留原业务阶段

进入 `issue_resolution` 时，`case_service` 必须记下当时主状态为 `resume_status`。

离开 `issue_resolution`（除 `closed`）时：

1. 必须提供 `resume_status`
2. 目标只能是 **`resume_status` 本身**，或 **`resume_status` 在主图上的直接前向边**（不得从 `lead` 的问题态直接跳到 `draft_preparation`）
3. 若目标本身另有门控（如 `final_review`），仍按上表校验

## 提醒（S1）

- DB `due_at` + 定时任务；默认约 2 / 5 个工作日
- 新材料处理中、人工接管、暂停、退信时取消相关自动提醒
