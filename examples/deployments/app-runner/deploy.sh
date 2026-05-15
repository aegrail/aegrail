#!/usr/bin/env bash
# Deploy the aegrail App Runner sample.
#
# Two modes — pick based on environment:
#   SECRETS_MODE=env  (default, dev)   OpenRouter key passed as a
#                                       plain RuntimeEnvironmentVariables
#                                       entry. NEVER for production.
#   SECRETS_MODE=kms  (production)     OpenRouter key stored in
#                                       Secrets Manager, encrypted by
#                                       a customer-managed KMS key
#                                       (CMK), injected via
#                                       RuntimeEnvironmentSecrets and
#                                       a least-privilege instance
#                                       role.
#
# Prereqs:
#   - AWS CLI v2 (authenticated)
#   - Docker running
#   - OPENROUTER_API_KEY exported in the calling shell
#
# Optional overrides (defaults shown):
#   AWS_REGION=us-east-1
#   ECR_REPO=aegrail-sample
#   SERVICE_NAME=aegrail-sample
#   IMAGE_TAG=v0.2.6
#   OPENROUTER_MODEL=openai/gpt-4o-mini
#   SECRETS_MODE=env
#   AGENT_IDENTITY=app-runner-sample/v1
#   BUDGET_USD=0.10
#   BUDGET_TOKENS=4000
#   BUDGET_WALL_SECONDS=60
#   EGRESS_ALLOWLIST=openrouter.ai

set -euo pipefail

: "${OPENROUTER_API_KEY:?OPENROUTER_API_KEY must be exported}"

AWS_REGION="${AWS_REGION:-us-east-1}"
ECR_REPO="${ECR_REPO:-aegrail-sample}"
SERVICE_NAME="${SERVICE_NAME:-aegrail-sample}"
IMAGE_TAG="${IMAGE_TAG:-v0.2.6}"
OPENROUTER_MODEL="${OPENROUTER_MODEL:-openai/gpt-4o-mini}"
SECRETS_MODE="${SECRETS_MODE:-env}"
AGENT_IDENTITY="${AGENT_IDENTITY:-app-runner-sample/v1}"
BUDGET_USD="${BUDGET_USD:-0.10}"
BUDGET_TOKENS="${BUDGET_TOKENS:-4000}"
BUDGET_WALL_SECONDS="${BUDGET_WALL_SECONDS:-60}"
EGRESS_ALLOWLIST="${EGRESS_ALLOWLIST:-openrouter.ai}"

ACCESS_ROLE="AppRunnerECRAccessRole-${SERVICE_NAME}"
INSTANCE_ROLE="AppRunnerInstanceRole-${SERVICE_NAME}"
KMS_ALIAS="alias/${SERVICE_NAME}"
SECRET_NAME="aegrail/${SERVICE_NAME}/openrouter-key"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Preflight ==="
command -v aws >/dev/null || { echo "aws CLI not installed"; exit 2; }
command -v docker >/dev/null || { echo "docker not installed"; exit 2; }
docker info >/dev/null 2>&1 || { echo "docker not running"; exit 2; }
case "${SECRETS_MODE}" in env|kms) ;; *) echo "SECRETS_MODE must be env|kms"; exit 2;; esac

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}"
echo "account=${ACCOUNT_ID} region=${AWS_REGION} mode=${SECRETS_MODE} ecr=${ECR_URI}"

echo ""
echo "=== ECR repository + image push ==="
aws ecr describe-repositories --region "${AWS_REGION}" --repository-names "${ECR_REPO}" >/dev/null 2>&1 || \
  aws ecr create-repository --region "${AWS_REGION}" --repository-name "${ECR_REPO}" \
    --image-scanning-configuration scanOnPush=true >/dev/null
aws ecr get-login-password --region "${AWS_REGION}" | \
  docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
docker build --platform linux/amd64 -t "${ECR_URI}:${IMAGE_TAG}" "${SCRIPT_DIR}"
docker push "${ECR_URI}:${IMAGE_TAG}"

