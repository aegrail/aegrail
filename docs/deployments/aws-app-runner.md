# Deploying aegrail-protected agents on AWS App Runner

This guide takes a containerised agent service from local Docker to a running
AWS App Runner endpoint, with all aegrail policy — identity, budget, egress
allowlist, audit destination — supplied at deploy time via environment
variables. The application code calls `Agent.from_env()` and never names the
values.

This guide was last verified end-to-end on **2026-05-15** against
**aegrail 0.2.6**, App Runner in **us-east-1**, with OpenRouter as the LLM
provider. The sample lives in
[`examples/deployments/app-runner/`](../../examples/deployments/app-runner/).

## What this gives you

- An HTTPS endpoint serving a small FastAPI app
- `Agent.from_env()` configured from App Runner runtime env vars
- Hard kill-switches on USD / tokens / wall-seconds / tool calls
- Egress allowlist enforced in-process (defence-in-depth before the
  forthcoming sidecar engine)
- Tamper-evident audit chain streaming to **CloudWatch Logs** via stdout

## What it does NOT yet give you

- Sidecar-level egress proxy enforcement (App Runner does not support
  sidecars; that's a Kubernetes / ECS / Fargate story — see those guides)
- Per-user identity from a Cognito JWT — wire `user_id=` on
  `agent.session(...)` from your auth middleware

## Prerequisites

- AWS CLI v2 authenticated against an account where you can create
  App Runner services, ECR repos, and IAM roles
- Docker (any recent version)
- An LLM provider key. The sample uses
  [OpenRouter](https://openrouter.ai) (`OPENROUTER_API_KEY`); swap in
  any provider you like by editing `agent_service.py`.

## The 90-second tour

```bash
cd examples/deployments/app-runner
export OPENROUTER_API_KEY=sk-or-...
./deploy.sh        # builds + pushes + creates service; ~3 min
./teardown.sh      # deletes service, ECR repo, IAM role
```

## What's actually happening

### 1. The application code

`agent_service.py` (excerpt — see the full file for the rest):

```python
from aegrail import Agent

AGENT = Agent.from_env()    # reads AEGRAIL_* env vars set by App Runner

@app.post("/chat")
def chat(body: ChatIn):
    with AGENT.session(user_id="app-runner-demo", task="chat") as s:
        reply, model, tin, tout = call_openrouter(body.message)
        s.record_llm(model=model, tokens_in=tin, tokens_out=tout,
                     cost_usd=estimate_cost(model, tin, tout))
        return {"reply": reply, "aegrail_state": s.state_snapshot}
```

No code mentions a USD budget, a token cap, an allowlisted host, or where
audit goes. All of that is operator-controlled.

### 2. The Dockerfile

```dockerfile
FROM python:3.12-slim
RUN useradd -m -u 10001 app
USER app
WORKDIR /home/app
ENV PATH="/home/app/.local/bin:${PATH}"
COPY --chown=app:app requirements.txt /home/app/
RUN pip install --user -r requirements.txt
COPY --chown=app:app agent_service.py /home/app/
EXPOSE 8080
CMD ["uvicorn", "agent_service:app", "--host", "0.0.0.0", "--port", "8080"]
```

Non-root user (App Runner best practice), port 8080.

### 3. The App Runner runtime environment variables

```
AEGRAIL_AGENT_IDENTITY    = app-runner-sample/v1
AEGRAIL_BUDGET_USD        = 0.10
AEGRAIL_BUDGET_TOKENS     = 4000
AEGRAIL_BUDGET_WALL_SECONDS = 60
AEGRAIL_BUDGET_MAX_TOOL_CALLS = 5
AEGRAIL_EGRESS_ALLOWLIST  = openrouter.ai
AEGRAIL_AUDIT_STDOUT      = 1
OPENROUTER_API_KEY        = (your key)
OPENROUTER_MODEL          = openai/gpt-4o-mini
```

`deploy.sh` passes these on `aws apprunner create-service` via the
`SourceConfiguration.ImageRepository.ImageConfiguration.RuntimeEnvironmentVariables`
block.

For production: move secrets (the OpenRouter key) into AWS Secrets Manager
and reference them via `RuntimeEnvironmentSecrets` instead of
`RuntimeEnvironmentVariables`. App Runner will inject them as env vars at
runtime — `Agent.from_env()` reads them identically.

### 4. The audit chain in CloudWatch

Because the service runs with `AEGRAIL_AUDIT_STDOUT=1`, every audit event
lands as a single-line JSON record in:

```
/aws/apprunner/<service-name>/<service-id>/application
```

Each event carries `prev_hash` + `event_hash` so a CloudWatch Logs query (or
an export to S3 + Athena) can verify the chain end-to-end.

Verifier sample:

```python
import json
from aegrail.audit import AuditEvent, verify_chain

with open("cloudwatch-export.jsonl") as f:
    events = [AuditEvent.model_validate(json.loads(line)) for line in f if line.strip()]
ok, bad = verify_chain(events)
assert ok, f"audit chain broken at event index {bad}"
```

### 5. Observed costs of the test run

For the verification deploy on 2026-05-15:
- App Runner: ~3 minutes of runtime, 0.25 vCPU / 0.5 GB → **~$0.01**
- ECR storage: <1 MB image, deleted within the same hour → **~$0.00**
- OpenRouter (gpt-4o-mini, 1 chat call): **<$0.0001**

App Runner has no idle cost when the service is paused; the listed price is
charged only when handling traffic, with a small provisioning floor.

## Production checklist

Before you let real traffic through this:

- [ ] Move `OPENROUTER_API_KEY` into AWS Secrets Manager; reference via
      `RuntimeEnvironmentSecrets`, not `RuntimeEnvironmentVariables`.
- [ ] Set `AEGRAIL_AGENT_IDENTITY` to a stable, audited string (e.g.
      `support-bot/v1.4.2-prod`) — your audit chain joins on this.
- [ ] Set budgets that match your operational appetite — Aegrail's
      kill-switch is hard, not soft. A `BudgetExceeded` exception will
      crash the session.
- [ ] Set `AEGRAIL_EGRESS_ALLOWLIST` to the exact set of LLM provider hosts
      you intend to allow. Comma-separated `fnmatch`-style patterns, e.g.
      `api.openai.com,*.anthropic.com`.
- [ ] Replace `agent.session(user_id="app-runner-demo", ...)` with the real
      caller identity from your auth middleware.
- [ ] Register tools explicitly on the Agent. The sample registers none
      because the demo only does LLM calls; in real systems you'll have a
      tool registry. Tools are passed in code via `Agent.from_env(tools=...)`.
- [ ] Stream CloudWatch Logs to long-term storage (S3 / Glacier) for
      retention beyond the App Runner default. Audit chain only verifies
      what you keep.

## Teardown

```bash
./teardown.sh
```

Deletes the App Runner service, the ECR repo (with all images), and the
IAM role.

## Reference

- AWS App Runner docs: https://docs.aws.amazon.com/apprunner/
- aegrail env vars: see `aegrail.Agent.from_env` and `aegrail.Budget.from_env`
  docstrings.
