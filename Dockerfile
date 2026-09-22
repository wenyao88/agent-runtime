FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY .env.example .env.example

EXPOSE 8000

CMD ["uvicorn", "agent_runtime.api.app:app", "--host", "0.0.0.0", "--port", "8000"]