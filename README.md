# UK Visa Email Agent（LSH）

精致 demo：以 Email 为入口、案件为中心的英国签证材料准备助手。  
首条业务路线：**Standard Visitor**。首发最终材料包一律人工放行。

## 当前阶段

**S3 已通过**（Visitor W06-V + 模型网关 W07）。下一阶段：**S4 材料包组装 + 发送控制**（粗粒度，一次验收）。

S3 规则门控在 **`standard_visitor` v2**；发布后换绑走 impact / `set_case_active_pack`。v1 行为冻结不变。

| 交付物 | 路径 |
|---|---|
| Visitor 场景 | `GET/POST /visitor-scenarios`（`/run` 为别名）· `rules/visitor_pack.py` |
| 模型网关 | `ai_capabilities/gateway.py` · `GET /cases/{id}/draft/groundedness` |
| 冻结契约 | `docs/` + `src/visa_agent/domain/` |
| 审核台 | `GET /cases/{id}/review-board` · 工作台案件详情 |
| 规则容器 | `src/visa_agent/rules/` → `/rule-packs`、`/cases/{id}/requirements` |
| 文档管线 | `src/visa_agent/document_pipeline/` → `/cases/{id}/evidence` |
| 模型草稿 | `src/visa_agent/ai_capabilities/` → `POST /cases/{id}/draft` |
| 工作台 | `GET /workbench` |
| HTTP | FastAPI：`visa_agent.fastapi_app` |
| Postgres | `docker-compose.yml` → 宿主 `15432` |

## 启动依赖

```bash
cd LSH
docker compose up -d          # Postgres @ 127.0.0.1:15432
```

### 运行服务

```bash
cd LSH
PYTHONPATH=src python3 -m uvicorn visa_agent.fastapi_app:app --host 127.0.0.1 --port 8000
```

### 测试

```bash
cd LSH
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 阶段约定（摘要）

- **S3**：Visitor 标准/转人工/反例（禁 Student 资金公式）；模型输出须 grounded，禁止宣称发送/批准
- **S2.3**：确认/审批历史；`decision_seq` 定最新决定；可重复读快照；驳回门控显示「已驳回」
- **S2.2**：规则包人工发布（`RULE_PUBLISH_TOKEN`）；未知 ≠ 通过；隔离库 `visa_agent_test`
- **S1**：SMTP + 白名单；模型草稿仅合成上下文
