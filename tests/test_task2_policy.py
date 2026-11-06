import json

import pytest

from task2_mcp_gateway.policy import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    PARSE_ERROR,
    UNAUTHORIZED_TOOL,
    RpcError,
    authorize,
    is_admin_tool,
    parse_message,
)


def call(name, req_id=1):
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "method": "tools/call", "params": {"name": name}}).encode()


@pytest.mark.parametrize(
    "name",
    [
        "admin_reset_key",
        "Admin_reset_key",
        "ADMIN_RESET_KEY",
        " admin_reset_key",
        "ａｄｍｉｎ_reset_key",        # fullwidth letters
        "adm​in_reset_key",      # zero-width space
    ],
)
def test_admin_tools_detected(name):
    assert is_admin_tool(name)


@pytest.mark.parametrize("name", ["get_customer_record", "administrator", "get_admin_key", "reset_admin_key", ""])
def test_non_admin_tools_not_flagged(name):
    assert not is_admin_tool(name)


def test_viewer_blocked_from_admin_tool_with_id_preserved():
    with pytest.raises(RpcError) as exc:
        authorize(parse_message(call("admin_reset_key", req_id="abc-7")), "viewer")
    assert exc.value.code == UNAUTHORIZED_TOOL
    assert exc.value.request_id == "abc-7"


def test_admin_allowed_admin_tool():
    authorize(parse_message(call("admin_reset_key")), "admin")


def test_viewer_allowed_normal_tool():
    authorize(parse_message(call("get_customer_record")), "viewer")


def test_other_methods_pass_for_viewer():
    authorize(parse_message(b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}'), "viewer")


@pytest.mark.parametrize(
    "params",
    [{}, {"name": 123}, {"name": None}, {"name": ["admin_reset_key"]}],
)
def test_tools_call_without_string_name_is_invalid_params(params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}).encode()
    with pytest.raises(RpcError) as exc:
        authorize(parse_message(body), "admin")
    assert exc.value.code == INVALID_PARAMS


@pytest.mark.parametrize("body", [b"{not json", b"", b'{"jsonrpc":"2.0","id":NaN,"method":"ping"}', b"\xff\xfe"])
def test_parse_errors(body):
    with pytest.raises(RpcError) as exc:
        parse_message(body)
    assert exc.value.code == PARSE_ERROR
    assert exc.value.http_status == 400


@pytest.mark.parametrize(
    "body",
    [
        # batch hiding an admin call
        b'[{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"admin_reset_key"}}]',
        # duplicate keys: checked as one tool, run as another by a first-wins parser
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"admin_reset_key","name":"get_customer_record"}}',
        b'{"jsonrpc":"2.0","id":1,"method":"tools/list","method":"tools/call"}',
        b'"just a string"',
        b'{"id":1,"method":"tools/list"}',                                 # missing jsonrpc
        b'{"jsonrpc":"1.0","id":1,"method":"tools/list"}',
        b'{"jsonrpc":"2.0","id":true,"method":"tools/list"}',
        b'{"jsonrpc":"2.0","id":null,"method":"tools/list"}',
        b'{"jsonrpc":"2.0","id":1,"method":42}',
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":["admin_reset_key"]}',
        # notification form of tools/call: no id, so no error could be returned
        b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"admin_reset_key"}}',
        b'{"jsonrpc":"2.0","id":1}',                                       # neither request nor response
        b'{"jsonrpc":"2.0","id":1,"result":{},"error":{"code":1,"message":"x"}}',
    ],
)
def test_invalid_requests(body):
    with pytest.raises(RpcError) as exc:
        parse_message(body)
    assert exc.value.code == INVALID_REQUEST
    assert exc.value.http_status == 400


def test_client_response_and_notification_are_accepted():
    assert parse_message(b'{"jsonrpc":"2.0","id":5,"result":{}}').method is None
    assert not parse_message(b'{"jsonrpc":"2.0","method":"notifications/initialized"}').is_request
