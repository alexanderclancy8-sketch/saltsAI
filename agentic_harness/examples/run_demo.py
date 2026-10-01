"""Runnable end-to-end demo (needs a real ANTHROPIC_API_KEY in the environment -
this is the only script in the package that makes a real network call).

    python -m agentic_harness.examples.run_demo

Seeds the mock filesystem with a config file, asks the harness to bump a timeout
value in it, and lets you watch: the orchestrator plans, a worker reads the file
(no approval needed), then the write hits the human-in-the-loop gate and pauses
for a y/N in your terminal before it's applied.
"""

from __future__ import annotations

from agentic_harness.harness import AgenticHarness, MockFS

CONFIG = '{\n  "timeout_seconds": 10,\n  "retries": 3\n}\n'


def main() -> None:
    fs = MockFS({"config.json": CONFIG})
    harness = AgenticHarness(fs=fs)

    result = harness.run("Read config.json and change timeout_seconds to 30, then write the file back.")

    print("\n=== subtasks ===")
    for s in result.subtasks:
        print("-", s)

    print("\n=== worker results ===")
    for r in result.worker_results:
        print(f"[{'ok' if r.success else 'FAILED'}] {r.task} ({r.turns_used} turns, "
              f"{len(r.tool_calls)} tool calls, {len(r.denied_calls)} denied)")
        print("  ", r.final_text.replace("\n", "\n   "))

    print("\n=== final summary ===")
    print(result.final_summary)

    print("\n=== resulting file ===")
    print(fs.files.get("config.json", "(unchanged / not written)"))


if __name__ == "__main__":
    main()
