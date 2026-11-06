import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from task1_mcp_server.models import GetCustomerRecordArgs, TriggerRefundArgs

REPO_ROOT = Path(__file__).resolve().parent.parent

INIT = [
    {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
    },
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
]

VALID_REFUND = {"customer_id": "CUST-10001", "amount": 25.5, "reason": "Damaged item on arrival"}


# ---------- schema validation ----------

@pytest.mark.parametrize("customer_id", ["CUST-10001", "CUST-00000", "CUST-99999"])
def test_customer_id_accepts_valid(customer_id):
    GetCustomerRecordArgs.model_validate({"customer_id": customer_id})


@pytest.mark.parametrize(
    "customer_id",
    [
        "CUST-1234",       # too short
        "CUST-123456",     # too long
        "cust-12345",      # wrong case
        "CUST12345",       # missing dash
        " CUST-12345",     # leading space
        "CUST-12345\n",    # trailing newline
        "CUST-1234a",      # letter
        "CUST-١٢٣٤٥",      # non-ASCII digits
        "",
        12345,             # not a string
        None,
    ],
)
def test_customer_id_rejects_invalid(customer_id):
    with pytest.raises(ValidationError):
        GetCustomerRecordArgs.model_validate({"customer_id": customer_id})


def test_refund_accepts_int_amount():
    # JSON has one number type, so 10 and 10.0 are both valid amounts.
    TriggerRefundArgs.model_validate({**VALID_REFUND, "amount": 10})


@pytest.mark.parametrize(
    "amount",
    [0, -5, -0.01, "10", True, float("nan"), float("inf"), 10_000.01, 10.005, None],
)
def test_refund_rejects_bad_amount(amount):
    with pytest.raises(ValidationError):
        TriggerRefundArgs.model_validate({**VALID_REFUND, "amount": amount})


@pytest.mark.parametrize("reason", ["short", "123456789", "          ", "   abc    ", "x" * 501, 1234567890])
def test_refund_rejects_bad_reason(reason):
    with pytest.raises(ValidationError):
        TriggerRefundArgs.model_validate({**VALID_REFUND, "reason": reason})


def test_reason_is_stripped():
    args = TriggerRefundArgs.model_validate({**VALID_REFUND, "reason": "  exactly10!  "})
    assert args.reason == "exactly10!"


def test_extra_fields_rejected():
    with pytest.raises(ValidationError):
        GetCustomerRecordArgs.model_validate({"customer_id": "CUST-10001", "admin": True})


def test_missing_fields_rejected():
    with pytest.raises(ValidationError):
        TriggerRefundArgs.model_validate({"customer_id": "CUST-10001"})


# ---------- end to end over stdio ----------

def _expected_replies(lines: list[str]) -> int:
    # One reply per request (has an id) and per malformed line; none for notifications.
    count = 0
    for line in lines:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            count += 1
            continue
        if not isinstance(msg, dict) or "method" not in msg or "id" in msg:
            count += 1
    return count


