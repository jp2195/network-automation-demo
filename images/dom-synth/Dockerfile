# Pre-baked dom-synth image. Replaces the deployment's
# `pip install valkey && exec python3 /app/dom_synth.py` shim.
# /app/dom_synth.py is also the ConfigMap mount path so the dashboard
# can keep iterating without rebuilding the image — the mount overrides
# the baked copy at runtime.
FROM python:3.14-slim

RUN pip install --no-cache-dir valkey

COPY dom_synth.py /app/dom_synth.py

CMD ["python3", "/app/dom_synth.py"]
