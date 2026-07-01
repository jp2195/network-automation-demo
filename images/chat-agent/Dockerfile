# Pre-baked chat-agent image (console chat lane). Carries the Pydantic AI
# agent runtime + FastAPI/uvicorn for the long-lived /api/chat service.
# No pygnmi/puresnmp: chat v1 exposes no device-level tools (read-only
# Prometheus/Loki/NetBox + the corridor_impact graph walk). Same
# convention as ai-analyst: the workflow-scripts ConfigMap mount overrides
# /scripts/* at runtime; the baked copy keeps the image runnable standalone.
FROM python:3.14-slim

RUN pip install --no-cache-dir "pydantic-ai-slim[openai]" fastapi uvicorn

# Build context is workloads/eventing/.
COPY scripts/ /scripts/

CMD ["python3", "/scripts/chat_server.py"]
