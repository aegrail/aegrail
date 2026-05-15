#!/usr/bin/env bash
# Tear down the aegrail App Runner sample.
#
# Usage:
#   ./teardown.sh
#
# Honors the same env var overrides as deploy.sh.

set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
ECR_REPO="${ECR_REPO:-aegrail-sample}"
SERVICE_NAME="${SERVICE_NAME:-aegrail-sample}"
ROLE_NAME="AppRunnerECRAccessRole-${SERVICE_NAME}"

echo "=== Delete App Runner service ==="
SVC_ARN=$(aws apprunner list-services --region "${AWS_REGION}" \
  --query "ServiceSummaryList[?ServiceName=='${SERVICE_NAME}'].ServiceArn" --output text)
if [ -n "${SVC_ARN}" ]; then
  aws apprunner delete-service --region "${AWS_REGION}" --service-arn "${SVC_ARN}" >/dev/null
  echo "delete initiated; waiting..."
  for i in $(seq 1 30); do
    s=$(aws apprunner describe-service --region "${AWS_REGION}" --service-arn "${SVC_ARN}" --query 'Service.Status' --output text 2>&1 || true)
    if echo "${s}" | grep -q "ResourceNotFound"; then break; fi
    [ "${s}" = "DELETED" ] && break
    echo "  [${i}] ${s}"; sleep 10
  done
else
  echo "(no service named ${SERVICE_NAME})"
fi

echo ""
echo "=== Delete ECR repository ==="
aws ecr delete-repository --region "${AWS_REGION}" --repository-name "${ECR_REPO}" --force 2>&1 | head -3 || true

echo ""
echo "=== Delete IAM access role ==="
aws iam detach-role-policy --role-name "${ROLE_NAME}" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess 2>/dev/null || true
aws iam delete-role --role-name "${ROLE_NAME}" 2>/dev/null || true

echo ""
echo "teardown complete"
