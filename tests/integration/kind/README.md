# kind integration test for aegrail

This directory contains a minimal end-to-end test that validates the
aegrail Python library works inside a real Kubernetes pod, with the
in-process interceptors enabled via the pod-level `AEGRAIL_INTERCEPT=1`
env var (no agent code change required).

## What it tests

1. The aegrail PyPI wheel installs cleanly inside a slim Python
   container.
2. `AEGRAIL_INTERCEPT=1` set on the pod auto-installs the egress
   interceptor when the agent constructs an `Agent`.
3. An egress request to a host **not** in the allowlist raises
   `EgressNotPermitted` and emits an `egress_denied` audit event.
4. An egress request to a host **in** the allowlist passes the
   egress check (failing only at the network layer, which is
   expected and proves the check let it through).
5. The audit chain (SHA-256 over `prev_hash`) validates end-to-end
   on the events produced inside the pod.

If all of those pass, the agent's stdout contains a `PASS` line and
the test orchestrator exits 0.

## What it does NOT test

- Ollama or any real LLM (the interceptors don't care what the egress
  destination is — fast, deterministic, CI-friendly)
- The forthcoming aegrail-engine sidecar (that has its own kind
  test, in the `arpitcoder/aegrail-engine` repo)
- Cross-pod or cross-namespace policy

## Prerequisites

```bash
# macOS via Homebrew:
brew install docker kind kubectl
open -a Docker  # start the Docker daemon

# Linux:
# docker — distro package
# kind   — https://kind.sigs.k8s.io/docs/user/quick-start/#installation
# kubectl — https://kubernetes.io/docs/tasks/tools/
```

## Run

```bash
bash tests/integration/kind/run.sh
```

The script:

1. Creates a kind cluster named `aegrail-kind-test`
2. Builds the sample agent image (which installs `aegrail` from
   PyPI at the version pinned in `Dockerfile`)
3. Loads the image into the kind cluster
4. Applies the pod manifest
5. Polls for pod completion (~30 s typical)
6. Greps the pod logs for `PASS`
7. Tears down the cluster and image on exit

Override the aegrail version under test:

```bash
AEGRAIL_VERSION=0.2.5 bash tests/integration/kind/run.sh
```

## When to run this

This test is **release-gate quality** for the Python library — run it
before tagging `vX.Y.Z` if the release touches the interceptor code
path or the audit chain. For other releases (e.g. pure docs), the
isolated-venv battle-test against Ollama remains sufficient.

The `aegrail-engine` repo has its own kind test (in `RELEASING.md`
there) which gates the sidecar releases. The two are independent —
one validates the Python library; the other validates the K8s
sidecar.
