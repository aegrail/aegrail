# AWS Fargate (ECS) — production-ready aegrail deployment

Fargate runs containers on ECS without you managing EC2. Compared to
App Runner: more wiring, but you get **sidecars** — which is the
seam for the forthcoming aegrail engine egress-proxy enforcer (v0.3),
and lets you run the Datadog / Fluent Bit / OpenTelemetry collector
alongside the agent.

This guide is the **production** path: KMS-backed Secrets Manager,
plain env vars for aegrail policy, task role with least privilege.

> **Test status:** Not yet end-to-end validated by the project. The
> ECS + Secrets Manager pattern is canonical AWS guidance and the
> aegrail-specific surface is the same as the validated App Runner
> path.

## At a glance

```
                              +-----------------------+
                              |  Fargate task         |
   ECR ────────── pull ──────►|  ┌─────────────────┐  |◄── ALB / API GW
   (image)                    |  │  agent (8080)   │  |
                              |  │  AEGRAIL_*      │  |
                              |  │  OPENROUTER_API_│◄─┼─── injected via
                              |  │    KEY (secret) │  |    task definition
                              |  └─────────────────┘  |    'secrets'
                              |  (sidecar slot for    |
                              |   v0.3 engine)        |
                              +-----------------------+
                                       │
                          ┌────────────┴────────────┐
                          ▼                         ▼
                  Task role (assumed by             Execution role
                  the running container):           (used at start):
                  - secretsmanager:GetSecretValue   - ECR pull
                  - kms:Decrypt (CMK)               - CW Logs create
```

## Resources you'll create

| Resource | Cost when idle |
|---|---|
| ECR repo | ~$0.10/GB-month |
| ECS cluster (Fargate) | $0 |
| Fargate task running 24/7 (0.25 vCPU / 0.5GB) | ~$9/month |
| ALB | ~$16/month |
| KMS CMK | $1/month |
| Secrets Manager secret | $0.40/month |
| Task + execution roles | $0 |

If you don't need an ALB (background worker, EventBridge-triggered),
omit it — the ECS service can run on Fargate Spot for ~70% cheaper.

## End-to-end deploy

