FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# 迁移文件必须进镜像：compose 的启动命令会在起服务前跑 `alembic upgrade head`
COPY alembic.ini ./
COPY alembic ./alembic

COPY .env.example .env.example

EXPOSE 8000

CMD ["uvicorn", "agent_runtime.api.app:app", "--host", "0.0.0.0", "--port", "8000"]