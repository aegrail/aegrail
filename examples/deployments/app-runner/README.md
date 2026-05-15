# aegrail App Runner sample

A minimal FastAPI service showing the env-var-driven deployment pattern
for AWS App Runner. See [`docs/deployments/aws-app-runner.md`](../../../docs/deployments/aws-app-runner.md)
for the full narrative.

## Files

- `agent_service.py` — the FastAPI app; calls `Agent.from_env()` and
  routes to OpenRouter
- `Dockerfile` — non-root, port 8080, uvicorn
- `requirements.txt` — aegrail 0.2.6 + FastAPI + uvicorn
- `deploy.sh` — `aws apprunner create-service` + ECR push + IAM role
- `teardown.sh` — deletes service, ECR repo, IAM role
- `.dockerignore` — excludes shell scripts and docs from the image

## Run it

```bash
export OPENROUTER_API_KEY=sk-or-...
./deploy.sh
# (note the printed Service URL)
curl -X POST https://<service-url>/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"In one sentence, what is aegrail?"}'
./teardown.sh
```

## Test locally first

```bash
docker build -t aegrail-sample:local .
docker run --rm -p 8080:8080 \
  -e AEGRAIL_AGENT_IDENTITY=app-runner-sample/v1 \
  -e AEGRAIL_BUDGET_USD=0.10 \
  -e AEGRAIL_BUDGET_TOKENS=4000 \
  -e AEGRAIL_BUDGET_WALL_SECONDS=60 \
  -e AEGRAIL_BUDGET_MAX_TOOL_CALLS=5 \
  -e AEGRAIL_EGRESS_ALLOWLIST=openrouter.ai \
  -e AEGRAIL_AUDIT_STDOUT=1 \
  -e OPENROUTER_API_KEY="$OPENROUTER_API_KEY" \
  aegrail-sample:local
```
