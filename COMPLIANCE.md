# Compliance — what aegrail emits, and what auditors actually need

aegrail does **not** make your organisation SOC 2, ISO 27001, or HIPAA
compliant. Compliance is an organisational property; no library can confer
it. What aegrail does is emit the *artifacts* an external auditor examines
when testing specific controls related to AI agent activity — in the
language those auditors use, on every session, by default, with no
post-hoc instrumentation.

This document maps aegrail's emitted events to the controls auditors test
against, with concrete extraction recipes for evidence packages.

---

## Tamper-evidence (the prerequisite)

Starting with v0.2.3, every emitted `AuditEvent` carries two fields:

- `prev_hash` — the SHA-256 of the previous event in the chain, or
  `null` for the genesis event
- `event_hash` — the SHA-256 of this event's serialized body
  (including its `prev_hash`)

Any post-hoc edit to a historical event invalidates the chain from that
point forward. Use `aegrail.audit.verify_chain(events)` to confirm a
log has not been tampered with:

```python
from aegrail import AuditEvent
from aegrail.audit import verify_chain
import json

with open("audit.jsonl") as f:
    events = [AuditEvent(**json.loads(line)) for line in f if line.strip()]

valid, first_bad_index = verify_chain(events)
if not valid:
    raise RuntimeError(f"audit chain broken at index {first_bad_index}")
```

The chain spans process restarts: `FileAuditSink` reads the last line on
open and continues from that event's hash. The same file written by many
agent sessions over weeks remains a single verifiable chain.

---

## SOC 2 Type II — Trust Services Criteria mapping

The five Trust Services Criteria categories — Security (Common Criteria),
Availability, Processing Integrity, Confidentiality, Privacy — include
many controls that auditors test by sampling artifacts from production
systems. The table below maps each criterion aegrail meaningfully
supports to the event(s) that constitute the evidence.

| Control | What auditors look for | aegrail event(s) that provide the evidence |
|---|---|---|
| **CC6.1** — Logical access controls | Records of *who* accessed *what* and *when* | All events: `ts`, `agent_identity`, `invoking_user`, `principal` |
| **CC6.2** — User authorization | Recorded user identity tied to each privileged action | `tool_call`, `tool_denied`, `llm_call` all carry `invoking_user` |
| **CC6.3** — Role-based access | Authorization decisions logged with rationale | `tool_denied` events with `reason ∈ {not_registered, predicate_false, predicate_error}` |
| **CC7.1** — Detection of unauthorised changes | Append-only, tamper-evident record | Chained `event_hash` / `prev_hash`; `verify_chain()` |
| **CC7.2** — Security event monitoring | Structured event stream that can feed a SIEM | JSONL output via `AuditSink.file` / `webhook` / `composite` |
| **CC7.3** — Incident response evidence | Forensic reconstruction of an incident timeline | Full event timeline per `session_id`, principal-linked |
| **CC9.2** — Operational risk mitigation | Records of automated controls firing | `budget_exceeded` events with `reason` |
| **PI1.1** — Processing integrity: input validation | Records that inputs were validated before processing | `tool_denied(reason='predicate_false')` with arg redaction |
| **C1.1** — Confidentiality of data | Evidence of redaction policies applied | `tool_call.args` keys-only by default; per-tool `redact()` for richer cases |

### Evidence extraction recipes

These `jq` recipes pull the artifacts an auditor will ask for, from a
JSONL audit log.

**All actions taken by a specific user during the audit window:**

```bash
jq -c 'select(.invoking_user == "alice" and .ts >= "2026-01-01" and .ts < "2026-04-01")' audit.jsonl
```

**All policy denials and their reasons** (CC6.3 evidence):

```bash
jq -c 'select(.event == "tool_denied") | {ts, principal, tool: .payload.tool, reason: .payload.reason}' audit.jsonl
```

**All budget kill-switch events** (CC9.2 evidence):

```bash
jq -c 'select(.event == "budget_exceeded") | {ts, principal, reason: .payload.reason}' audit.jsonl
```

**Reconstruct a single session's full timeline** (CC7.3 evidence):

```bash
jq -c 'select(.session_id == "sess_1778638800000_4bf0a4f8cf1c")' audit.jsonl
```

**Daily count of tool denials, by reason** (control-effectiveness reporting):

```bash
jq -r 'select(.event == "tool_denied") | [.ts[:10], .payload.reason] | @tsv' audit.jsonl \
  | sort | uniq -c
```

---

## ISO 27001:2022 mapping

ISO 27001 Annex A controls relevant to AI-agent workloads:

