# aegrail architecture

This document captures the long-term shape of `aegrail` as a project,
why it has the surface it does today, and how the cross-language /
sidecar / engine story sequences. Written for contributors, reviewers,
and operators trying to understand whether aegrail's shape matches
their problem.

## The problem

Production AI agents need a runtime contract that the LLM cannot
argue out of: identity, budget, audit, tool authorization. The library
shape (`pip install aegrail`) is the *application-level* answer to
this — it works for any Python agent that routes its tool calls
through `session.call_tool(...)`. Today that's where aegrail lives.

Two structural gaps in the library-only shape have been raised by
senior reviewers and accepted as real:

1. **Language coverage.** A Python library serves only Python agents.
   Half the agent ecosystem (LangChain.js, Vercel AI SDK, MCP TS
   servers, langchaingo, rust-bert, JVM frameworks) is locked out.

2. **Bypass resistance.** Even within Python, a developer can skip
   aegrail by importing `requests` directly or shelling out to a
   subprocess. The library only governs what flows through
   `session.call_tool(...)`. For a security-adjacent product, the
   honest answer to "what stops a developer from bypassing this?"
   has to be better than "discipline."

This document describes the architecture that closes both gaps.

## Three layers, three responsibilities

```
┌────────────────────────────────────────────────────────────────────┐
│  Layer 3 — language SDKs                                           │
│  Thin clients per language. Same contract surface.                 │
│  - aegrail-py       (Python; current PyPI artifact)                │
│  - aegrail-js       (Node / TypeScript)        [future]            │
│  - aegrail-go       (Go)                       [future]            │
│  - aegrail-jvm      (Java / Kotlin)            [future]            │
└────────────────────────────────────────────────────────────────────┘
                                │
                                │  same wire protocol
                                ▼
┌────────────────────────────────────────────────────────────────────┐
│  Layer 2 — enforcement engine                                      │
│  Single binary, language-agnostic, the actual policy boundary.     │
│  - aegrail-engine   (Go, lives in arpitcoder/aegrail-engine repo)  │
│    - allowlist policy                                              │
│    - audit chain                                                   │
│    - HTTP forward proxy (egress enforcement)                       │
│    - gRPC service for SDK communication                            │
│  Packaged as a K8s sidecar via Helm chart.                         │
└────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌────────────────────────────────────────────────────────────────────┐
│  Layer 1 — substrate                                               │
│  - K8s NetworkPolicy / Cilium                                      │
│  - Container isolation (gVisor, Firecracker)                       │
│  - Per-pod identity (SPIRE, kube ServiceAccount)                   │
│  aegrail does not implement this layer; it composes with it.       │
└────────────────────────────────────────────────────────────────────┘
```

## What lives where, today and tomorrow

| Component | State today | Repo | Long-term |
|---|---|---|---|
| Python library `aegrail` | Shipped to PyPI (v0.2.3) | `arpitcoder/aegrail` | Becomes the Python SDK on Layer 3. Public API stays stable. |
| Tool ACL, Budget, AuditEvent, identity, chain verification | Implemented in Python | `arpitcoder/aegrail` | Re-implemented in Go inside the engine; Python SDK becomes a thin client. |
| Audit log format (JSONL + SHA-256 chain) | Defined in Python | `arpitcoder/aegrail` | Becomes the canonical wire format. Any language can produce and verify these. |
| OWASP / SOC 2 / ISO 27001 / NIST mapping | `COMPLIANCE.md` in Python repo | `arpitcoder/aegrail` | Stays here as the canonical mapping document; engine inherits. |
| Egress allowlist enforcement | Not yet shipped | `arpitcoder/aegrail-engine` | Go sidecar; v0.3.0 milestone |
| Helm chart for K8s deployment | Not yet shipped | `arpitcoder/aegrail-engine` | Ships with v0.3.0 |
| Python-level interceptors (`intercept_outbound`, audit hook) | Not yet shipped | `arpitcoder/aegrail` | v0.2.4 milestone — fills the in-Python defense-in-depth slot above the sidecar |
| Node / Go / JVM SDKs | Not started | TBD | Built as thin clients once the engine wire protocol stabilises |

