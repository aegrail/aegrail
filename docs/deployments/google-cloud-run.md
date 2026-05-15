# Google Cloud Run — production-ready aegrail deployment

Cloud Run is GCP's fully-managed container runtime — direct analogue
to App Runner. This guide is the **production** path: KMS-backed
Secret Manager for the LLM provider key, plain env vars for aegrail
policy.

> **Test status:** Not yet end-to-end validated by the project. The
> Cloud Run + Secret Manager pattern is canonical Google guidance;
> the aegrail-specific surface (`Agent.from_env()`) is identical to
> the App Runner path which **is** validated. Run the verification
> script in your own project to confirm before going to prod.

## At a glance

```
                          +-----------------------+
                          |       Cloud Run       |
   Artifact Registry ────►|  aegrail-sample       |◄──── HTTPS users
   (image)                |                       |
                          |  env: AEGRAIL_*       |
                          |  env-from-secret:     |
                          |    OPENROUTER_API_KEY |
                          +-----------------------+
                                  │       │
              ┌───────────────────┘       └────────────┐
              ▼                                        ▼
   Secret Manager:                            Runtime SA:
   projects/PROJ/secrets/                     secretmanager.secretAccessor
   openrouter-key                             cloudkms.cryptoKeyDecrypter
       │                                      (scoped to this secret)
       └──► encrypted by                              ▲
            CMEK (Cloud KMS)                          │ rotate on schedule
```

## Resources you'll create

| Resource | Cost when idle |
|---|---|
| Artifact Registry repo | $0.10/GB-month |
| Cloud KMS keyring + key (CMEK) | $0.06/month per key version |
| Secret Manager secret | $0.06/active version/month + $0.03 per 10k accesses |
| Cloud Run service | $0 — true zero-scale, pay only per request |
| Runtime service account | $0 |

Cloud Run's zero-cost-when-idle is the headline win vs. App Runner.

## End-to-end deploy

```bash
# Prereqs: gcloud authenticated, docker available, project quota for Cloud Run
export GCP_PROJECT=your-project
export GCP_REGION=us-central1
export OPENROUTER_API_KEY=sk-or-...

# 1. Image registry
gcloud artifacts repositories create aegrail-sample \
  --repository-format=docker --location="$GCP_REGION"
gcloud auth configure-docker "$GCP_REGION-docker.pkg.dev"

# 2. Build + push (use the existing sample at examples/deployments/app-runner)
cd examples/deployments/app-runner
IMAGE="$GCP_REGION-docker.pkg.dev/$GCP_PROJECT/aegrail-sample/agent:v0.2.6"
docker build --platform linux/amd64 -t "$IMAGE" .
docker push "$IMAGE"

# 3. KMS — keyring, key, with rotation
gcloud kms keyrings create aegrail --location="$GCP_REGION"
gcloud kms keys create openrouter-key \
  --keyring=aegrail --location="$GCP_REGION" \
  --purpose=encryption \
  --rotation-period=90d --next-rotation-time=$(date -v +90d -u +%Y-%m-%dT%H:%M:%SZ)

# 4. Secret Manager — secret encrypted by the CMEK
gcloud secrets create openrouter-key \
  --replication-policy=user-managed --locations="$GCP_REGION" \
  --kms-key-name="projects/$GCP_PROJECT/locations/$GCP_REGION/keyRings/aegrail/cryptoKeys/openrouter-key"
printf "%s" "$OPENROUTER_API_KEY" | gcloud secrets versions add openrouter-key --data-file=-

# 5. Runtime service account with least-privilege secret + KMS access
gcloud iam service-accounts create aegrail-sample-sa
SA="aegrail-sample-sa@$GCP_PROJECT.iam.gserviceaccount.com"
gcloud secrets add-iam-policy-binding openrouter-key \
  --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor
gcloud kms keys add-iam-policy-binding openrouter-key \
  --keyring=aegrail --location="$GCP_REGION" \
  --member="serviceAccount:$SA" --role=roles/cloudkms.cryptoKeyDecrypter

# 6. Deploy
gcloud run deploy aegrail-sample \
  --image="$IMAGE" --region="$GCP_REGION" --platform=managed \
  --service-account="$SA" \
  --no-allow-unauthenticated \
  --port=8080 --memory=512Mi --cpu=1 \
  --set-env-vars="^@^AEGRAIL_AGENT_IDENTITY=cloud-run-sample/v1@AEGRAIL_BUDGET_USD=5.00@AEGRAIL_BUDGET_TOKENS=100000@AEGRAIL_BUDGET_WALL_SECONDS=120@AEGRAIL_BUDGET_MAX_TOOL_CALLS=10@AEGRAIL_EGRESS_ALLOWLIST=openrouter.ai@AEGRAIL_AUDIT_STDOUT=1@OPENROUTER_MODEL=openai/gpt-4o-mini" \
  --set-secrets="OPENROUTER_API_KEY=openrouter-key:latest"
```

