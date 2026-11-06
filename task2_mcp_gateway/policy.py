"""JSON-RPC parsing and tool authorization. No I/O, so every rule is unit-testable."""

import json
import unicodedata
from dataclasses import dataclass
from typing import Any

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNAUTHENTICATED = -32000
UNAUTHORIZED_TOOL = -32001

ADMIN_TOOL_PREFIX = "admin_"


class RpcError(Exception):
    def __init__(self, code: int, message: str, request_id: Any = None, http_status: int = 200):
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = request_id
        self.http_status = http_status

    def body(self) -> dict:
        return {"jsonrpc": "2.0", "id": self.request_id, "error": {"code": self.code, "message": self.message}}


@dataclass(frozen=True)
class Message:
    raw: dict
    method: str | None
    request_id: Any
    params: dict | None

    @property
    def is_request(self) -> bool:
        return self.method is not None and "id" in self.raw

    @property
    def tool_name(self) -> str | None:
        if self.method == "tools/call" and self.params is not None:
            name = self.params.get("name")
            return name if isinstance(name, str) else None
        return None


class _DuplicateKey(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    # json.loads keeps the last value of a repeated key; other parsers keep the
    # first. {"name": "get_x", "name": "admin_x"} would then be checked as one
    # tool and run as another, so repeated keys are refused outright.
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise _DuplicateKey(key)
        obj[key] = value
    return obj


def _reject_constant(token: str):
    raise ValueError(f"{token} is not valid JSON")


def _valid_id(value: Any) -> bool:
    # MCP request ids are strings or integers. bool is a subclass of int in
    # Python, so it is excluded explicitly.
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


def parse_message(body: bytes) -> Message:
    try:
        msg = json.loads(body, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)
    except _DuplicateKey:
        raise RpcError(INVALID_REQUEST, "Invalid Request: duplicate keys", http_status=400) from None
    except (ValueError, UnicodeDecodeError):
        raise RpcError(PARSE_ERROR, "Parse error", http_status=400) from None

    if isinstance(msg, list):
        # MCP dropped JSON-RPC batching in protocol version 2025-06-18. Refusing
        # batches also means there is exactly one message to authorize.
        raise RpcError(INVALID_REQUEST, "Invalid Request: batch requests are not supported", http_status=400)
    if not isinstance(msg, dict):
        raise RpcError(INVALID_REQUEST, "Invalid Request", http_status=400)

    request_id = msg.get("id") if _valid_id(msg.get("id")) else None

    def invalid(reason: str) -> RpcError:
        return RpcError(INVALID_REQUEST, f"Invalid Request: {reason}", request_id, http_status=400)

    if msg.get("jsonrpc") != "2.0":
        raise invalid('"jsonrpc" must be "2.0"')
    if "id" in msg and not _valid_id(msg["id"]):
        raise invalid('"id" must be a string or integer')

    method = msg.get("method")
    if method is None:
        # A client's reply to a server-initiated request (e.g. sampling).
        if "id" not in msg or ("result" in msg) == ("error" in msg):
            raise invalid("a response needs an id and exactly one of result or error")
        return Message(raw=msg, method=None, request_id=request_id, params=None)

    if not isinstance(method, str):
        raise invalid('"method" must be a string')
    params = msg.get("params")
    if params is not None and not isinstance(params, dict):
        raise invalid('"params" must be an object')
    if method == "tools/call" and "id" not in msg:
        raise invalid("tools/call must be a request with an id")

    return Message(raw=msg, method=method, request_id=request_id, params=params)


def is_admin_tool(name: str) -> bool:
    # Fail closed on look-alike names. A downstream server that matches names
    # case-insensitively or after Unicode normalization would otherwise run
    # "Admin_reset_key" or a fullwidth "ａｄｍｉｎ_reset_key" for a viewer.
    normalized = unicodedata.normalize("NFKC", name)
    normalized = "".join(ch for ch in normalized if unicodedata.category(ch) != "Cf")  # zero-width chars
    return normalized.strip().casefold().startswith(ADMIN_TOOL_PREFIX)


def authorize(message: Message, role: str) -> None:
    """Raise RpcError if `role` may not send `message`. Everything except tools/call passes."""
    if message.method != "tools/call":
        return
    name = message.tool_name
    if name is None:
        raise RpcError(INVALID_PARAMS, "Invalid params: tools/call requires a string params.name", message.request_id)
    if is_admin_tool(name) and role != "admin":
        raise RpcError(UNAUTHORIZED_TOOL, "Unauthorized Tool Call", message.request_id)