```bash
export AWS_REGION=us-east-1
export OPENROUTER_API_KEY=sk-or-...
export CLUSTER=aegrail-sample
export SERVICE=aegrail-sample
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# 1. KMS + Secrets Manager (same as App Runner guide)
KEY_ID=$(aws kms create-key --description "aegrail-fargate CMK" --query 'KeyMetadata.KeyId' --output text)
KEY_ARN=$(aws kms describe-key --key-id "$KEY_ID" --query 'KeyMetadata.Arn' --output text)
aws kms create-alias --alias-name alias/aegrail-fargate --target-key-id "$KEY_ID"
SECRET_ARN=$(aws secretsmanager create-secret \
  --name aegrail/fargate/openrouter-key \
  --kms-key-id "$KEY_ARN" \
  --secret-string "$OPENROUTER_API_KEY" \
  --query 'ARN' --output text)

# 2. ECR + image push
aws ecr create-repository --repository-name aegrail-sample >/dev/null
ECR_URI="$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/aegrail-sample"
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
docker build --platform linux/amd64 -t "$ECR_URI:v0.2.6" examples/deployments/app-runner
docker push "$ECR_URI:v0.2.6"

# 3. Two IAM roles
cat > /tmp/ecs-trust.json <<'EOF'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}
EOF

# Execution role: ECR pull + CW Logs (used by the agent that starts the task)
aws iam create-role --role-name aegrailSampleExecutionRole \
  --assume-role-policy-document file:///tmp/ecs-trust.json >/dev/null
aws iam attach-role-policy --role-name aegrailSampleExecutionRole \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
# Execution role also reads the secret at container-start
cat > /tmp/exec-secret-policy.json <<EOF
{
  "Version":"2012-10-17",
  "Statement":[
    {"Effect":"Allow","Action":"secretsmanager:GetSecretValue","Resource":"$SECRET_ARN"},
    {"Effect":"Allow","Action":"kms:Decrypt","Resource":"$KEY_ARN"}
  ]
}
EOF
aws iam put-role-policy --role-name aegrailSampleExecutionRole \
  --policy-name SecretAccess --policy-document file:///tmp/exec-secret-policy.json

# Task role: AWS API access from inside the running container (empty for now)
aws iam create-role --role-name aegrailSampleTaskRole \
  --assume-role-policy-document file:///tmp/ecs-trust.json >/dev/null

EXEC_ROLE_ARN=$(aws iam get-role --role-name aegrailSampleExecutionRole --query 'Role.Arn' --output text)
TASK_ROLE_ARN=$(aws iam get-role --role-name aegrailSampleTaskRole --query 'Role.Arn' --output text)

# 4. Task definition
aws logs create-log-group --log-group-name /ecs/aegrail-sample >/dev/null
cat > /tmp/taskdef.json <<EOF
{
  "family": "aegrail-sample",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "256",
  "memory": "512",
  "executionRoleArn": "$EXEC_ROLE_ARN",
  "taskRoleArn": "$TASK_ROLE_ARN",
  "containerDefinitions": [{
    "name": "agent",
    "image": "$ECR_URI:v0.2.6",
    "essential": true,
    "portMappings": [{"containerPort": 8080, "protocol": "tcp"}],
    "environment": [
      {"name": "AEGRAIL_AGENT_IDENTITY", "value": "fargate-sample/v1"},
      {"name": "AEGRAIL_BUDGET_USD", "value": "5.00"},
      {"name": "AEGRAIL_BUDGET_TOKENS", "value": "100000"},
      {"name": "AEGRAIL_BUDGET_WALL_SECONDS", "value": "120"},
      {"name": "AEGRAIL_BUDGET_MAX_TOOL_CALLS", "value": "10"},
      {"name": "AEGRAIL_EGRESS_ALLOWLIST", "value": "openrouter.ai"},
      {"name": "AEGRAIL_AUDIT_STDOUT", "value": "1"},
      {"name": "OPENROUTER_MODEL", "value": "openai/gpt-4o-mini"}
    ],
    "secrets": [
      {"name": "OPENROUTER_API_KEY", "valueFrom": "$SECRET_ARN"}
    ],
    "logConfiguration": {
      "logDriver": "awslogs",
      "options": {
        "awslogs-group": "/ecs/aegrail-sample",
        "awslogs-region": "$AWS_REGION",
        "awslogs-stream-prefix": "agent"
      }
    }
  }]
}
EOF
aws ecs register-task-definition --cli-input-json file:///tmp/taskdef.json >/dev/null

# 5. ECS cluster + service (assumes a default VPC with public subnets; for prod, use private subnets + NAT)
aws ecs create-cluster --cluster-name "$CLUSTER" >/dev/null
SUBNETS=$(aws ec2 describe-subnets --filters Name=default-for-az,Values=true --query 'Subnets[*].SubnetId' --output text | tr '\t' ',')
SG=$(aws ec2 create-security-group --group-name aegrail-sample-sg --description "aegrail sample" --query 'GroupId' --output text)
aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port 8080 --cidr 0.0.0.0/0 >/dev/null
aws ecs create-service \
  --cluster "$CLUSTER" --service-name "$SERVICE" \
  --task-definition aegrail-sample --launch-type FARGATE \
  --desired-count 1 \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=ENABLED}" \
  >/dev/null
```

## What the env split looks like

| aegrail env | Task definition mechanism |
|---|---|
| `AEGRAIL_*` non-secret | `environment[]` array |
| `OPENROUTER_API_KEY` | `secrets[]` array → `valueFrom: <secret ARN>` |

The **execution role**, not the task role, is what pulls the secret
at container start. The task role is for AWS API calls *from inside
the running container* (S3 reads, DynamoDB queries, etc.).

