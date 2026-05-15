# Deploying aegrail-protected agents

aegrail is **operator-controlled**: the developer declares behavior
(`Agent.from_env(tools=...)`); the operator declares policy (every
`AEGRAIL_*` env var, plus any LLM-provider key). The same container
image moves from laptop → staging → prod without code changes.

This directory has one guide per target platform. Every guide follows
the same shape so once you've read one, you can skim the others.

## The cross-platform principle

Two classes of variable end up on the container, and they belong in
different stores. Mixing them is the most common production mistake.

### 1. Non-secret operator config — plain env

These describe behavior, not credentials. They are safe to log, safe
to display in the cloud console, safe to ship in `kubectl describe`.

| Env var | What it sets | Sensitive? |
|---|---|---|
| `AEGRAIL_AGENT_IDENTITY` | agent role string | no |
| `AEGRAIL_BUDGET_USD` / `_TOKENS` / `_WALL_SECONDS` / `_MAX_RECURSION` / `_MAX_TOOL_CALLS` | hard kill-switch ceilings | no |
| `AEGRAIL_EGRESS_ALLOWLIST` | comma-separated host patterns | no |
| `AEGRAIL_AUDIT_FILE` / `AEGRAIL_AUDIT_STDOUT` | where audit chain lands | no |
| `AEGRAIL_INTERCEPT` | `1` enables auto-installed in-process interceptors | no |
| `OPENROUTER_MODEL` / `OPENAI_MODEL` / similar | model identifier | no |

Use the platform's normal env-var mechanism (ConfigMap, `--set-env-vars`,
`RuntimeEnvironmentVariables`, etc.).

### 2. Secrets — KMS-backed managed secret store

LLM provider keys and any downstream service credentials. Hard rule:
**never** put these in plain env, ConfigMaps, container images, or
git. Always reference an encrypted store; the platform decrypts at
container start and injects the value as an env var.

| Env var | Belongs in |
|---|---|
| `OPENROUTER_API_KEY` | secret store |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / etc. | secret store |
| Any database password / downstream service token | secret store |

| Platform | Secret store | Encryption |
|---|---|---|
| AWS App Runner | Secrets Manager (preferred) or SSM Parameter Store SecureString, referenced via `RuntimeEnvironmentSecrets` | KMS (AWS-managed or customer-managed CMK) |
| AWS ECS / Fargate | Secrets Manager or SSM, referenced via `secrets` in task def | KMS (CMK recommended) |
| Google Cloud Run | Secret Manager, referenced via `--set-secrets` | Google-managed by default; CMEK via Cloud KMS for SOC2/ISO27001 |
| Azure Container Apps | Container Apps secrets, optionally synced from Key Vault | Microsoft-managed by default; customer-managed via Key Vault Premium / HSM |
| Kubernetes | External Secrets Operator pulling from cloud KMS, **or** Sealed Secrets, **or** Secrets Store CSI Driver | Cloud-side KMS; never raw `kind: Secret` in git |

aegrail itself doesn't know or care which is which. The container
gets env vars; `Agent.from_env()` reads them. The split exists
**purely for the operator's posture** — audit access to the secret,
rotate the key without redeploying, prove encryption at rest under
your customer-managed key.

## Guides

- [AWS App Runner](aws-app-runner.md) — image-based, fully managed.
  **Verified end-to-end 2026-05-15** with KMS-backed Secrets Manager.
- [Google Cloud Run](google-cloud-run.md) — image-based, fully managed.
  Uses Secret Manager with optional CMEK.
- [Azure Container Apps](azure-container-apps.md) — image-based,
  fully managed. Uses Container Apps secrets with Key Vault sync.
- [AWS Fargate (ECS)](aws-fargate.md) — task-based on ECS. Adds
  sidecar capability vs. App Runner, at the cost of more wiring.
- [Kubernetes](kubernetes.md) — ConfigMap for non-secret env, External
  Secrets Operator (or equivalent) for KMS-backed secrets.
  **Verified end-to-end 2026-05-15** on kind for the env-var path.

## What the production checklist always includes

Every guide ends with the same checklist. The bullets that are
identical across platforms:

- [ ] Secrets in a managed KMS-backed store, NOT plain env vars.
- [ ] `AEGRAIL_AGENT_IDENTITY` is stable and audited (you'll join
      on this in incident review).
- [ ] Budgets reflect production appetite — `BudgetExceeded` is hard.
- [ ] `AEGRAIL_EGRESS_ALLOWLIST` is the explicit set of provider hosts.
- [ ] `agent.session(user_id=...)` carries the real caller identity,
      not a fixed string.
- [ ] Audit logs stream to long-term storage (S3 / GCS / Blob /
      object store) beyond the platform's default retention.
- [ ] If `tools=` is in use, every Tool has explicit `when=`
      predicates for state-changing actions.
- [ ] LLM provider key rotation is documented and tested.
