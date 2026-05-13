"""Measure aegrail's deterministic-enforcement overhead.

The Murati-era question: does the runtime check fit inside a 200ms
full-duplex chunk? Answer below, in microseconds.

The same enforcement boundary runs whether tool calls arrive over
batch JSON, SSE, WebSockets, or a Realtime stream. This benchmark
measures the Python-side cost.
"""

from __future__ import annotations

import statistics
import time

from aegrail import Agent, AuditSink, Budget


def noop() -> None:
    return None


def main() -> None:
    agent = Agent(
        identity="realtime-bot/v1",
        budget=Budget(usd=10.0, wall_seconds=60, max_tool_calls=100_000),
        audit=AuditSink.memory(),
    )

    samples_record_llm: list[int] = []
    samples_call_tool: list[int] = []
    samples_check: list[int] = []

    with agent.session(user_id="bench", task="latency") as s:
        # Warm up JIT-y bits, allocator, etc.
        for _ in range(100):
            s.record_llm(model="m", tokens_in=1, tokens_out=1, cost_usd=0.00001)

        for _ in range(2000):
            t0 = time.perf_counter_ns()
            s.record_llm(model="m", tokens_in=1, tokens_out=1, cost_usd=0.00001)
            samples_record_llm.append(time.perf_counter_ns() - t0)

        for _ in range(2000):
            t0 = time.perf_counter_ns()
            s.call_tool("noop", noop)
            samples_call_tool.append(time.perf_counter_ns() - t0)

        for _ in range(2000):
            t0 = time.perf_counter_ns()
            s.check_budget()
            samples_check.append(time.perf_counter_ns() - t0)

    def report(name: str, samples_ns: list[int]) -> None:
        samples_us = [n / 1000.0 for n in samples_ns]
        sorted_us = sorted(samples_us)
        p50 = statistics.median(samples_us)
        p99 = sorted_us[int(len(samples_us) * 0.99) - 1]
        p999 = sorted_us[int(len(samples_us) * 0.999) - 1]
        print(f"  {name:20s} p50={p50:7.2f}us  p99={p99:7.2f}us  p99.9={p999:7.2f}us")

    print("aegrail deterministic-enforcement latency (n=2000 per row):")
    print()
    report("check_budget()", samples_check)
    report("call_tool(noop)", samples_call_tool)
    report("record_llm()", samples_record_llm)
    print()
    median_tool_us = statistics.median([n / 1000 for n in samples_call_tool])
    p999_tool_us = sorted([n / 1000 for n in samples_call_tool])[int(2000 * 0.999) - 1]
    chunk_us = 200_000
    print(f"realtime chunk size:  {chunk_us:>8,d}us  (200ms)")
    print(f"call_tool p50:        {median_tool_us:>8.2f}us")
    print(f"call_tool p99.9:      {p999_tool_us:>8.2f}us")
    print(f"headroom p50:         ~{chunk_us / median_tool_us:>8,.0f}x faster than one chunk")
    print(f"headroom p99.9:       ~{chunk_us / p999_tool_us:>8,.0f}x faster (worst-case)")


if __name__ == "__main__":
    main()
