FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY demo/runbook.yaml ./demo/runbook.yaml
RUN pip install --no-cache-dir ".[postgres,ai]"

USER nobody
EXPOSE 8000
CMD ["uvicorn", "incident_agent.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