The custom `^@^` delimiter on `--set-env-vars` lets values contain
commas (the egress allowlist).

## What the env split looks like

Identical to App Runner — only the platform mechanics differ:

| aegrail env | Cloud Run flag |
|---|---|
| `AEGRAIL_*` non-secret | `--set-env-vars` |
| `OPENROUTER_API_KEY` | `--set-secrets=OPENROUTER_API_KEY=openrouter-key:latest` |

Cloud Run resolves the secret reference per container start and
injects it as a normal env var into the process — `Agent.from_env()`
sees it exactly the same way it sees any other env var.

## Verification

```bash
URL=$(gcloud run services describe aegrail-sample --region="$GCP_REGION" --format='value(status.url)')
ID_TOKEN=$(gcloud auth print-identity-token)
curl -sX POST "$URL/chat" \
  -H "Authorization: Bearer $ID_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message":"Reply with exactly: CMEK-backed aegrail on Cloud Run works."}'
```

## Audit chain destination

`AEGRAIL_AUDIT_STDOUT=1` sends every aegrail audit event to stdout,
which Cloud Run forwards to **Cloud Logging** under the resource
`Cloud Run Revision` automatically. The log entries are structured —
each JSON line lands as a `jsonPayload`. Query example:

```
resource.type = "cloud_run_revision"
resource.labels.service_name = "aegrail-sample"
jsonPayload.event = "llm_call"
```

For long-term retention, sink the Cloud Logging stream to BigQuery or
GCS via a **Log Router sink**.

## Production checklist (Cloud Run-specific)

- [ ] Use CMEK (customer-managed encryption key) on the secret
      version. Google-managed encryption is the default; auditors
      will want CMEK for SOC 2 / ISO 27001.
- [ ] Enable **Secret Manager rotation** by setting a
      `nextRotationTime` and writing a small Cloud Function to call
      `secrets.versions.add` on schedule. Pin Cloud Run's secret
      reference to `:latest` so it picks up rotated versions on the
      next cold start.
- [ ] `--no-allow-unauthenticated` — require IAM-authenticated
      callers. Put a load balancer + Identity-Aware Proxy in front
      for user-facing routes.
- [ ] Set `--min-instances` based on the cost/latency tradeoff. `0`
      = cheapest, ~2s cold start. `1` = always-warm.
- [ ] Use **VPC connectors** if outbound egress should traverse
      private networking (for the v0.3 engine sidecar story later).
- [ ] Enable **binary authorization** with attestations from your
      CI so only signed images deploy.
- [ ] Configure a Log Router sink to GCS for audit retention beyond
      Cloud Logging's default 30 days.

## Teardown

```bash
gcloud run services delete aegrail-sample --region="$GCP_REGION" --quiet
gcloud secrets delete openrouter-key --quiet
gcloud kms keys versions destroy 1 --key=openrouter-key --keyring=aegrail --location="$GCP_REGION"
gcloud iam service-accounts delete "$SA" --quiet
gcloud artifacts repositories delete aegrail-sample --location="$GCP_REGION" --quiet
```

KMS key versions destroy with a default 24-hour scheduled-deletion
window (changeable via `--destroy-scheduled-duration`).
