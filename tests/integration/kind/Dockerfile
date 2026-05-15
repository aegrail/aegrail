FROM python:3.12-slim

# Pin to the version this test is validating. Bump on each release
# that should be kind-validated; the test orchestrator can override
# via --build-arg AEGRAIL_VERSION=...
ARG AEGRAIL_VERSION=0.2.4

RUN pip install --no-cache-dir aegrail==${AEGRAIL_VERSION}

COPY agent.py /agent.py

ENV PYTHONUNBUFFERED=1

CMD ["python", "/agent.py"]
