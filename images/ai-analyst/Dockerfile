# Pre-baked AI-analyst image (wave-2 feature 4, advisory lane). Carries
# the Pydantic AI agent runtime + read-only tool deps: pygnmi (Get only —
# see scripts/gnmi_readonly.py), puresnmp for the FRR cabinets, slack-sdk
# for the optional notification. Same convention as eventing-py: the
# workflow-scripts ConfigMap mount overrides /scripts/* at runtime; the
# baked copy keeps the image runnable standalone.
FROM python:3.12-slim

RUN pip install --no-cache-dir "pydantic-ai-slim[openai]" pygnmi puresnmp slack-sdk

# Build context is workloads/eventing/.
COPY scripts/ /scripts/

# No CMD — the WorkflowTemplate sets `command: [python3]` and
# `source: |` (runpy.run_path).