echo ""
echo "=== ECR access role ==="
if ! aws iam get-role --role-name "${ACCESS_ROLE}" >/dev/null 2>&1; then
  T=$(mktemp); cat > "${T}" <<EOF
{ "Version":"2012-10-17", "Statement":[{"Effect":"Allow","Principal":{"Service":"build.apprunner.amazonaws.com"},"Action":"sts:AssumeRole"}] }
EOF
  aws iam create-role --role-name "${ACCESS_ROLE}" --assume-role-policy-document "file://${T}" >/dev/null
  aws iam attach-role-policy --role-name "${ACCESS_ROLE}" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess
  rm -f "${T}"
  sleep 10
fi
ACCESS_ROLE_ARN=$(aws iam get-role --role-name "${ACCESS_ROLE}" --query 'Role.Arn' --output text)

INSTANCE_ROLE_ARN=""
SECRET_ARN=""
KEY_ARN=""

if [ "${SECRETS_MODE}" = "kms" ]; then
  echo ""
  echo "=== KMS customer-managed key + alias ==="
  if ! aws kms describe-key --key-id "${KMS_ALIAS}" >/dev/null 2>&1; then
    KEY_ID=$(aws kms create-key \
      --description "aegrail ${SERVICE_NAME} secret encryption" \
      --tags TagKey=Project,TagValue=aegrail TagKey=Service,TagValue="${SERVICE_NAME}" \
      --query 'KeyMetadata.KeyId' --output text)
    aws kms create-alias --alias-name "${KMS_ALIAS}" --target-key-id "${KEY_ID}"
  fi
  KEY_ARN=$(aws kms describe-key --key-id "${KMS_ALIAS}" --query 'KeyMetadata.Arn' --output text)
  echo "KEY_ARN=${KEY_ARN}"

  echo ""
  echo "=== Secrets Manager secret (encrypted by CMK) ==="
  if SECRET_ARN=$(aws secretsmanager describe-secret --secret-id "${SECRET_NAME}" --query 'ARN' --output text 2>/dev/null); then
    aws secretsmanager update-secret --secret-id "${SECRET_ARN}" \
      --kms-key-id "${KEY_ARN}" \
      --secret-string "${OPENROUTER_API_KEY}" >/dev/null
  else
    SECRET_ARN=$(aws secretsmanager create-secret \
      --name "${SECRET_NAME}" \
      --description "OpenRouter API key for ${SERVICE_NAME}" \
      --kms-key-id "${KEY_ARN}" \
      --secret-string "${OPENROUTER_API_KEY}" \
      --query 'ARN' --output text)
  fi
  echo "SECRET_ARN=${SECRET_ARN}"

  echo ""
  echo "=== Instance role with least-privilege secret access ==="
  if ! aws iam get-role --role-name "${INSTANCE_ROLE}" >/dev/null 2>&1; then
    T=$(mktemp); cat > "${T}" <<EOF
{ "Version":"2012-10-17", "Statement":[{"Effect":"Allow","Principal":{"Service":"tasks.apprunner.amazonaws.com"},"Action":"sts:AssumeRole"}] }
EOF
    aws iam create-role --role-name "${INSTANCE_ROLE}" --assume-role-policy-document "file://${T}" >/dev/null
    rm -f "${T}"
  fi
  P=$(mktemp); cat > "${P}" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect":"Allow","Action":"secretsmanager:GetSecretValue","Resource":"${SECRET_ARN}"},
    {"Effect":"Allow","Action":"kms:Decrypt","Resource":"${KEY_ARN}"}
  ]
}
EOF
  aws iam put-role-policy --role-name "${INSTANCE_ROLE}" --policy-name SecretAccess --policy-document "file://${P}"
  rm -f "${P}"
  sleep 12
  INSTANCE_ROLE_ARN=$(aws iam get-role --role-name "${INSTANCE_ROLE}" --query 'Role.Arn' --output text)
  echo "INSTANCE_ROLE_ARN=${INSTANCE_ROLE_ARN}"
fi

