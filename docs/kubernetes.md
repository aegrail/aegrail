# Deploying aegrail-protected agents on Kubernetes

> **For the full production guide** (ConfigMap + External Secrets
> Operator + KMS-backed secrets + NetworkPolicy + the production
> checklist), see [`deployments/kubernetes.md`](deployments/kubernetes.md).
>
> This page covers the original v0.2.4 interceptor-focused pattern
> for reference. It still works and is still recommended as part of
> the layered model — the deployments guide adds the v0.2.6
> env-var pattern on top.

This guide covers the recommended K8s deployment pattern for AI
agents that use the aegrail Python library. The pattern is
**developer-effortless**: the agent code does not change between
local development and a hardened K8s deployment; the platform
controls enforcement via pod-level env vars.

For the forthcoming sidecar (engine + Helm chart in
[`arpitcoder/aegrail-engine`](https://github.com/arpitcoder/aegrail-engine)),
which adds network-layer enforcement on top of the in-process
interceptors documented below, see that repo.

---

## The pattern, in three pieces

1. **Build the agent container with `aegrail` installed.**

   ```dockerfile
   FROM python:3.12-slim
   RUN pip install --no-cache-dir aegrail==0.2.4
   COPY your_agent.py /your_agent.py
   ENV PYTHONUNBUFFERED=1
   CMD ["python", "/your_agent.py"]
   ```

2. **Set `AEGRAIL_INTERCEPT=1` in the pod spec.** This causes
   `aegrail.Agent.__init__` to auto-install the in-process
   interceptors when the agent first constructs an `Agent` object.
   Your agent code does not need to call `intercept_outbound()` or
   `install_audit_hook()` explicitly.

   ```yaml
   apiVersion: v1
   kind: Pod
   metadata:
     name: my-agent
   spec:
     containers:
       - name: agent
         image: your-registry/my-agent:1.0
         env:
           - name: AEGRAIL_INTERCEPT
             value: "1"
   ```

3. **Declare the egress allowlist on the `Agent`.** This can come
   from a ConfigMap mounted as env vars or as a file the agent
   reads — your call.

   ```python
   import os
   from aegrail import Agent, Budget, AuditSink

   agent = Agent(
       identity="my-agent/v1",
       budget=Budget(usd=5.0, max_tool_calls=10),
       audit=AuditSink.file("/var/log/aegrail/audit.jsonl"),
       egress_allowlist=os.environ.get(
           "AEGRAIL_EGRESS_ALLOWLIST", ""
       ).split(",") or None,
   )
   ```

With those three pieces, any outbound HTTP request inside an
active aegrail session is checked against the allowlist; denied
destinations raise `EgressNotPermitted` and emit `egress_denied`
audit events that chain into the SHA-256 tamper-evident log.

---

## End-to-end validated pattern

The repo ships a working kind cluster test that exercises all of
the above: [`tests/integration/kind/`](../tests/integration/kind/).
Run it locally:

```bash
bash tests/integration/kind/run.sh
```

The script creates a kind cluster, builds the sample agent
container (which installs `aegrail` from PyPI), loads the image
into the cluster, applies a Pod manifest with `AEGRAIL_INTERCEPT=1`
set, and asserts the agent's stdout shows both an
`EgressNotPermitted` denial *and* an allowed-host pass-through.

This test is the K8s-side equivalent of the Python release rule
"no PyPI publish until the wheel passes a real LLM smoke test":
release-gate validation that the production deployment path works.

---

## Audit log collection

The `AuditSink.file(path)` sink writes JSONL line-by-line. The
typical pattern on K8s:

1. Mount an `emptyDir` volume at `/var/log/aegrail/` inside the
   container.
2. Configure the agent to write to `/var/log/aegrail/audit.jsonl`.
3. Run a log-shipping sidecar (Fluent Bit, Vector, Promtail) that
   tails the file and ships to your central log store (Loki,
   Elasticsearch, ClickHouse, S3).

Each event includes `prev_hash` + `event_hash`. The collector can
verify the chain at ingest, or you can run `verify_chain()`
periodically on a window of events to confirm no tampering.

Alternative: write to stdout via `AuditSink.stdout()` and let the
cluster's existing log shipper collect the pod logs. Same data,
simpler. The trade-off: stdout-collected JSONL can interleave with
non-aegrail log lines in the same pod, so you'd need a tag or
filter on the collection side.

---

## What this pattern doesn't cover (use the sidecar)

The Python interceptors are powerful for Python agents but they
can't catch:

- **Non-Python agents** — Node, Go, Rust agents in the same
  cluster (or even the same pod)
- **`ctypes` / raw-socket bypasses** — a determined bypass via
  `ctypes.CDLL("libc.so.6")` or direct socket creation evades
  the monkey-patches
- **Subprocesses** — `subprocess.Popen(["curl", ...])` makes
  HTTP requests from a different process

For those, the `aegrail-engine` sidecar (Go HTTP forward proxy,
deployed alongside the agent pod via Helm chart) enforces at the
network layer. It catches everything that ends up at a socket,
regardless of language or library. Use the two layers together;
each protects a different bypass vector.

See the layered model in [`ARCHITECTURE.md`](../ARCHITECTURE.md).

---

## Troubleshooting

**Pod starts but interceptor doesn't fire.**
Check the pod env: `kubectl exec <pod> -- env | grep AEGRAIL`.
If `AEGRAIL_INTERCEPT=1` isn't set, the auto-install doesn't run.

**Interceptor fires but logs go nowhere.**
By default the audit sink is `StdoutAuditSink` — events go to the
container's stdout. If you set `AuditSink.file(...)` make sure the
mount and path exist with write permissions for the agent's UID.

**Chain validation fails.**
Most common cause: someone manually edited the JSONL file. The
chain is designed to detect exactly that. Run
`verify_chain(events)` to find the first bad index; the event
before that index is the last known-good record.

**Interceptor blocks calls during library import.**
This is intentional: interceptors only enforce *inside an active
session*. Calls outside `with agent.session(...)` pass through.
If you see denials during agent setup, you're inside a session
earlier than you expected.
