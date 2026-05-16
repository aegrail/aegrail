# Kubernetes — production-ready aegrail deployment

This is the **production** path for running an aegrail-protected
agent on Kubernetes. There are now two patterns, depending on how
much developer cooperation you have:

- **Zero-developer-effort (recommended, v0.4.x engine):** install
  the `aegrail-engine` Helm chart with the **mutating admission
  webhook** and **MITM** enabled. Label a namespace
  `aegrail.io/inject=enabled`. Every pod in that namespace gets the
  engine sidecar auto-injected, `HTTP_PROXY`/`HTTPS_PROXY` set on
  every container, the MITM CA mounted, and HTTPS trust env vars
  configured. The agent author writes zero aegrail code; HTTPS
  traffic to public LLM providers gets token-accounted at the
  network layer.

- **Library-in-code pattern (still supported):** developers
  `pip install aegrail`, call `Agent.from_env()`, and the SDK
  enforces in-process. ConfigMap supplies the policy, External
  Secrets Operator supplies the LLM provider key. This was the
  original pattern; it remains correct.

**Verified end-to-end 2026-05-16** on kind:
- Webhook auto-injection path (9 scenarios) —
  [`aegrail-engine/tests/kind/run-webhook.sh`](https://github.com/arpitcoder/aegrail-engine/blob/main/tests/kind/run-webhook.sh)
- Engine proxy + Ollama token parsing (13 scenarios) —
  [`aegrail-engine/tests/kind/run.sh`](https://github.com/arpitcoder/aegrail-engine/blob/main/tests/kind/run.sh)
- MITM TLS termination + token enforcement end-to-end (Go test) —
  [`internal/proxy/mitm_test.go`](https://github.com/arpitcoder/aegrail-engine/blob/main/internal/proxy/mitm_test.go)

The library-in-code path is also kind-validated against Ollama —
sample under [`tests/integration/kind/`](../../tests/integration/kind/)
in the aegrail repo.

## Pattern A: zero-developer-effort (engine + webhook + MITM)

For platform teams: enable per-namespace, the dev team writes
nothing.

### 1. Install the engine chart with webhook + MITM enabled

```bash
helm repo add aegrail https://arpitcoder.github.io/aegrail-engine
helm repo update

# Pre-create the MITM CA Secret in the engine's namespace.
# Easiest: let the engine generate it on first install with
# webhook off, capture the CA from the pod logs, then turn on
# webhook+MITM in a second helm upgrade.
helm install aegrail-engine aegrail/aegrail-engine \
  --namespace aegrail-system --create-namespace \
  --set 'policy.allowlist[0]=api.openai.com' \
  --set 'policy.allowlist[1]=*.anthropic.com' \
  --set 'webhook.enabled=true' \
  --set 'mitm.hosts=api.openai.com,api.anthropic.com' \
  --set 'mitm.caSecretName=aegrail-mitm-ca' \
  --set 'limits.maxTokens=1000000'
```

### 2. Copy the CA Secret into each target namespace

K8s does not allow Pod Secret mounts to reference cross-namespace
Secrets, so the CA must exist in each namespace where injection is
labeled.

```bash
for ns in agents-prod agents-staging; do
  kubectl get secret aegrail-mitm-ca -n aegrail-system -o yaml \
    | sed "s/namespace: aegrail-system/namespace: $ns/" \
    | kubectl apply -f -
done
```

A first-class controller that replicates this Secret automatically
across labeled namespaces is engine roadmap v0.5.0.

### 3. Label the namespaces you want covered

```bash
kubectl label namespace agents-prod aegrail.io/inject=enabled
kubectl label namespace agents-staging aegrail.io/inject=enabled
```

### 4. Apply an agent pod — the webhook does the rest

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: my-agent
  namespace: agents-prod
  labels:
    aegrail.io/identity: support-bot-v1
spec:
  containers:
    - name: app
      image: your-registry/agent:1.0
```

The webhook injects:
- The `aegrail-engine` sidecar container with the configured
  allowlist, policy, audit destination, MITM CA, and identity
  binding from the pod label.
- `HTTP_PROXY=http://localhost:8080`,
  `HTTPS_PROXY=http://localhost:8080`,
  `NO_PROXY=localhost,127.0.0.1,.svc,.cluster.local` on `app`.
- `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`
  pointing at `/etc/aegrail/mitm-ca/ca.crt` so HTTPS calls to
  api.openai.com / api.anthropic.com succeed through the engine's
  MITM.
- Volume mounts wiring it all together.

Your `app` container runs unchanged. Every outbound LLM call is
captured, allowlist-checked, rate-limited, budget-accounted,
audited.

### What gets enforced at the engine layer

| Concern | Engine enforces? |
|---|---|
| Egress allowlist (any HTTP/HTTPS, any language) | Yes |
| Request count / rate limit | Yes (`limits.maxRequests`, `limits.rate`) |
| Token budget | Yes for MITM'd hosts AND for plain-HTTP forwards (Ollama, in-cluster gateways) |
| Audit chain (SHA-256-linked) | Yes |
| Identity binding from pod label | Yes (downward API) |
| Tool ACL | No — that's the in-process SDK's job (Pattern B) |
| Approval gates | No — engine roadmap v0.5+ |
| Per-user dual-principal authz | No — engine has no concept of the invoking user |

For pattern B (library-in-code), the SDK adds tool ACL +
dual-principal authz + agent-side budget enforcement before the
LLM call. The two patterns combine: install the chart AND use the
SDK, and you get both layers.

## Pattern B: library-in-code (developer cooperation)

## At a glance

```
                    +---------------------------------+
                    |  Kubernetes pod                 |
   Registry ──────► |  ┌──────────────────────────┐   |
   (image)          |  │  agent container         │   |
                    |  │  envFrom: ConfigMap      │   |
                    |  │    AEGRAIL_AGENT_IDENTITY│   |
                    |  │    AEGRAIL_BUDGET_*      │   |
                    |  │    AEGRAIL_EGRESS_*      │   |
                    |  │    AEGRAIL_AUDIT_FILE    │   |
                    |  │    AEGRAIL_INTERCEPT=1   │   |
                    |  │  envFrom: Secret         │   |
                    |  │    OPENROUTER_API_KEY    │◄──┼── reconciled by ESO
                    |  │                          │   |
                    |  │  audit → /var/log/...    │   |
                    |  └──────────────────────────┘   |
                    |  ┌──────────────────────────┐   |
                    |  │  log-shipper sidecar     │   |
                    |  │  (Fluent Bit / Vector)   │   |
                    |  └──────────────────────────┘   |
                    +---------------------------------+
                                  │
                                  │ (ESO controller)
                                  ▼
                       AWS Secrets Manager
                       (or GCP Secret Manager
                       or Azure Key Vault)
                                  │
                          encrypted by KMS / CMEK / Key Vault
```

## Resources

| Resource | Purpose |
|---|---|
| `ConfigMap aegrail-policy` | Non-secret operator config — identity, budget, allowlist, audit destination |
| `Secret openrouter-key` | Synced from cloud secret store by ESO; mounted as env var |
| `ExternalSecret openrouter-key` | The CRD that tells ESO to keep the K8s Secret in sync with the cloud secret |
| `ClusterSecretStore` (or `SecretStore`) | ESO's connection to the cloud KMS-backed store, using IRSA / Workload Identity / Managed Identity |
| `ServiceAccount aegrail-agent` | Pod-bound identity; IRSA-annotated (EKS) or workload-identity-bound (GKE) |
| `Deployment aegrail-agent` | The agent itself; references both ConfigMap and Secret via `envFrom` |
| `NetworkPolicy aegrail-egress` | Cluster-side egress enforcement (defence-in-depth) |

## End-to-end deploy — EKS (AWS)

```bash
# Prereqs: kubectl, helm, eksctl (or your EKS cluster already up)
export CLUSTER=aegrail-prod
export AWS_REGION=us-east-1

# 1. Install External Secrets Operator (one-time per cluster)
helm repo add external-secrets https://charts.external-secrets.io
helm install external-secrets external-secrets/external-secrets \
  --namespace external-secrets --create-namespace --wait

# 2. KMS + Secrets Manager (same recipe as the App Runner / Fargate guides)
KEY_ID=$(aws kms create-key --description "aegrail K8s CMK" --query 'KeyMetadata.KeyId' --output text)
KEY_ARN=$(aws kms describe-key --key-id "$KEY_ID" --query 'KeyMetadata.Arn' --output text)
aws kms create-alias --alias-name alias/aegrail-k8s --target-key-id "$KEY_ID"
aws secretsmanager create-secret \
  --name aegrail/k8s/openrouter-key \
  --kms-key-id "$KEY_ARN" \
  --secret-string "$OPENROUTER_API_KEY"

# 3. IRSA — a ServiceAccount that the ESO controller can assume to read the secret
eksctl create iamserviceaccount \
  --cluster "$CLUSTER" --namespace external-secrets --name external-secrets \
  --attach-policy-arn arn:aws:iam::aws:policy/SecretsManagerReadWrite \
  --override-existing-serviceaccounts --approve
# (Tighten to least-privilege in real deploys.)

# 4. Cluster-wide store + ExternalSecret
kubectl apply -f - <<EOF
apiVersion: external-secrets.io/v1
kind: ClusterSecretStore
metadata: { name: aws-secretsmanager }
spec:
  provider:
    aws:
      service: SecretsManager
      region: $AWS_REGION
      auth:
        jwt:
          serviceAccountRef:
            name: external-secrets
            namespace: external-secrets
---
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata: { name: openrouter-key, namespace: default }
spec:
  refreshInterval: 1h
  secretStoreRef: { name: aws-secretsmanager, kind: ClusterSecretStore }
  target: { name: openrouter-key, creationPolicy: Owner }
  data:
    - secretKey: OPENROUTER_API_KEY
      remoteRef: { key: aegrail/k8s/openrouter-key }
EOF
```

After ~1 minute, `kubectl get secret openrouter-key -o yaml` will
show the Kubernetes Secret with the value, decoded from the cloud
KMS-encrypted store by the ESO controller.

## End-to-end deploy — GKE (GCP)

Replace ESO config with the **Secret Manager CSI driver** or use ESO
with a GCP `SecretStore`:

```yaml
apiVersion: external-secrets.io/v1
kind: ClusterSecretStore
metadata: { name: gcp-secretmanager }
spec:
  provider:
    gcpsm:
      projectID: your-project
      auth:
        workloadIdentity:
          clusterLocation: us-central1
          clusterName: aegrail-prod
          serviceAccountRef: { name: external-secrets, namespace: external-secrets }
```

## End-to-end deploy — AKS (Azure)

Use ESO with an Azure `SecretStore` backed by Key Vault and workload
identity, OR the **Secrets Store CSI Driver** + Azure Key Vault
provider. Both reach the same end state — a K8s `Secret` synced from
Key Vault, transparently CMK-encrypted on the Azure side.

## Deployment, Job, or StatefulSet?

The right workload kind for an agent is almost always **`Deployment`**.
StatefulSet is a common wrong default for agents — worth being
explicit about when each fits.

| Kind | Use when | aegrail engine pattern |
|---|---|---|
| **Deployment** | The agent serves requests (REST / gRPC / queue consumer). Session state lives in Redis / Postgres / a vector DB — not on the pod's local disk. This is ~95% of production agents. | Engine as sidecar in the same pod. |
| **Job / CronJob** | One-shot batch agent (a LangGraph workflow run, a scheduled report generator, an evaluation job). The pod exits after the work finishes. | Engine as sidecar; main container exit signals job completion. |
| **StatefulSet** | The pod itself carries durable state that survives restarts and the identity of pod-N matters. Legitimately rare for agents. | Same sidecar pattern as Deployment. |
| **DaemonSet** | One agent process per node (rare, niche use case for node-local observability agents). | Engine as a separate per-node DaemonSet, not as a sidecar. |

### Why StatefulSet is usually the wrong default

A `StatefulSet` gives you stable pod identity (`pod-0`, `pod-1`), a
stable PV per ordinal, and ordered rolling updates. You need those
when **the pod owns durable state** — Postgres primaries, Kafka
brokers, Cassandra rings.

Agents almost never own durable local state. Look at where state
actually lives:

| Agent state | Where it lives | Pod-stateful? |
|---|---|---|
| In-flight session (one user turn) | RAM, dies with the pod | No |
| Conversation history | External (Redis, Postgres, DynamoDB) | No |
| Vector DB | External service (Pinecone, Weaviate, pgvector) | No |
| LLM weights | Provider API (OpenAI, Anthropic, Bedrock) | No |
| Audit chain | Stream out (stdout → log aggregator → S3) | No |
| Long-running workflow checkpoint | External (LangGraph state in Postgres, Temporal, Step Functions) | No |

If you find yourself reaching for StatefulSet, ask: *what's on local
disk that I can't move to a service?* Usually nothing. Push it out,
use Deployment.

### Where StatefulSet *is* the right choice

Three legitimate cases:

1. **Self-hosted model server with local weights cached on disk**
   (vLLM, Ollama, TGI with 30 GB of model files). StatefulSet + PV
   avoids re-downloading weights on every pod restart. But here the
   StatefulSet is the *model server* — the agent talking to it
   stays a Deployment.
2. **Local vector index baked into the pod** (FAISS / hnswlib on
   local SSD). Persisting the index across restarts justifies a PV.
   Usually an anti-pattern; externalize the index.
3. **Sticky-session agents** where each user is routed to the same
   pod for an entire conversation and the session lives in RAM. Real
   but scales poorly. Push the session to Redis instead.

### One anti-pattern to avoid

Some teams reach for StatefulSet because they want "stable pod
names for log aggregation." That's the wrong reason. Log
aggregators join on pod labels, not pod ordinals. The
`aegrail.io/identity` label (consumed by the engine sidecar via
[`agentIdentityFromLabel`](https://github.com/arpitcoder/aegrail-engine)
since engine v0.1.1) is the right primitive — your audit chain
stays stable across pod restarts because the **agent identity is a
label, not a pod name**.

## The aegrail manifests (cloud-independent)

Once the secret is being reconciled in (ESO or CSI), the agent's K8s
manifests are identical across clouds:

```yaml
apiVersion: v1
kind: ConfigMap
metadata: { name: aegrail-policy }
data:
  AEGRAIL_AGENT_IDENTITY: "k8s-agent/v1"
  AEGRAIL_BUDGET_USD: "5.00"
  AEGRAIL_BUDGET_TOKENS: "100000"
  AEGRAIL_BUDGET_WALL_SECONDS: "120"
  AEGRAIL_BUDGET_MAX_TOOL_CALLS: "10"
  AEGRAIL_EGRESS_ALLOWLIST: "openrouter.ai"
  AEGRAIL_AUDIT_FILE: "/var/log/aegrail/audit.jsonl"
  AEGRAIL_INTERCEPT: "1"
  OPENROUTER_MODEL: "openai/gpt-4o-mini"
---
apiVersion: apps/v1
kind: Deployment
metadata: { name: aegrail-agent }
spec:
  replicas: 2
  selector: { matchLabels: { app: aegrail-agent } }
  template:
    metadata:
      labels: { app: aegrail-agent }
      annotations:
        # ConfigMap content hash → triggers rollout when policy changes
        checksum/policy: "{{ sha256sum (toYaml .) }}"
    spec:
      serviceAccountName: aegrail-agent
      automountServiceAccountToken: false
      containers:
        - name: agent
          image: your-registry/aegrail-agent:v0.2.6
          ports: [{ containerPort: 8080 }]
          envFrom:
            - configMapRef: { name: aegrail-policy }
            - secretRef:    { name: openrouter-key }
          volumeMounts:
            - { name: audit, mountPath: /var/log/aegrail }
          securityContext:
            runAsNonRoot: true
            runAsUser: 10001
            readOnlyRootFilesystem: true
            allowPrivilegeEscalation: false
            capabilities: { drop: [ALL] }
          resources:
            requests: { cpu: 50m, memory: 128Mi }
            limits:   { cpu: 500m, memory: 512Mi }
          livenessProbe:
            httpGet: { path: /healthz, port: 8080 }
        - name: fluent-bit
          image: fluent/fluent-bit:3.0
          volumeMounts:
            - { name: audit, mountPath: /var/log/aegrail, readOnly: true }
            # plus your Fluent Bit config ConfigMap
      volumes:
        - { name: audit, emptyDir: {} }
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: aegrail-egress }
spec:
  podSelector: { matchLabels: { app: aegrail-agent } }
  policyTypes: [Egress]
  egress:
    # Allow only the LLM provider hosts (resolved via cluster DNS)
    - to:
        - ipBlock: { cidr: 0.0.0.0/0 }  # tighten with a sidecar egress proxy or your cloud's egress controls
      ports:
        - { protocol: TCP, port: 443 }
    # Allow DNS
    - to:
        - namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: kube-system } }
      ports:
        - { protocol: UDP, port: 53 }
```

The `NetworkPolicy` is rough — true egress control on K8s usually
requires either a CNI that supports FQDN policies (Cilium) or an
**egress proxy** (the v0.3 aegrail engine sidecar). The aegrail
in-process interceptors with `AEGRAIL_INTERCEPT=1` are the bridge
until the engine ships.

## What the env split looks like

| aegrail env | K8s mechanism |
|---|---|
| `AEGRAIL_*` non-secret | `ConfigMap` → `envFrom: configMapRef` |
| `OPENROUTER_API_KEY` | `Secret` synced from cloud KMS store by ESO → `envFrom: secretRef` |

`Agent.from_env()` reads both with the same code.

## Audit chain collection

Two patterns; pick one:

- **File sink + sidecar (recommended for forensic retention)** —
  `AEGRAIL_AUDIT_FILE=/var/log/aegrail/audit.jsonl`, mount an
  `emptyDir` (or PV) at that path, run Fluent Bit / Vector /
  Promtail as a sidecar that tails the file and ships to your
  central store (Loki, Elasticsearch, ClickHouse, S3). The
  `prev_hash`/`event_hash` chain survives across pod restarts as
  long as the volume is persistent.
- **Stdout sink** — `AEGRAIL_AUDIT_STDOUT=1`, pod logs go to your
  cluster's existing log pipeline. Simpler; loses chain continuity
  across pod restarts (because each pod starts a fresh chain).

Use the file sink for SOC 2 / ISO 27001 evidence; stdout for dev or
when you have an ELT pipeline that re-stitches by `session_id`.

## Verification

```bash
kubectl apply -f manifests.yaml
kubectl rollout status deploy/aegrail-agent

POD=$(kubectl get pod -l app=aegrail-agent -o jsonpath='{.items[0].metadata.name}')
kubectl port-forward "pod/$POD" 8080:8080 &
curl -sX POST http://127.0.0.1:8080/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"Reply with exactly: K8s + ESO aegrail works."}'

# Verify the audit chain
kubectl exec "$POD" -c agent -- python -c \
'from aegrail.audit import AuditEvent, verify_chain; import json
with open("/var/log/aegrail/audit.jsonl") as f:
    es = [AuditEvent.model_validate(json.loads(l)) for l in f if l.strip()]
ok, bad = verify_chain(es)
print("chain", "ok" if ok else "broken@" + str(bad), "events", len(es))'
```

## Production checklist (K8s-specific)

- [ ] Secrets reconciled by ESO / CSI driver from a KMS-backed
      cloud store, **never** raw `kind: Secret` in git.
- [ ] If using SealedSecrets instead of ESO: the seal key is
      backed up and rotation is documented.
- [ ] `securityContext`: non-root, read-only rootfs, dropped
      capabilities. (Shown above.)
- [ ] `automountServiceAccountToken: false` unless the agent
      makes Kubernetes API calls.
- [ ] **PodSecurity standard "restricted"** on the namespace.
- [ ] CNI supports either FQDN NetworkPolicy (Cilium) OR you run
      an egress proxy sidecar — the in-process interceptors are
      defence-in-depth, not the only layer.
- [ ] ConfigMap mounted via `envFrom`, with a `checksum/policy`
      annotation on the Deployment so policy changes roll the pods.
- [ ] Audit volume is a PV (not `emptyDir`) if you need chain
      continuity across pod restarts.
- [ ] HorizontalPodAutoscaler on the deployment with sensible
      bounds; agent pods waiting on LLM responses are mostly idle
      so HPA on CPU is misleading — prefer request-rate or
      external metrics.

## Teardown

```bash
kubectl delete -f manifests.yaml
kubectl delete externalsecret openrouter-key
helm uninstall external-secrets -n external-secrets

# Then the cloud-side KMS / Secrets Manager teardown as documented
# in the AWS / GCP / Azure guides.
```

## Where this leaves you

Two enforcement layers active:

1. **In-process interceptors** (`AEGRAIL_INTERCEPT=1`) — catches
   Python-stack HTTP, audits everything.
2. **NetworkPolicy / Cilium FQDN** — catches anything that escapes
   the language layer (subprocess, ctypes, non-Python sidecar).

The third layer — the dedicated aegrail-engine sidecar — is
roadmap v0.3, shipped via the
[`arpitcoder/aegrail-engine`](https://github.com/arpitcoder/aegrail-engine)
repo's Helm chart. Until then, the combination above is the
production-grade story.