echo ""
echo "=== App Runner service ==="
SC=$(mktemp)
RUNTIME_ENV="{
  \"AEGRAIL_AGENT_IDENTITY\": \"${AGENT_IDENTITY}\",
  \"AEGRAIL_BUDGET_USD\": \"${BUDGET_USD}\",
  \"AEGRAIL_BUDGET_TOKENS\": \"${BUDGET_TOKENS}\",
  \"AEGRAIL_BUDGET_WALL_SECONDS\": \"${BUDGET_WALL_SECONDS}\",
  \"AEGRAIL_BUDGET_MAX_TOOL_CALLS\": \"5\",
  \"AEGRAIL_EGRESS_ALLOWLIST\": \"${EGRESS_ALLOWLIST}\",
  \"AEGRAIL_AUDIT_STDOUT\": \"1\",
  \"OPENROUTER_MODEL\": \"${OPENROUTER_MODEL}\""
if [ "${SECRETS_MODE}" = "env" ]; then
  RUNTIME_ENV="${RUNTIME_ENV},
  \"OPENROUTER_API_KEY\": \"${OPENROUTER_API_KEY}\""
  RUNTIME_SECRETS=""
else
  RUNTIME_SECRETS=",
      \"RuntimeEnvironmentSecrets\": {
        \"OPENROUTER_API_KEY\": \"${SECRET_ARN}\"
      }"
fi
RUNTIME_ENV="${RUNTIME_ENV}
}"

cat > "${SC}" <<EOF
{
  "ImageRepository": {
    "ImageIdentifier": "${ECR_URI}:${IMAGE_TAG}",
    "ImageConfiguration": {
      "Port": "8080",
      "RuntimeEnvironmentVariables": ${RUNTIME_ENV}${RUNTIME_SECRETS}
    },
    "ImageRepositoryType": "ECR"
  },
  "AutoDeploymentsEnabled": false,
  "AuthenticationConfiguration": {
    "AccessRoleArn": "${ACCESS_ROLE_ARN}"
  }
}
EOF

if [ "${SECRETS_MODE}" = "kms" ]; then
  IC="Cpu=0.25 vCPU,Memory=0.5 GB,InstanceRoleArn=${INSTANCE_ROLE_ARN}"
else
  IC="Cpu=0.25 vCPU,Memory=0.5 GB"
fi

aws apprunner create-service \
  --region "${AWS_REGION}" \
  --service-name "${SERVICE_NAME}" \
  --source-configuration "file://${SC}" \
  --instance-configuration "${IC}" \
  --health-check-configuration 'Protocol=HTTP,Path=/healthz,Interval=10,Timeout=5,HealthyThreshold=1,UnhealthyThreshold=5' \
  --query 'Service.ServiceArn' --output text > /tmp/aegrail-apprunner-arn.txt
SVC_ARN=$(cat /tmp/aegrail-apprunner-arn.txt)
rm -f "${SC}"
echo "service ARN: ${SVC_ARN}"

echo ""
echo "=== Wait for RUNNING ==="
for i in $(seq 1 40); do
  s=$(aws apprunner describe-service --region "${AWS_REGION}" --service-arn "${SVC_ARN}" --query 'Service.Status' --output text)
  echo "  [${i}] ${s}"
  case "${s}" in
    RUNNING) break;;
    CREATE_FAILED) echo "create failed"; exit 1;;
  esac
  sleep 15
done

URL="https://$(aws apprunner describe-service --region "${AWS_REGION}" --service-arn "${SVC_ARN}" --query 'Service.ServiceUrl' --output text)"
echo ""
echo "URL: ${URL}"
echo ""
echo "Try it:"
echo "  curl ${URL}/"
echo "  curl -X POST ${URL}/chat -H 'Content-Type: application/json' -d '{\"message\":\"hello\"}'"
echo ""
echo "Teardown:"
echo "  AWS_REGION=${AWS_REGION} SERVICE_NAME=${SERVICE_NAME} ECR_REPO=${ECR_REPO} SECRETS_MODE=${SECRETS_MODE} ./teardown.sh"