| Control | What's tested | aegrail evidence |
|---|---|---|
| **A.5.15** — Access control | Documented, enforced access restrictions | Per-agent tool ACL declared at `Agent` construction |
| **A.5.16** — Identity management | Unique identities, lifecycle | Per-session principals `<agent>@<sess>`, validated identity format |
| **A.5.17** — Authentication info | No shared credentials | Sessions never reuse identity strings across runs |
| **A.5.18** — Access rights | Authorization rights granted by role | `Tool.when(args)` predicate gates per-call authorisation |
| **A.8.15** — Logging | Adequate logs of user activities | `tool_call` + `tool_denied` + `llm_call` events |
| **A.8.16** — Monitoring activities | Anomalous activities detected | `budget_exceeded` and `tool_denied` are first-class events |
| **A.8.24** — Use of cryptography | Cryptographic controls implemented | SHA-256 chain on every audit event |

---

## NIST SP 800-53 (informational)

NIST SP 800-53 controls overlap meaningfully with SOC 2 CC. The most
directly applicable to aegrail:

| Control | aegrail evidence |
|---|---|
| **AC-2** (Account management) | Per-session principals |
| **AC-3** (Access enforcement) | Tool ACL with `ToolNotPermitted` |
| **AC-6** (Least privilege) | Each agent has only the tools it needs |
| **AU-2** (Audit events) | Full event taxonomy: session_start, session_end, llm_call, tool_call, tool_denied, budget_exceeded |
| **AU-3** (Audit record content) | Identity, time, action, outcome on every record |
| **AU-9** (Protection of audit information) | Tamper-evident chain via `event_hash` |
| **AU-12** (Audit generation) | Structured JSONL, append-only |
| **SI-4** (System monitoring) | Composite sinks for SIEM/webhook routing |

The forthcoming **NIST AI 800 series** for AI agents — including the
COSAiS SP 800-53 control overlays expected late 2026 to 2027 — is the
strategic direction aegrail is tracking. When those overlays publish,
this document will be updated to map them directly.

---

## OWASP Top 10 for Agentic Applications

Already cited in the README, repeated here for completeness:

| Risk | aegrail control |
|---|---|
| **ASI02** — Tool Misuse | Tool registry + `when(args)` predicate; denials surface as `tool_denied(reason='predicate_false')` |
| **ASI03** — Identity & Privilege Abuse | Tool registry bound to `Agent.identity`; cross-agent calls denied with `reason='not_registered'` |

---

## What aegrail does *not* claim

To be clear about scope:

- aegrail does **not** authenticate the `invoking_user` it records. That
  identity is provided by the caller — your application's auth layer is
  responsible for verifying it before invoking aegrail.
- aegrail does **not** ship a SIEM, dashboard, or alert manager. It emits
  the events; you ship them to Splunk / Datadog / ClickHouse / S3 via
  the audit sinks.
- aegrail does **not** enforce retention. Operators are responsible for
  retaining audit logs per their applicable compliance regime (typically
  1 year for SOC 2; longer for some HIPAA scenarios).
- aegrail does **not** ship encryption-at-rest for the audit file. Use
  filesystem-level encryption (LUKS, FileVault, EBS encryption) or write
  the JSONL to an encrypted-at-rest store via a custom sink.
- aegrail is **not** a substitute for network egress controls (CNI /
  NetworkPolicy / Cilium) or process isolation (containers / gVisor).
  See the README "defense-in-depth" section.

For ASI01 (Agent Goal Hijack) — prompt-injection of the LLM itself —
aegrail provides *runtime mitigation* (the injected action is denied at
the boundary) but does not *prevent* the injection from reaching the
model. Use a prompt-injection detection layer (Lakera, Prompt Security,
others) upstream of aegrail for defense-in-depth.

---

## How to use this document in an audit

1. **Map the controls your auditor cares about** to the table above.
2. **Run the corresponding `jq` extraction recipe** on your audit log
   for the audit window.
3. **Verify chain integrity** on the extracted log:
   ```python
   from aegrail.audit import verify_chain
   from aegrail import AuditEvent
   import json
   events = [AuditEvent(**json.loads(line)) for line in open("audit.jsonl")]
   valid, idx = verify_chain(events)
   assert valid, f"chain broken at event {idx}"
   ```
4. **Hand the extracted JSONL + the verification result** to the auditor
   as evidence. The chain hash proves the log has not been edited since
   it was written.

If an auditor asks for control evidence aegrail does not provide today,
open an issue at https://github.com/arpitcoder/aegrail/issues — that's
the direct path to influencing the next release's scope.
