#!/usr/bin/env bash
# Deploy the aegrail App Runner sample.
#
# Prereqs:
#   - AWS CLI v2 and authenticated session (e.g. `aws sso login`)
#   - Docker running
#   - Environment variables exported for the LLM provider key:
#       export OPENROUTER_API_KEY=sk-or-...
#
# Optional overrides (defaults shown):
#   AWS_REGION=us-east-1
#   ECR_REPO=aegrail-sample
#   SERVICE_NAME=aegrail-sample
#   IMAGE_TAG=v0.2.6
#   OPENROUTER_MODEL=openai/gpt-4o-mini
#
# Usage:
#   ./deploy.sh

set -euo pipefail

: "${OPENROUTER_API_KEY:?OPENROUTER_API_KEY must be exported in the calling shell}"

AWS_REGION="${AWS_REGION:-us-east-1}"
ECR_REPO="${ECR_REPO:-aegrail-sample}"
SERVICE_NAME="${SERVICE_NAME:-aegrail-sample}"
IMAGE_TAG="${IMAGE_TAG:-v0.2.6}"
OPENROUTER_MODEL="${OPENROUTER_MODEL:-openai/gpt-4o-mini}"
ROLE_NAME="AppRunnerECRAccessRole-${SERVICE_NAME}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Preflight ==="
command -v aws >/dev/null || { echo "aws CLI not installed"; exit 2; }
command -v docker >/dev/null || { echo "docker not installed"; exit 2; }
docker info >/dev/null 2>&1 || { echo "docker not running"; exit 2; }

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}"
echo "account=${ACCOUNT_ID} region=${AWS_REGION} ecr=${ECR_URI}"

echo ""
echo "=== Ensure ECR repository ==="
aws ecr describe-repositories --repository-names "${ECR_REPO}" --region "${AWS_REGION}" >/dev/null 2>&1 || \
  aws ecr create-repository --repository-name "${ECR_REPO}" --region "${AWS_REGION}" \
    --image-scanning-configuration scanOnPush=true >/dev/null

echo ""
echo "=== Build & push image ==="
aws ecr get-login-password --region "${AWS_REGION}" | \
  docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
# App Runner needs linux/amd64 — force the platform.
docker build --platform linux/amd64 -t "${ECR_URI}:${IMAGE_TAG}" "${SCRIPT_DIR}"
docker push "${ECR_URI}:${IMAGE_TAG}"

echo ""
echo "=== Ensure ECR access role ==="
if ! aws iam get-role --role-name "${ROLE_NAME}" >/dev/null 2>&1; then
  TRUST_DOC=$(mktemp)
  cat > "${TRUST_DOC}" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "build.apprunner.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}
EOF
  aws iam create-role --role-name "${ROLE_NAME}" \
    --assume-role-policy-document "file://${TRUST_DOC}" \
    --description "App Runner ECR access for ${SERVICE_NAME}" >/dev/null
  aws iam attach-role-policy --role-name "${ROLE_NAME}" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess
  rm -f "${TRUST_DOC}"
  # IAM role propagation often needs a few seconds before App Runner can assume it
  sleep 10
fi
ROLE_ARN=$(aws iam get-role --role-name "${ROLE_NAME}" --query 'Role.Arn' --output text)

echo ""
echo "=== Create App Runner service ==="
SOURCE_CONFIG=$(mktemp)
cat > "${SOURCE_CONFIG}" <<EOF
{
  "ImageRepository": {
    "ImageIdentifier": "${ECR_URI}:${IMAGE_TAG}",
    "ImageConfiguration": {
      "Port": "8080",
      "RuntimeEnvironmentVariables": {
        "AEGRAIL_AGENT_IDENTITY": "app-runner-sample/v1",
        "AEGRAIL_BUDGET_USD": "0.10",
        "AEGRAIL_BUDGET_TOKENS": "4000",
        "AEGRAIL_BUDGET_WALL_SECONDS": "60",
        "AEGRAIL_BUDGET_MAX_TOOL_CALLS": "5",
        "AEGRAIL_EGRESS_ALLOWLIST": "openrouter.ai",
        "AEGRAIL_AUDIT_STDOUT": "1",
        "OPENROUTER_API_KEY": "${OPENROUTER_API_KEY}",
        "OPENROUTER_MODEL": "${OPENROUTER_MODEL}"
      }
    },
    "ImageRepositoryType": "ECR"
  },
  "AutoDeploymentsEnabled": false,
  "AuthenticationConfiguration": {
    "AccessRoleArn": "${ROLE_ARN}"
  }
}
EOF

aws apprunner create-service \
  --region "${AWS_REGION}" \
  --service-name "${SERVICE_NAME}" \
  --source-configuration "file://${SOURCE_CONFIG}" \
  --instance-configuration 'Cpu=0.25 vCPU,Memory=0.5 GB' \
  --health-check-configuration 'Protocol=HTTP,Path=/healthz,Interval=10,Timeout=5,HealthyThreshold=1,UnhealthyThreshold=5' \
  --query 'Service.ServiceArn' --output text > /tmp/aegrail-apprunner-arn.txt
SVC_ARN=$(cat /tmp/aegrail-apprunner-arn.txt)
rm -f "${SOURCE_CONFIG}"
echo "service ARN: ${SVC_ARN}"

echo ""
echo "=== Wait for RUNNING ==="
for i in $(seq 1 40); do
  s=$(aws apprunner describe-service --region "${AWS_REGION}" --service-arn "${SVC_ARN}" --query 'Service.Status' --output text)
  echo "  [${i}] ${s}"
  case "${s}" in
    RUNNING)        break ;;
    CREATE_FAILED)  echo "service create failed"; exit 1 ;;
  esac
  sleep 15
done

URL="https://$(aws apprunner describe-service --region "${AWS_REGION}" --service-arn "${SVC_ARN}" --query 'Service.ServiceUrl' --output text)"
echo ""
echo "Service URL: ${URL}"
echo ""
echo "Try it:"
echo "  curl ${URL}/"
echo "  curl -X POST ${URL}/chat -H 'Content-Type: application/json' -d '{\"message\":\"hello\"}'"
echo ""
echo "Tear down when done:"
echo "  AWS_REGION=${AWS_REGION} SERVICE_NAME=${SERVICE_NAME} ECR_REPO=${ECR_REPO} ./teardown.sh"
