#!/usr/bin/env bash
# Kind integration test for v0.2.6 ConfigMap-driven configuration.
#
# Validates the operator story: identity, budget, egress allowlist,
# and audit destination ALL come from a ConfigMap consumed via
# envFrom. Application code calls Agent.from_env() and never names
# the values.
#
# Usage:
#   bash tests/integration/kind/run_configmap.sh
#
# Reuses an existing kind cluster if KIND_CLUSTER is already running;
# otherwise creates one. Cleans up on exit unless KEEP_CLUSTER=1.

set -euo pipefail

CLUSTER_NAME="${KIND_CLUSTER:-aegrail-kind-test}"
IMAGE_NAME="aegrail-configmap-agent:kind-test"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POD_NAME="aegrail-configmap-agent"
AEGRAIL_VERSION="${AEGRAIL_VERSION:-0.2.6}"

cleanup() {
  if [ "${KEEP_CLUSTER:-0}" = "1" ]; then
    echo "(KEEP_CLUSTER=1; leaving cluster running)"
    return
  fi
  echo ""
  echo "=== cleanup ==="
  kubectl delete pod "${POD_NAME}" --ignore-not-found >/dev/null 2>&1 || true
  kubectl delete configmap aegrail-config --ignore-not-found >/dev/null 2>&1 || true
  kind delete cluster --name "${CLUSTER_NAME}" 2>/dev/null || true
  docker image rm "${IMAGE_NAME}" 2>/dev/null || true
}
trap cleanup EXIT

echo "=== Prerequisites ==="
command -v docker >/dev/null || { echo "docker not installed"; exit 2; }
command -v kind >/dev/null   || { echo "kind not installed (brew install kind)"; exit 2; }
command -v kubectl >/dev/null || { echo "kubectl not installed"; exit 2; }
docker info >/dev/null 2>&1   || { echo "docker not running"; exit 2; }

if kind get clusters | grep -qx "${CLUSTER_NAME}"; then
  echo "=== Reusing existing kind cluster '${CLUSTER_NAME}' ==="
else
  echo "=== Create kind cluster '${CLUSTER_NAME}' ==="
  kind create cluster --name "${CLUSTER_NAME}"
fi

echo ""
echo "=== Stage local wheel into build context ==="
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
WHEEL_PATH=$(ls -1 "${REPO_ROOT}/dist/aegrail-${AEGRAIL_VERSION}"-*.whl 2>/dev/null | head -1)
if [ -z "${WHEEL_PATH}" ]; then
  echo "no wheel matching aegrail-${AEGRAIL_VERSION}-*.whl in ${REPO_ROOT}/dist"
  echo "run 'python -m build' first"
  exit 2
fi
echo "using wheel: ${WHEEL_PATH}"
cp "${WHEEL_PATH}" "${SCRIPT_DIR}/"
STAGED_WHEEL="${SCRIPT_DIR}/$(basename "${WHEEL_PATH}")"
# Clean up the staged copy when the script exits
trap 'rm -f "${STAGED_WHEEL}"; cleanup' EXIT

echo ""
echo "=== Build agent image (aegrail==${AEGRAIL_VERSION} from local wheel) ==="
docker build \
  -t "${IMAGE_NAME}" \
  -f "${SCRIPT_DIR}/configmap_Dockerfile" \
  "${SCRIPT_DIR}"

echo ""
echo "=== Load image into kind ==="
kind load docker-image "${IMAGE_NAME}" --name "${CLUSTER_NAME}"

echo ""
echo "=== Apply ConfigMap and Pod ==="
kubectl apply -f "${SCRIPT_DIR}/configmap.yaml"
kubectl apply -f "${SCRIPT_DIR}/configmap_pod.yaml"

echo ""
echo "=== Wait for pod to finish ==="
for i in $(seq 1 60); do
  phase=$(kubectl get pod "${POD_NAME}" -o jsonpath='{.status.phase}' 2>/dev/null || echo "Unknown")
  echo "  poll ${i}: phase=${phase}"
  if [ "${phase}" = "Succeeded" ] || [ "${phase}" = "Failed" ]; then
    break
  fi
  sleep 2
done

echo ""
echo "=== Pod logs ==="
kubectl logs "${POD_NAME}"

echo ""
echo "=== Verify result ==="
logs="$(kubectl logs "${POD_NAME}")"
if echo "${logs}" | grep -q "^PASS"; then
  echo ""
  echo "kind ConfigMap integration test: PASS"
  exit 0
else
  echo ""
  echo "kind ConfigMap integration test: FAIL"
  echo "(no PASS line in pod logs)"
  exit 1
fi
