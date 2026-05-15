"""Sample agent for the kind integration test.

Runs once inside a Kubernetes pod and validates that aegrail's
in-process interceptors enforce the egress allowlist when
`AEGRAIL_INTERCEPT=1` is set via pod env (no developer code change
required — that's the load-bearing property).

Exits 0 on PASS, non-zero on FAIL. Prints a clear PASS / FAIL line so
the test orchestrator can grep for it.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

from aegrail import Agent, AuditSink, Budget, EgressNotPermitted
from aegrail.audit import verify_chain


def main() -> int:
    # Sanity check: the deployment platform set AEGRAIL_INTERCEPT=1
    # via the pod env. This is the developer-effortless deployment
    # discipline we're testing.
    if os.environ.get("AEGRAIL_INTERCEPT") != "1":
        print("FAIL: AEGRAIL_INTERCEPT env var not set on the pod")
        return 1

    sink = AuditSink.memory()
    agent = Agent(
        identity="kind-test-agent/v1",
        budget=Budget(usd=1.0, max_tool_calls=10),
        audit=sink,
        egress_allowlist=["allowed.example"],
    )

    with agent.session(user_id="kind-test"):
        # Test 1: denied destination must raise EgressNotPermitted
        try:
            urllib.request.urlopen("http://denied.example/", timeout=1)
        except EgressNotPermitted as exc:
            print(f"OK: denied.example -> EgressNotPermitted (host={exc.host})")
        except urllib.error.URLError as exc:
            print(
                "FAIL: denied.example should have been blocked at the boundary; "
                f"got URLError instead: {exc}"
            )
            return 1
        except Exception as exc:
            print(f"FAIL: unexpected exception type {type(exc).__name__}: {exc}")
            return 1
        else:
            print("FAIL: denied.example should have raised; got success")
            return 1

        # Test 2: allowed destination must pass the egress check (will
        # fail at the network layer because the host doesn't resolve,
        # but that's URLError, not EgressNotPermitted — the point is the
        # check let it through)
        try:
            urllib.request.urlopen("http://allowed.example/", timeout=1)
        except EgressNotPermitted as exc:
            print(f"FAIL: allowed.example should have passed the egress check; got {exc}")
            return 1
        except urllib.error.URLError:
            print(
                "OK: allowed.example -> URLError "
                "(passed egress check, failed at network as expected)"
            )
        except Exception as exc:
            print(f"FAIL: unexpected exception type {type(exc).__name__}: {exc}")
            return 1

    # Verify exactly one egress_denied event in the audit log
    denied = [e for e in sink.events if e.event == "egress_denied"]
    if len(denied) != 1:
        print(f"FAIL: expected 1 egress_denied event, got {len(denied)}")
        return 1
    if denied[0].payload["host"] != "denied.example":
        print(f"FAIL: expected denied.example in event, got {denied[0].payload['host']!r}")
        return 1

    # Verify the audit chain validates end-to-end
    valid, bad_index = verify_chain(sink.events)
    if not valid:
        print(f"FAIL: audit chain broken at event index {bad_index}")
        return 1

    print(f"OK: audit chain validates ({len(sink.events)} events, all linked)")
    print("PASS - aegrail v0.2.4 interceptors enforce egress allowlist in kind cluster")
    return 0


if __name__ == "__main__":
    sys.exit(main())