## Sequencing

The work is sequenced so each step ships visible value, builds on the
previous, and doesn't lock in decisions before they're informed by
real usage.

```
v0.2.x  Python library — adoption polish (current state)
        - tool ACL, audit chain, COMPLIANCE.md (shipped)
        - v0.2.4: Python interceptors (next)
        - v0.2.5: aegrail-lint (CI-time enforcement)

v0.3.0  Go sidecar — egress proxy + Helm chart
        - lives in arpitcoder/aegrail-engine
        - first multi-language step: enforcement no longer
          requires the agent to be in Python
        - Helm chart for K8s sidecar injection
        - same audit log format as the Python library

v0.4.x  Engine maturity
        - approval gates for irreversible actions
        - gRPC service exposing the policy API to SDKs
        - aegrail-py 1.0: refactored to be a thin client of the
          engine, optional (can still run library-only mode)

v1.0    Multi-language SDKs
        - aegrail-js, aegrail-go, aegrail-jvm thin clients
        - hosted control plane (paid product)
        - CNCF Sandbox submission once 3+ contributors / 3+
          production adopters
```

## Cross-language compatibility today, without the engine

Even before the engine ships, the audit log format is **language-
independent** by design:

- JSONL on disk, no Python-specific encoding
- SHA-256 chain link in every event — verifiable in any language with
  a SHA-256 implementation
- Documented in `COMPLIANCE.md` and the per-event JSON shape in the
  README

A Node / Go / Rust consumer can already **read aegrail audit logs and
verify their integrity** without using Python. The gap that the
engine closes is the *write* side: producing those logs (and
enforcing the ACL while doing so) from non-Python agents.

## Defense-in-depth, restated

aegrail occupies one layer. It composes with the layers below it; it
does not replace them. Repeating the boundary explicitly:

| Layer | What aegrail does | What's the right tool below it |
|---|---|---|
| L7 — agent capability | aegrail tool ACL, audit, identity | — |
| L7 — agent in-process bypass detection | aegrail Python interceptors (v0.2.4) | — |
| L4–L7 — egress enforcement | aegrail-engine HTTP forward proxy (v0.3.0) | — |
| L3–L4 — network egress | _not aegrail_ | NetworkPolicy, Cilium, eBPF |
| Process / kernel — syscall filtering | _not aegrail_ | seccomp-bpf, gVisor, Firecracker |
| Identity infrastructure | _not aegrail_ | SPIRE, kube ServiceAccount, Vault |

The Python interceptor work in v0.2.4 and the Go sidecar work in
v0.3.0 are both about the upper two layers. The substrate stays
outside aegrail's scope.

## Why Go (not Rust) for the engine

A choice we made deliberately. Reasoning in three points:

1. **Solo-maintainer productivity.** Aegrail is a side project at
   10–20 hrs/week. Rust adds 30–50% complexity tax per feature vs Go.
   That tax compounds across years of maintenance.
2. **CNCF / K8s ecosystem fit.** Kubernetes, Helm, cert-manager,
   Falco, OPA, Vault — all Go. The toolchain (kubebuilder, Helm
   templating, controller-runtime) assumes Go.
3. **Performance is sufficient.** A Go HTTP forward proxy easily
   sustains 100k req/s on a single core. aegrail's enforcement
   workload (allowlist match + audit append) is well within Go's
   performance envelope. Rust's speed advantage doesn't pay back
   the complexity cost at this scale.

If the engine ever needs to embed inside a process (FFI, WASM), Rust
becomes the right call for that specific case. We'll cross that
bridge when a real use case demands it.

## Where this document lives

- `arpitcoder/aegrail` → this file (overall architecture, both layers)
- `arpitcoder/aegrail-engine` → engine-specific design + deployment
  notes (lives there because that's where the engine code is)
- `COMPLIANCE.md` in `arpitcoder/aegrail` → control mappings, shared

Updates to architectural decisions land in this file by PR. The
roadmap-discipline rules in `CLAUDE.md` govern when structural work
proceeds.
