# Azure Container Apps — production-ready aegrail deployment

Azure Container Apps (ACA) is Azure's fully-managed container runtime.
This guide is the **production** path: Container Apps secrets synced
from **Key Vault with HSM-backed customer-managed keys** for the LLM
provider key; plain env vars for aegrail policy.

> **Test status:** Not yet end-to-end validated by the project. The
> ACA + Key Vault sync pattern is canonical Microsoft guidance; the
> aegrail-specific surface is identical to the validated App Runner
> path. Run the verification before going to prod.

## At a glance

```
                          +-----------------------+
                          |    Container Apps     |
   ACR ─────────────────► |  aegrail-sample       |◄──── HTTPS users
   (image)                |                       |
                          |  env: AEGRAIL_*       |
                          |  env-from-secret:     |
                          |    OPENROUTER_API_KEY |
                          +-----------------------+
                                       │
                          ┌────────────┴────────────┐
                          ▼                         ▼
                  Container Apps secret      User-assigned managed
                  "openrouter-key"           identity with:
                          │                  - get on the Key Vault
                          ▲                    secret
                          │ synced from
                  Azure Key Vault Premium
                  secret openrouter-key
                          │
                          ▼
                  Customer-managed key (HSM)
                  in the same Key Vault
```

## Resources you'll create

| Resource | Cost when idle |
|---|---|
| Azure Container Registry (Basic SKU) | ~$5/month |
| Container Apps Environment | $0 environment, per-vCPU-second when active |
| Container App | $0 when scaled to zero |
| Key Vault (Premium for HSM) | ~$1/month + $0.03 per 10k operations |
| User-assigned managed identity | $0 |

ACA scales to zero like Cloud Run.

## End-to-end deploy

```bash
# Prereqs: az CLI authenticated, docker available
export AZ_RESOURCE_GROUP=aegrail-sample-rg
export AZ_LOCATION=eastus
export AZ_REGISTRY=aegrailsample$RANDOM
export AZ_KV=aegrail-kv-$RANDOM
export AZ_APP=aegrail-sample
export OPENROUTER_API_KEY=sk-or-...

# 1. Resource group + ACR + Key Vault (Premium)
az group create -n "$AZ_RESOURCE_GROUP" -l "$AZ_LOCATION"
az acr create -n "$AZ_REGISTRY" -g "$AZ_RESOURCE_GROUP" --sku Basic
az keyvault create -n "$AZ_KV" -g "$AZ_RESOURCE_GROUP" --sku premium \
  --enable-rbac-authorization true

# 2. Build + push
cd examples/deployments/app-runner
az acr login -n "$AZ_REGISTRY"
IMAGE="$AZ_REGISTRY.azurecr.io/aegrail-sample:v0.2.6"
docker build --platform linux/amd64 -t "$IMAGE" .
docker push "$IMAGE"

# 3. Store the OpenRouter key in Key Vault (encrypted by HSM-backed CMK)
MY_PRINCIPAL=$(az ad signed-in-user show --query id -o tsv)
az role assignment create --assignee "$MY_PRINCIPAL" \
  --role "Key Vault Secrets Officer" \
  --scope $(az keyvault show -n "$AZ_KV" --query id -o tsv)
sleep 10
az keyvault secret set --vault-name "$AZ_KV" --name openrouter-key \
  --value "$OPENROUTER_API_KEY"

# 4. User-assigned managed identity with least-privilege Key Vault read
az identity create -g "$AZ_RESOURCE_GROUP" -n "$AZ_APP-mi"
MI_ID=$(az identity show -g "$AZ_RESOURCE_GROUP" -n "$AZ_APP-mi" --query id -o tsv)
MI_PRINCIPAL=$(az identity show -g "$AZ_RESOURCE_GROUP" -n "$AZ_APP-mi" --query principalId -o tsv)
SECRET_ID=$(az keyvault secret show --vault-name "$AZ_KV" --name openrouter-key --query id -o tsv)
az role assignment create --assignee-object-id "$MI_PRINCIPAL" \
  --assignee-principal-type ServicePrincipal \
  --role "Key Vault Secrets User" \
  --scope $(az keyvault show -n "$AZ_KV" --query id -o tsv)

# 5. Container Apps environment + app
az containerapp env create -n "$AZ_APP-env" -g "$AZ_RESOURCE_GROUP" -l "$AZ_LOCATION"

az containerapp create -n "$AZ_APP" -g "$AZ_RESOURCE_GROUP" \
  --environment "$AZ_APP-env" \
  --image "$IMAGE" --target-port 8080 --ingress external \
  --user-assigned "$MI_ID" \
  --registry-server "$AZ_REGISTRY.azurecr.io" --registry-identity "$MI_ID" \
  --secrets "openrouter-key=keyvaultref:${SECRET_ID},identityref:${MI_ID}" \
  --env-vars \
    "AEGRAIL_AGENT_IDENTITY=aca-sample/v1" \
    "AEGRAIL_BUDGET_USD=5.00" \
    "AEGRAIL_BUDGET_TOKENS=100000" \
    "AEGRAIL_BUDGET_WALL_SECONDS=120" \
    "AEGRAIL_BUDGET_MAX_TOOL_CALLS=10" \
    "AEGRAIL_EGRESS_ALLOWLIST=openrouter.ai" \
    "AEGRAIL_AUDIT_STDOUT=1" \
    "OPENROUTER_MODEL=openai/gpt-4o-mini" \
    "OPENROUTER_API_KEY=secretref:openrouter-key"
```