def run_server(
    lines: list[str], argv: list[str] | None = None, env: dict[str, str] | None = None
) -> tuple[list[dict], str]:
    """Send raw lines to a fresh server process; return parsed stdout frames and stderr.

    stdin stays open until every expected reply has arrived. Closing it is the
    stdio shutdown signal, and closing early would cancel requests in flight.
    """
    proc = subprocess.Popen(
        argv or [sys.executable, "-m", "task1_mcp_server.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )
    stdout_lines: queue.Queue[str] = queue.Queue()
    reader = threading.Thread(target=lambda: [stdout_lines.put(l) for l in proc.stdout], daemon=True)
    reader.start()

    proc.stdin.write("\n".join(lines) + "\n")
    proc.stdin.flush()

    frames = []
    deadline = time.monotonic() + 15
    while len(frames) < _expected_replies(lines):
        line = stdout_lines.get(timeout=max(0.1, deadline - time.monotonic()))
        frame = json.loads(line)  # any non-JSON line on stdout fails the test here
        assert frame.get("jsonrpc") == "2.0", line
        assert "result" in frame or "error" in frame, line
        frames.append(frame)

    proc.stdin.close()
    proc.wait(timeout=10)
    reader.join(timeout=5)
    stderr = proc.stderr.read()
    if not stdout_lines.empty():
        pytest.fail(f"unexpected extra stdout line: {stdout_lines.get()!r}")
    return frames, stderr


def call(req_id: int, name: str, arguments) -> str:
    return json.dumps(
        {"jsonrpc": "2.0", "id": req_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    )


def by_id(frames: list[dict]) -> dict:
    return {f["id"]: f for f in frames}


def session(*requests: str) -> list[str]:
    return [json.dumps(m) for m in INIT] + list(requests)


def test_stdout_is_pure_jsonrpc_and_logs_go_to_stderr():
    frames, stderr = run_server(
        session(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
            call(2, "get_customer_record", {"customer_id": "bad"}),
            "this is not json",
        )
    )
    assert len(frames) == 4  # initialize, tools/list, two errors
    assert "starting customer-support MCP server" in stderr
    assert "rejected get_customer_record" in stderr


def test_tools_list_publishes_strict_schemas():
    frames, _ = run_server(session(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})))
    tools = {t["name"]: t for t in by_id(frames)[1]["result"]["tools"]}
    assert set(tools) == {"get_customer_record", "trigger_refund"}
    refund = tools["trigger_refund"]["inputSchema"]
    assert refund["additionalProperties"] is False
    assert set(refund["required"]) == {"customer_id", "amount", "reason"}
    assert refund["properties"]["reason"]["minLength"] == 10
    assert refund["properties"]["amount"]["exclusiveMinimum"] == 0


def test_valid_calls_succeed():
    frames, _ = run_server(
        session(
            call(1, "get_customer_record", {"customer_id": "CUST-10001"}),
            call(2, "trigger_refund", VALID_REFUND),
        )
    )
    responses = by_id(frames)
    record = json.loads(responses[1]["result"]["content"][0]["text"])
    assert record["customer_id"] == "CUST-10001"
    receipt = json.loads(responses[2]["result"]["content"][0]["text"])
    assert receipt["amount"] == 25.5
    assert receipt["remaining_refundable_balance"] == 474.5


def test_invalid_arguments_return_invalid_params():
    frames, _ = run_server(
        session(
            call(1, "get_customer_record", {"customer_id": "CUST-1"}),
            call(2, "trigger_refund", {"customer_id": "CUST-10001", "amount": "10", "reason": "short"}),
            call(3, "trigger_refund", None),
        )
    )
    responses = by_id(frames)
    for req_id in (1, 2, 3):
        assert responses[req_id]["error"]["code"] == -32602
    fields = {e["field"] for e in responses[2]["error"]["data"]["errors"]}
    assert fields == {"amount", "reason"}


def test_validation_errors_do_not_echo_input():
    secret = "CUST-1<script>"
    frames, _ = run_server(session(call(1, "get_customer_record", {"customer_id": secret})))
    assert secret not in json.dumps(frames)


def test_unknown_tool_returns_invalid_params():
    frames, _ = run_server(session(call(1, "delete_everything", {})))
    assert by_id(frames)[1]["error"]["code"] == -32602


def test_unknown_method_returns_method_not_found():
    frames, _ = run_server(session(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/explode"})))
    assert by_id(frames)[1]["error"]["code"] == -32601


def test_business_failures_are_tool_errors_not_protocol_errors():
    frames, _ = run_server(
        session(
            call(1, "get_customer_record", {"customer_id": "CUST-99999"}),
            call(2, "trigger_refund", {**VALID_REFUND, "amount": 9999}),
        )
    )
    responses = by_id(frames)
    for req_id in (1, 2):
        assert "error" not in responses[req_id]
        assert responses[req_id]["result"]["isError"] is True


def test_malformed_lines_get_errors_and_server_keeps_serving():
    frames, _ = run_server(
        session(
            "{not json",
            json.dumps({"hello": "world"}),
            call(1, "get_customer_record", {"customer_id": "CUST-10001"}),
        )
    )
    codes = [f["error"]["code"] for f in frames if f.get("id") is None]
    assert codes == [-32700, -32600]
    assert by_id(frames)[1]["result"]["isError"] is False


def test_stray_print_in_handler_does_not_reach_stdout():
    # Simulates a careless print() inside tool code. The print must land on
    # stderr and the JSON-RPC stream must stay clean. Run with output buffering
    # on (the default when stdout is a pipe): that is the case that leaks if the
    # server relies only on the SDK's fd 1 diversion.
    script = (
        "import anyio, task1_mcp_server.server as s\n"
        "orig = s.store.get\n"
        "def noisy(cid):\n"
        "    print('debug: looking up', cid)\n"
        "    return orig(cid)\n"
        "s.store.get = noisy\n"
        "anyio.run(s.main)\n"
    )
    frames, stderr = run_server(
        session(call(1, "get_customer_record", {"customer_id": "CUST-10001"})),
        argv=[sys.executable, "-c", script],
        env={k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"},
    )
    assert by_id(frames)[1]["result"]["isError"] is False
    assert "debug: looking up CUST-10001" in stderr
