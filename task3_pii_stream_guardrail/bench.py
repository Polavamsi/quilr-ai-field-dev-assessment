"""Measure what the guardrail costs: time to first token and total stream time.

Starts the mock LLM and the guardrail with uvicorn, then streams the same reply
directly from the mock and through the guardrail.

    uv run python -m task3_pii_stream_guardrail.bench
"""

import json
import os
import statistics
import subprocess
import sys
import time

import httpx

MOCK_PORT, GUARD_PORT = 9201, 8100
RUNS = 20
REQUEST = {"model": "mock", "stream": True, "messages": [{"role": "user", "content": "account details?"}]}


def wait_until_up(url: str, timeout_s: float = 15) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            httpx.get(url, timeout=0.5)
            return
        except httpx.HTTPError:
            time.sleep(0.1)
    raise RuntimeError(f"{url} did not start")


def measure(url: str) -> tuple[float, float, str]:
    """Return (seconds to first content, seconds to end of stream, full text)."""
    text, first = "", None
    start = time.perf_counter()
    with httpx.stream("POST", url, json=REQUEST, timeout=30) as resp:
        for line in resp.iter_lines():
            if not line.startswith("data: {"):
                continue
            for choice in json.loads(line[6:]).get("choices", []):
                piece = choice.get("delta", {}).get("content")
                if piece:
                    first = first if first is not None else time.perf_counter() - start
                    text += piece
    return first, time.perf_counter() - start, text


def serve(app: str, port: int, env: dict, factory: bool = False) -> subprocess.Popen:
    cmd = [sys.executable, "-m", "uvicorn", app, "--port", str(port), "--log-level", "warning"]
    if factory:
        cmd.append("--factory")
    return subprocess.Popen(cmd, env={**os.environ, **env}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> None:
    mock = serve("task3_pii_stream_guardrail.mock_llm:app", MOCK_PORT, {"MOCK_CHUNK_DELAY_MS": "20"})
    guard = serve(
        "task3_pii_stream_guardrail.app:create_app_from_env", GUARD_PORT,
        {"UPSTREAM_URL": f"http://127.0.0.1:{MOCK_PORT}/v1/chat/completions"}, factory=True,
    )
    try:
        wait_until_up(f"http://127.0.0.1:{GUARD_PORT}/healthz")
        wait_until_up(f"http://127.0.0.1:{MOCK_PORT}/docs")
        direct_url = f"http://127.0.0.1:{MOCK_PORT}/v1/chat/completions"
        guard_url = f"http://127.0.0.1:{GUARD_PORT}/v1/chat/completions"
        measure(direct_url), measure(guard_url)  # warm up both

        results = {"direct": [], "guardrail": []}
        for _ in range(RUNS):
            results["direct"].append(measure(direct_url))
            results["guardrail"].append(measure(guard_url))

        print(f"{RUNS} runs, mock LLM sends 1-6 characters every 20 ms\n")
        print(f"{'':12}{'TTFT median':>14}{'TTFT p95':>12}{'total median':>15}")
        for name, rows in results.items():
            ttft = sorted(r[0] * 1000 for r in rows)
            total = [r[1] * 1000 for r in rows]
            p95 = ttft[int(0.95 * (len(ttft) - 1))]
            print(f"{name:12}{statistics.median(ttft):>11.1f} ms{p95:>9.1f} ms{statistics.median(total):>12.1f} ms")

        sample = results["guardrail"][0][2]
        print(f"\nredacted output: {sample}")
    finally:
        for proc in (guard, mock):
            proc.terminate()
            proc.wait()


if __name__ == "__main__":
    main()
