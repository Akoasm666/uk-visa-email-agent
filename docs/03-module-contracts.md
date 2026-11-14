# 模块契约（W00）

共享边界。变更状态枚举或本文件中的输入/输出形状前须 TL 确认。

## 总原则

| 模块 | 交出 | 绝不做 |
|---|---|---|
| Email 适配 | 原始记录 ID、授权线索、正文/附件引用 | 签证决策、改案件结论 |
| 案件与授权 | 客户/申请人/案件/参与人、事实版本；**校验并输出 TransitionContext**（客户确认/最终审批/重开授权是否绑定当前版本） | 用主题当授权；用聊天摘要覆盖事实 |
| 文档管线 | 原件引用、人工标注字段候选 | 改原件；S1 不做 OCR 自动确认 |
| 工作流/状态机 | 状态推进、等待、提醒、接管、发件箱；迁移前必须用 case_service 的 TransitionContext 调用 validate_transition | 在模型循环中等待客户；跳过门控条件 |
| 规则与知识 | 适用要求、检查结果、依据、未知原因 | 模型自动发布规则；含糊“通过分” |
| AI 能力 | 候选事实/草稿、来源、建议动作 | 外部副作用（发信/审批/改状态） |
| 人工工作台 | 具体版本的决定 | 万能跳过全部校验 |
| 文件组装 | 清单、哈希、检查结果 | 用未确认事实补空白 |
| 发送控制 | 发送状态与对账结果 | 超时盲重发；向非白名单发送 |

## 共享标识

```text
tenant_id / customer_id / applicant_id / case_id / case_version
email_event_id / evidence_id / requirement_id / review_id / outbox_id
route_pack_id + route_pack_version
model_call_id（若有）
```

每次处理至少关联：`case_id`、`case_version`、`email_event_id`（若由邮件触发）。

## 邮件处理流水线（单事件）

1. 持久化原件并去重  
2. 分类（客户邮件 / 自动回复 / 退信 / 垃圾 / 投诉）  
3. 归属案件（不确定 → 人工归档队列）  
4. 隔离附件；提取本轮新增正文  
5. AI 提出意图与候选事实（只读）  
6. 形成变更提案；关键冲突不自动覆盖  
7. 事务提交事实/证据版本；使受影响审批失效  
8. 规则重算 → 允许的下一步动作集合  
9. 仅在允许动作内生成表达  
10. 校验后写入发件箱；发送前再核版本与接管与白名单  

旧任务晚返回时，不得用旧 `case_version` 结果覆盖新事实。

## S1 最小 API 面（建议，实现可微调路径名）

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/internal/email/inbound` | 收件适配回调/轮询写入 |
| POST | `/internal/email/poll` | 手动/定时拉取测试邮箱 |
| GET | `/cases` | 案件列表（工作台） |
| GET | `/cases/{case_id}` | 案件摘要 |
| POST | `/cases/{case_id}/transition` | 受控状态迁移 |
| POST | `/cases/{case_id}/takeover` | 人工接管/交还 |
| POST | `/cases/{case_id}/evidence/{id}/annotations` | 人工标注 |
| POST | `/cases/{case_id}/reviews` | 审批决定 |
| POST | `/internal/email/outbox` | S1.2：仅无案件 ordinary_reply；白名单门禁；`send_operation_id` 幂等 |
| POST | `/internal/email/inbound` | 模拟入站；原子去重；归案状态先为 unattributed |
| GET | `/health` | 健康检查 |

对外客户无独立门户；Email 即界面。

## 发送硬门禁（S1）

出站必须声明 `OutboundMessageKind`：`ordinary_reply`｜`progress_package`｜`final_package`。  
未知 kind / 未知 `package_kind` / 普通回复夹带材料包 → 均返回 **`VALIDATION_ERROR`**（不使用已废弃的别名枚举）。

材料包发送须同时携带 delivery 给出的 **`package_id` + 实际 `package_kind`**（`final_package`｜`progress_package`｜`handoff_or_termination`）。

### 共用门禁（所有种类）

1. 全部收件人 ∈ 出站白名单  
2. 无 `human_takeover` 阻碍自动发送，或存在匹配的人工发送批准  
3. `case_version` 与发件箱记录一致  
4. 无未处理的更新入站邮件  
5. 非 `system_blocked` / `paused` / `cancelled`

### 普通回复

`ordinary_reply` **禁止**携带 `package_id` 或 `package_kind`；违反 → `VALIDATION_ERROR`。

### 最终材料包审批（按实际包类型，不按自报标签）

是否要求最终包审批由 **实际 `package_kind == final_package`** 决定；  
将最终包标成 `progress_package` **不能**绕过审批。  
声明 kind=`final_package` 时，实际 `package_kind` 也必须是 `final_package`。

最终包人工审批必须**同时绑定**：

1. **案件版本** `case_version`  
2. **材料包** `package_id`  
3. **收件人集合**（精确匹配，大小写不敏感）

任一不匹配 → `FINAL_PACKAGE_APPROVAL_MISMATCH`；缺审批 → `REVIEW_APPROVAL_REQUIRED`。
