# AWS App Runner — production-ready aegrail deployment

This guide is the **production** path for running an aegrail-protected
agent on AWS App Runner. It uses **KMS-backed Secrets Manager** for
the LLM provider key and **plain `RuntimeEnvironmentVariables`** for
operator-controlled aegrail policy.

The pattern was **verified end-to-end on 2026-05-15** against
aegrail 0.2.6 in `us-east-1`. The deployed service responded with
*"KMS-backed aegrail works."* and the audit chain (3 events, all
linked) flowed into CloudWatch.

Sample at [`examples/deployments/app-runner/`](../../examples/deployments/app-runner/).

## At a glance

```
                            +----------------------+
                            |       App Runner     |
   ECR ──── pull ──────────►|  aegrail-sample      |◄──── HTTPS users
   (image)                  |                      |
                            |  env: AEGRAIL_*      |
                            |  env-from-secret:    |
                            |    OPENROUTER_API_KEY|
                            +----------------------+
                                   │     │
                ┌──────────────────┘     └──────────────┐
                ▼                                       ▼
    Secrets Manager:                          Instance role:
    aegrail/sample-prod/                      kms:Decrypt (on the CMK)
    openrouter-key                            secretsmanager:GetSecretValue
       │                                      (on the secret)
       └──► encrypted by CMK ◄───── alias/aegrail-sample-prod
                                                       ▲
                                                       │ (rotate on schedule)
```

## Resources you'll create

| Resource | Purpose | Cost when idle |
|---|---|---|
| KMS Customer Managed Key (CMK) | Encrypts the secret. CMK = audit + rotation. | $1/month |
| KMS alias | Stable handle so you can rotate the underlying key without redeploy | $0 |
| Secrets Manager secret | Holds the LLM provider key, encrypted by the CMK | $0.40/month + per-call |
| ECR repo | Image registry | ~$0.10/GB-month |
| IAM `AccessRole` | App Runner assumes this to pull from ECR | $0 |
| IAM `InstanceRole` | The running container assumes this to fetch the secret | $0 |
| App Runner service | The running agent | $1/month base + per-vCPU-second when active |

Steady-state minimum is roughly **$2.50/month** with no traffic.

## End-to-end deploy

`examples/deployments/app-runner/deploy.sh` does all of this with one
command. It supports two modes via the `SECRETS_MODE` env var:

- `SECRETS_MODE=env` (default for dev) — `OPENROUTER_API_KEY` is
  passed as a plain `RuntimeEnvironmentVariables` entry. Fast for
  iteration; **never for production**.
- `SECRETS_MODE=kms` (production) — creates a CMK, stores the key in
  Secrets Manager encrypted by the CMK, wires an instance role with
  least-privilege access, and references the secret via
  `RuntimeEnvironmentSecrets`.

```bash
cd examples/deployments/app-runner
export OPENROUTER_API_KEY=sk-or-...
SECRETS_MODE=kms ./deploy.sh
# (when done)
SECRETS_MODE=kms ./teardown.sh
```

## What the production deploy actually does

The commands below are what `deploy.sh` runs under `SECRETS_MODE=kms`.
Walking through them by hand once helps explain what's underneath.

### 1. Customer-managed KMS key

```bash
aws kms create-key \
  --description "aegrail prod CMK" \
  --tags TagKey=Project,TagValue=aegrail
KEY_ARN=$(aws kms describe-key --key-id "${KEY_ID}" --query 'KeyMetadata.Arn' --output text)
aws kms create-alias --alias-name alias/aegrail-prod --target-key-id "${KEY_ID}"
```

A CMK gives you:
- Per-key audit trail in CloudTrail (auditors will ask).
- Independent rotation schedule.
- Optional key policy preventing AWS-side access.

### 2. Store the secret

```bash
aws secretsmanager create-secret \
  --name "aegrail/prod/openrouter-key" \
  --kms-key-id "${KEY_ARN}" \
  --secret-string "${OPENROUTER_API_KEY}"
```

Naming convention: `aegrail/<env>/<purpose>`. Lets a single IAM policy
grant access to all secrets for one environment via a prefix wildcard.

### 3. Roles — split access and instance

App Runner uses two roles with different scope. Don't merge them.

**Access role** — for `build.apprunner.amazonaws.com`, used at deploy
time to pull the image from ECR:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "build.apprunner.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}
```

Attach AWS-managed policy `AWSAppRunnerServicePolicyForECRAccess`.

**Instance role** — for `tasks.apprunner.amazonaws.com`, assumed by
the *running* container:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "tasks.apprunner.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}
```

