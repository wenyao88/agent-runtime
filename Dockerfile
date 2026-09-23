# 基础镜像可覆盖：`docker build --build-arg PYTHON_IMAGE=...`（或在 compose 的 build.args 里指定）。
# 本机实测中 `python:3.12-slim` 拉不下来（网络/镜像源），参数化后可直接用本地已有镜像，不必改代码。
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

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