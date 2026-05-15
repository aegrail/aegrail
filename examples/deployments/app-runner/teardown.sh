#!/usr/bin/env bash
# Tear down the aegrail App Runner sample.
#
# Honors SECRETS_MODE=env|kms — `kms` also schedules the CMK for
# deletion (7-day minimum window) and force-deletes the secret.

set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
ECR_REPO="${ECR_REPO:-aegrail-sample}"
SERVICE_NAME="${SERVICE_NAME:-aegrail-sample}"
SECRETS_MODE="${SECRETS_MODE:-env}"
ACCESS_ROLE="AppRunnerECRAccessRole-${SERVICE_NAME}"
INSTANCE_ROLE="AppRunnerInstanceRole-${SERVICE_NAME}"
KMS_ALIAS="alias/${SERVICE_NAME}"
SECRET_NAME="aegrail/${SERVICE_NAME}/openrouter-key"

echo "=== App Runner service ==="
SVC_ARN=$(aws apprunner list-services --region "${AWS_REGION}" \
  --query "ServiceSummaryList[?ServiceName=='${SERVICE_NAME}'].ServiceArn" --output text)
if [ -n "${SVC_ARN}" ]; then
  aws apprunner delete-service --region "${AWS_REGION}" --service-arn "${SVC_ARN}" >/dev/null
  for i in $(seq 1 30); do
    s=$(aws apprunner describe-service --region "${AWS_REGION}" --service-arn "${SVC_ARN}" --query 'Service.Status' --output text 2>&1 || true)
    echo "$s" | grep -q "ResourceNotFound" && break
    [ "${s}" = "DELETED" ] && break
    echo "  [${i}] ${s}"; sleep 10
  done
fi

echo ""
echo "=== ECR repository ==="
aws ecr delete-repository --region "${AWS_REGION}" --repository-name "${ECR_REPO}" --force 2>&1 | head -2 || true

echo ""
echo "=== IAM roles ==="
aws iam detach-role-policy --role-name "${ACCESS_ROLE}" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess 2>/dev/null || true
aws iam delete-role --role-name "${ACCESS_ROLE}" 2>/dev/null || true
aws iam delete-role-policy --role-name "${INSTANCE_ROLE}" --policy-name SecretAccess 2>/dev/null || true
aws iam delete-role --role-name "${INSTANCE_ROLE}" 2>/dev/null || true

if [ "${SECRETS_MODE}" = "kms" ]; then
  echo ""
  echo "=== Secrets Manager secret (force, no recovery) ==="
  aws secretsmanager delete-secret --secret-id "${SECRET_NAME}" --force-delete-without-recovery 2>&1 | head -3 || true

  echo ""
  echo "=== KMS alias + scheduled key deletion (7-day window) ==="
  KEY_ID=$(aws kms describe-key --key-id "${KMS_ALIAS}" --query 'KeyMetadata.KeyId' --output text 2>/dev/null || true)
  aws kms delete-alias --alias-name "${KMS_ALIAS}" 2>/dev/null || true
  if [ -n "${KEY_ID}" ]; then
    aws kms schedule-key-deletion --key-id "${KEY_ID}" --pending-window-in-days 7 \
      --query 'DeletionDate' --output text 2>&1 || true
  fi
fi

echo ""
echo "teardown complete"
