#!/usr/bin/env bash
# Kind integration test for the aegrail Python library.
#
# Spins up a local kind cluster, builds the sample agent container
# from this directory's Dockerfile (which installs aegrail from
# PyPI), runs it as a Pod with AEGRAIL_INTERCEPT=1 set in pod env,
# and asserts the agent's stdout contains the PASS line.
#
# Usage:
#   bash tests/integration/kind/run.sh
#
# Requirements on the host:
#   - docker (running)
#   - kind   (https://kind.sigs.k8s.io)
#   - kubectl
#
# This test is intentionally not gated on Ollama or any real LLM —
# the interceptors don't care what the destination of an HTTP call
# is, only whether it matches the allowlist. Faster, deterministic,
# CI-friendly.

set -euo pipefail

CLUSTER_NAME="${KIND_CLUSTER:-aegrail-kind-test}"
IMAGE_NAME="aegrail-sample-agent:kind-test"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POD_NAME="aegrail-sample-agent"
AEGRAIL_VERSION="${AEGRAIL_VERSION:-0.2.4}"

cleanup() {
  echo ""
  echo "=== cleanup ==="
  kind delete cluster --name "${CLUSTER_NAME}" 2>/dev/null || true
  docker image rm "${IMAGE_NAME}" 2>/dev/null || true
}
trap cleanup EXIT

echo "=== Prerequisites ==="
command -v docker >/dev/null || { echo "docker not installed"; exit 2; }
command -v kind >/dev/null   || { echo "kind not installed (brew install kind)"; exit 2; }
command -v kubectl >/dev/null || { echo "kubectl not installed"; exit 2; }
docker info >/dev/null 2>&1   || { echo "docker not running"; exit 2; }

echo "=== Create kind cluster '${CLUSTER_NAME}' ==="
kind create cluster --name "${CLUSTER_NAME}"

echo ""
echo "=== Build sample agent image (aegrail==${AEGRAIL_VERSION}) ==="
docker build \
  --build-arg "AEGRAIL_VERSION=${AEGRAIL_VERSION}" \
  -t "${IMAGE_NAME}" \
  -f "${SCRIPT_DIR}/Dockerfile" \
  "${SCRIPT_DIR}"

echo ""
echo "=== Load image into kind ==="
kind load docker-image "${IMAGE_NAME}" --name "${CLUSTER_NAME}"

echo ""
echo "=== Apply pod manifest ==="
kubectl apply -f "${SCRIPT_DIR}/pod.yaml"

echo ""
echo "=== Wait for pod to finish ==="
# The pod runs the agent script once and exits. Wait up to 120s for
# the pod to reach a terminal phase.
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
  echo "kind integration test: PASS"
  exit 0
else
  echo ""
  echo "kind integration test: FAIL"
  echo "(no PASS line in pod logs)"
  exit 1
fi
