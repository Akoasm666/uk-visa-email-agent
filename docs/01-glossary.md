# 领域名词表（W00）

所有模块必须使用同一含义。冲突时以本表与 `src/visa_agent/domain/` 为准。

| 术语 | 英文标识 | 含义 | 不变量 |
|---|---|---|---|
| 客户 | `Customer` | 购买/接受服务的人；未必是申请人 | 客户 ID ≠ 申请人 ID |
| 申请人 | `Applicant` | 签证申请主体 | 有独立身份与授权记录 |
| 案件 | `Case` | 某申请人在某路线、某轮申请中的办理工作 | 同一客户可有多个案件 |
| 案件版本 | `CaseVersion` | 事实/证据/要求集合的单调版本号 | 任何关键变更递增版本；审批绑定版本 |
| 关联案件组 | `CaseGroup` | 家庭或多人同行 | 共享材料须显式授权 |
| 参与人授权 | `ParticipantGrant` | 谁可看/可提供/可收什么 | 发过邮件 ≠ 授权代表；付款人 ≠ 读资金材料权 |
| 邮件事件 | `EmailEvent` | 原始邮件、服务商回调、线程关系 | 主题/案件号不能单独决定归属 |
| 事实断言 | `FactClaim` | 某来源对某字段的陈述 | 含原文、时间、证据位置 |
| 已接受事实 | `AcceptedFact` | 经确认流程的字段版本 | ≠ 证明现实世界真实 |
| 证据文件 | `EvidenceFile` | 原件、处理副本、页码、提取、归属、版本 | S1 不做 OCR 改写原件 |
| 材料要求 | `Requirement` | 路线/场景下的条件化要求 | 状态见枚举，不用单一完成百分比 |
| 问题/补件 | `OpenQuestion` | 缺什么、为何、向谁、截止、关闭条件 | 已收到待审 ≠ 再催同一文件 |
| 审查决定 | `ReviewDecision` | 谁依据什么批准/驳回哪个版本 | 只对具体版本有效 |
| 交付包 | `DeliveryPackage` | 快照 + 清单 + 校验 + 审批 + 发送状态 | 最终包 / 进度包 / 转人工须分开 |
| 路线包 | `RoutePack` | 版本化规则、来源、决策表、模板、案例 | LLM 不得运行时改写规则 |
| 发件箱记录 | `OutboxMessage` | 决定发送与调用服务商发送分离 | 超时不得盲重发 |
| 白名单收件人 | `OutboundAllowlist` | S1 允许对外发送的测试邮箱集合 | 非白名单一律拒绝发送 |

## 事实状态（`FactStatus`）

`pending_extraction`｜`customer_stated`｜`document_supported`｜`human_verified`｜`conflict`｜`superseded`｜`unknown`

规则：未知不得写成否；模型自报置信度 ≠ 已核实。

## 要求状态（`RequirementStatus`）

`applicability_unknown`｜`applicable_missing`｜`received_pending_review`｜`evidence_accepted`｜`needs_correction`｜`not_applicable`｜`exempted_with_basis`

须区分：不满足资格 / 尚未证明满足 / 无需随申请提交 / 依法豁免。
