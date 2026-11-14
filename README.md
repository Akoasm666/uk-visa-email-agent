# UK Visa Email Agent（LSH）

精致 demo：以 Email 为入口、案件为中心的英国签证材料准备助手。  
首条业务路线：**Standard Visitor**。首发最终材料包一律人工放行。

## 当前阶段

**S2.2 已通过**（规则容器 W05）。下一阶段：**S2.3 审核台增强（旧审批失效可见）**。

| 交付物 | 路径 |
|---|---|
| 冻结契约 | `docs/` + `src/visa_agent/domain/` |
| 规则容器 | `src/visa_agent/rules/` → `/rule-packs`、`/cases/{id}/requirements` |
| 文档管线 | `src/visa_agent/document_pipeline/` → `/cases/{id}/evidence` |
| 模型草稿 | `src/visa_agent/ai_capabilities/` → `POST /cases/{id}/draft` |
| 收件 | 人工确认白名单邮箱；可选 `POST /internal/email/poll` |
| 案件服务 | `src/visa_agent/case_service/` |
| 工作流迁移 | `src/visa_agent/workflow/` |
| 工作台 | `GET /workbench` |
| HTTP | FastAPI：`visa_agent.fastapi_app` |
| Postgres | `docker-compose.yml` → 宿主 `15432` |
| 本地文件 | `data/files/` |

## 启动依赖

```bash
cd LSH
docker compose up -d          # Postgres @ 127.0.0.1:15432
```

### 运行服务（优先 FastAPI）

```bash
cd LSH
PYTHONPATH=src python3 -m uvicorn visa_agent.fastapi_app:app --host 127.0.0.1 --port 8000
```

健康检查：`GET http://127.0.0.1:8000/health`  
工作台：`GET http://127.0.0.1:8000/workbench`

### 测试

```bash
cd LSH
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

自动化测试对模型 **mock**；真实模型 + 白名单 SMTP 验收：

```bash
LSH_LIVE_S1_4=1 PYTHONPATH=src python3 scripts/s1_4_live_acceptance.py
```

证据写入 `data/s1_4_evidence/`。脚本验证模型草稿 + 白名单 SMTP；**收件由人工在白名单邮箱确认**（不必配 IMAP）。

## S2.2 规则容器约定

- 版本化 rule pack；来源 URL + 获取日期（见 `docs/05-visitor-rule-pack-outline.md`）
- 发布默认关闭；需在 `.env` 配置 `RULE_PUBLISH_TOKEN`，请求头 `X-Rule-Publish-Token`（或 `Authorization: Bearer`）校验
- 种子只插入尚不存在的 pack 版本，从不改写已有规则内容；内容变更须新版本再发布
- 未发布 / 无来源 / 无规则项不可发布；换绑版本会 bump `case_version` 并使旧审批失效
- 正式评估只用当前绑定包；未知 / 转人工未判定 / 待审 **不算通过**
- `GET /rule-packs/{id}/impact?from_version=` 按租户列出仍绑旧版的案件
- S2.2 测试：`VISA_AGENT_SKIP_APP_BOOT=1` 后对隔离库 `visa_agent_test` 调用 `bootstrap`

## S1 约定

- 邮件：**SMTP**（IMAP 收信）；仅白名单测试邮箱；合成材料
- 模型：硅基流动 `zai-org/GLM-5.3-Flash`；草稿输入仅为合成上下文，AI 模块不发信/不审批/不改状态
- 存储：PostgreSQL + 本地文件；开发库 Compose `15432`
