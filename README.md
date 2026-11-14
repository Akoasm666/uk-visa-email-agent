# UK Visa Email Agent（LSH）

精致 demo：以 Email 为入口、案件为中心的英国签证材料准备助手。  
首条业务路线：**Standard Visitor**。首发最终材料包一律人工放行。

## 当前阶段

**S1.2 Email 适配待验收**（W00 已冻结；S1.1 骨架已交付）

| 交付物 | 路径 |
|---|---|
| 冻结契约 | `docs/` + `src/visa_agent/domain/` |
| S1 集成约定 | `docs/06-s1-integration-acceptance.md` |
| 配置 | `.env`（本地，不入库）+ `src/visa_agent/config.py` |
| HTTP | FastAPI：`visa_agent.fastapi_app`；标准库兜底：`visa_agent.http_app` |
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
# 或标准库：
PYTHONPATH=src python3 -m visa_agent.http_app
```

健康检查：`GET http://127.0.0.1:8000/health`

### 测试

```bash
cd LSH
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## S1 约定

- 邮件：**SMTP**（IMAP 收信）；账号与白名单暂空，注册好后写入 `.env`
- 模型：硅基流动 `zai-org/GLM-5.3-Flash`
- 存储：PostgreSQL + 本地文件；本机 5432 未验证登录，开发库用 Compose `15432`