Inline policy — least-privilege, scoped to the specific secret and CMK:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:us-east-1:ACCT:secret:aegrail/prod/openrouter-key-*"
    },
    {
      "Effect": "Allow",
      "Action": "kms:Decrypt",
      "Resource": "arn:aws:kms:us-east-1:ACCT:key/KEY-ID"
    }
  ]
}
```

The `-*` suffix on the secret ARN matches the random suffix Secrets
Manager appends. Without it, IAM denies the call.

### 4. Create the service with the right env split

```json
{
  "ImageRepository": {
    "ImageIdentifier": "ACCT.dkr.ecr.us-east-1.amazonaws.com/aegrail:v0.2.6",
    "ImageConfiguration": {
      "Port": "8080",
      "RuntimeEnvironmentVariables": {
        "AEGRAIL_AGENT_IDENTITY": "support-bot/v1",
        "AEGRAIL_BUDGET_USD": "5.00",
        "AEGRAIL_BUDGET_TOKENS": "100000",
        "AEGRAIL_BUDGET_WALL_SECONDS": "120",
        "AEGRAIL_EGRESS_ALLOWLIST": "api.openai.com,*.anthropic.com",
        "AEGRAIL_AUDIT_STDOUT": "1",
        "OPENROUTER_MODEL": "openai/gpt-4o-mini"
      },
      "RuntimeEnvironmentSecrets": {
        "OPENROUTER_API_KEY": "arn:aws:secretsmanager:us-east-1:ACCT:secret:aegrail/prod/openrouter-key-XXXXXX"
      }
    },
    "ImageRepositoryType": "ECR"
  },
  "AuthenticationConfiguration": {
    "AccessRoleArn": "arn:aws:iam::ACCT:role/AppRunnerECRAccessRole-aegrail-prod"
  }
}
```

```json
{
  "Cpu": "0.25 vCPU",
  "Memory": "0.5 GB",
  "InstanceRoleArn": "arn:aws:iam::ACCT:role/AppRunnerInstanceRole-aegrail-prod"
}
```

`InstanceRoleArn` is on the instance config, *not* in
`AuthenticationConfiguration`. The latter is for image-pull only.

## Verification — observed behavior

```bash
$ curl -sX POST https://bznhzfkrpn.us-east-1.awsapprunner.com/chat \
    -H 'Content-Type: application/json' \
    -d '{"message":"Reply with exactly: KMS-backed aegrail works."}' | jq
{
  "reply": "KMS-backed aegrail works.",
  "model": "openai/gpt-4o-mini",
  "tokens_in": 18,
  "tokens_out": 8,
  "cost_usd": 7.5e-06,
  "aegrail_state": {
    "tokens_used": 26,
    "usd_used": 7e-06,
    "tool_calls": 0,
    "recursion_depth": 0,
    "wall_elapsed": 0.468
  }
}
```

CloudWatch stream `/aws/apprunner/aegrail-sample-prod/.../application`
contains three JSON-line aegrail audit events (`session_start`,
`llm_call`, `session_end`) with `prev_hash` / `event_hash` linking
them. Confirmed via `aegrail.audit.verify_chain` over the exported
lines: `(True, -1)`.

## Production checklist (App Runner-specific)

- [ ] Use a customer-managed KMS key (CMK), not the AWS-managed
      `aws/secretsmanager` key. CMK = key policy + CloudTrail data
      events you control.
- [ ] Enable **automatic rotation** on the Secrets Manager secret
      (https://docs.aws.amazon.com/secretsmanager/latest/userguide/rotating-secrets.html).
      The instance role doesn't need to change; App Runner re-fetches
      on the next container start.
- [ ] Restrict the secret's resource policy to the specific instance
      role ARN. Defence-in-depth on top of the inline policy.
- [ ] Set `AutoDeploymentsEnabled: false` and roll forward via
      `aws apprunner update-service` from CI — keeps prod immune to
      surprise image-tag pushes.
- [ ] Add an App Runner **observability configuration** (X-Ray
      tracing) and an **observability** dashboard. The aegrail audit
      stream covers *what the agent did*; X-Ray covers *how the
      container performed*.
- [ ] Stream the CloudWatch log group to S3 (CloudWatch Logs
      → Kinesis → S3) for audit retention. Default CW Logs retention
      is short.
- [ ] Add a CloudWatch **metric filter** on
      `event="budget_exceeded"` and alarm on it — the budget
      kill-switch is supposed to fire occasionally; an unexpected
      spike means something else.

## Teardown

```bash
SECRETS_MODE=kms ./teardown.sh
```

Deletes service → ECR repo → both IAM roles → secret (force, no
recovery window) → KMS alias → schedules the CMK for deletion in 7
days (KMS minimum).

## Notes

- **KMS pending-deletion window** is 7-30 days; 7 is the minimum AWS
  allows. The key incurs no charge during this window. To cancel
  deletion, `aws kms cancel-key-deletion --key-id ...`.
- **Secrets Manager pricing**: $0.40/secret/month + $0.05 per 10k
  API calls. App Runner refreshes secrets on container start, not
  per request, so call volume is bounded by deploy/scale events.
- **App Runner doesn't support sidecars.** If you need a sidecar
  egress proxy, target [Fargate](aws-fargate.md) or
  [Kubernetes](kubernetes.md) instead. aegrail's v0.3 engine is the
  long-term answer here; until then, the in-process interceptors
  (`AEGRAIL_INTERCEPT=1`) are your defense-in-depth on App Runner.
