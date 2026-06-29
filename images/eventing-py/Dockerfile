# Pre-baked eventing image. Replaces the per-step `pip install slack-sdk
# valkey` and per-script `pip install` ceremonies inside the Argo Workflow
# Templates. The scripts ConfigMap mount at /scripts/ is still the
# read-write source of truth for the Python source — this image just
# carries the pinned dependencies and a copy that runs without the mount
# for one-off use (the mount overrides /scripts/* at workflow runtime).
FROM python:3.14-slim

RUN pip install --no-cache-dir slack-sdk valkey

# Build context is workloads/eventing/. Copy the scripts dir to /scripts/
# so the same path works whether the workflow mounts the ConfigMap or not.
COPY scripts/ /scripts/

# No CMD — the WorkflowTemplate sets `command: [python3]` and
# `source: |` (runpy.run_path) per step.