The `keyvaultref:` syntax is the load-bearing bit — ACA refreshes
the secret value from Key Vault on every revision rollout, no app
restart needed when the underlying Key Vault secret rotates.

## What the env split looks like

| aegrail env | ACA mechanism |
|---|---|
| `AEGRAIL_*` non-secret | `--env-vars KEY=value` |
| `OPENROUTER_API_KEY` | `--secrets openrouter-key=keyvaultref:...` then `OPENROUTER_API_KEY=secretref:openrouter-key` |

## Verification

```bash
URL=$(az containerapp show -n "$AZ_APP" -g "$AZ_RESOURCE_GROUP" --query 'properties.configuration.ingress.fqdn' -o tsv)
curl -sX POST "https://$URL/chat" \
  -H 'Content-Type: application/json' \
  -d '{"message":"Reply with exactly: ACA + Key Vault aegrail works."}'
```

## Audit chain destination

`AEGRAIL_AUDIT_STDOUT=1` streams events into the **Container Apps
Logs** stream (Azure Monitor Log Analytics workspace). Each aegrail
event lands as a `Log_s` column with the JSON payload.

For long-term retention, configure a **diagnostic setting** on the
Container App that sinks logs to an Event Hub or Storage Account.

## Production checklist (ACA-specific)

- [ ] Use **Key Vault Premium** with an HSM-backed customer-managed
      key for the secret. Standard SKU uses software-protected keys
      only.
- [ ] Enable **Key Vault purge protection** (`--enable-purge-protection`).
      Compliance auditors will check.
- [ ] Use a **user-assigned managed identity**, not the system-assigned
      one. User-assigned identities outlive the app and let you
      rotate the app or roll a revision without re-granting Key Vault
      access.
- [ ] Use ACA **revisions** for blue-green: deploy a new revision,
      let it pass health, shift traffic with
      `az containerapp revision set-mode multiple`.
- [ ] Set ACA **scale rules** based on HTTP concurrency or external
      events, not just CPU — agent workloads are mostly waiting on
      LLM provider responses.
- [ ] Enable **private networking** for the Container Apps
      environment if your aegrail egress allowlist should be
      enforced by your VNet's NSG / Firewall instead of just
      in-process.
- [ ] Stream logs to a Log Analytics workspace with retention set to
      your audit retention policy (defaults too short for SOC 2).

## Teardown

```bash
az group delete -n "$AZ_RESOURCE_GROUP" --yes --no-wait
```

Resource group delete cascades into every nested resource — the
fastest cleanup of any cloud. Key Vault soft-delete adds a 90-day
recovery window; pass `--purge-protection false` if you need an
immediate full purge for an ephemeral test (not recommended for prod).