## Verification

Get the task's public IP (in dev) or hit the ALB (in prod):

```bash
TASK_ARN=$(aws ecs list-tasks --cluster "$CLUSTER" --service-name "$SERVICE" --query 'taskArns[0]' --output text)
ENI=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN" --query 'tasks[0].attachments[0].details[?name==`networkInterfaceId`].value' --output text)
IP=$(aws ec2 describe-network-interfaces --network-interface-ids "$ENI" --query 'NetworkInterfaces[0].Association.PublicIp' --output text)
curl -sX POST "http://$IP:8080/chat" -H 'Content-Type: application/json' \
  -d '{"message":"Reply with exactly: Fargate + KMS aegrail works."}'
```

## Sidecar slot — where the v0.3 engine plugs in

A second container in the same task definition shares the network
namespace. For aegrail's forthcoming Go egress proxy:

```json
{
  "name": "aegrail-engine",
  "image": "ghcr.io/aegrail/aegrail-engine:v0.3.0",
  "essential": false,
  "portMappings": [{"containerPort": 9099, "protocol": "tcp"}],
  "environment": [
    {"name": "AEGRAIL_ENGINE_ALLOWLIST", "value": "openrouter.ai"}
  ]
}
```

The agent container then runs with `HTTP_PROXY=http://localhost:9099`
to route all outbound HTTP through the proxy — which enforces the
allowlist at the network layer, not in-process.

This is the structural-completeness story Fargate enables that App
Runner doesn't.

## Audit chain destination

`AEGRAIL_AUDIT_STDOUT=1` streams into the CloudWatch log group
`/ecs/aegrail-sample`. Stream each aegrail event lands as one line.
Sink to S3 via Kinesis Firehose subscription filter for audit
retention.

## Production checklist (Fargate-specific)

- [ ] Run in **private subnets** with a NAT gateway. The example
      above uses public subnets for brevity; do not ship that to prod.
- [ ] Front the service with an **Application Load Balancer**.
      Configure ALB → target group health check on `/healthz`.
- [ ] Set `desiredCount` and use **service auto-scaling** based on
      request count, CPU, or memory. ECS scales Fargate tasks, not
      EC2 instances.
- [ ] Use **Fargate Spot** for stateless agents — ~70% cost
      reduction; ECS replaces Spot-interrupted tasks within ~2 min.
- [ ] Set `enableExecuteCommand: true` so you can `ecs execute-command`
      into a running task for incident response — but lock down which
      principals can call it.
- [ ] Enable **CloudWatch Container Insights** on the cluster for
      task-level metrics.
- [ ] Rotate Secrets Manager secrets on schedule. Tasks pick up
      rotated secrets on the **next task start**; force-redeploy the
      service after rotation if you want immediate uptake:
      `aws ecs update-service --force-new-deployment`.

## Teardown

```bash
aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" --desired-count 0 >/dev/null
aws ecs delete-service --cluster "$CLUSTER" --service "$SERVICE" --force >/dev/null
aws ecs delete-cluster --cluster "$CLUSTER" >/dev/null
aws ec2 delete-security-group --group-id "$SG" >/dev/null
aws ecr delete-repository --repository-name aegrail-sample --force >/dev/null
aws iam delete-role-policy --role-name aegrailSampleExecutionRole --policy-name SecretAccess
aws iam detach-role-policy --role-name aegrailSampleExecutionRole \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
aws iam delete-role --role-name aegrailSampleExecutionRole
aws iam delete-role --role-name aegrailSampleTaskRole
aws secretsmanager delete-secret --secret-id aegrail/fargate/openrouter-key --force-delete-without-recovery
aws kms delete-alias --alias-name alias/aegrail-fargate
aws kms schedule-key-deletion --key-id "$KEY_ID" --pending-window-in-days 7
aws logs delete-log-group --log-group-name /ecs/aegrail-sample
```
