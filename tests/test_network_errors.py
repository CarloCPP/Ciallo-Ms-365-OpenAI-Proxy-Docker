from __future__ import annotations

import asyncio
import json
import socket

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from m365_copilot_openai_proxy.app import create_app
from m365_copilot_openai_proxy.config import Settings
from m365_copilot_openai_proxy.consumer_client import ConsumerCopilotClient
from m365_copilot_openai_proxy.error_handlers import register_error_handlers
from test_reasoning_output import _events, _payload
from test_substrate_cot_narration import _client


@pytest.mark.parametrize("provider", ["m365", "consumer"])
@pytest.mark.parametrize("protocol", ["chat/completions", "messages", "responses"])
@pytest.mark.parametrize("stream", [False, True])
def test_real_tcp_refusal_has_protocol_network_error_not_quota(provider, protocol, stream, tmp_path, monkeypatch):
    import m365_copilot_openai_proxy.consumer_client as cc
    import m365_copilot_openai_proxy.substrate_client as sc

    # Keep the port reserved but not listening: no DNS dependency or race with
    # another server acquiring a released port.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        monkeypatch.setattr(sc, "_WS_BASE", f"ws://127.0.0.1:{port}/")
        monkeypatch.setattr(cc, "BASE_URL", f"http://127.0.0.1:{port}")
        app = create_app(
            Settings(TOKEN_DIR=str(tmp_path), API_KEY="k", ADMIN_PASSWORD=""),
            copilot_client_factory=lambda **kwargs: _client(),
        )
        key = "k"
        if provider == "consumer":
            account = app.state.account_store.add(name="Consumer")
            app.state.account_store.set_consumer_auth(account.id, cookies=[], access_token="consumer-token")
            key = app.state.key_store.add(name="Consumer Key", account_id=account.id).key
            app.state.consumer_client_factory = lambda **kwargs: ConsumerCopilotClient(timeout=5)
        app.state.tool_planning_mode = "native"
        body = _payload(protocol, tools=stream, stream=stream)
        if provider == "consumer":
            body["model"] = "copilot"
        response = TestClient(app).post("/v1/" + protocol, headers={"Authorization": f"Bearer {key}"}, json=body)
    assert "retry-after" not in response.headers
    if not stream:
        assert response.status_code == 503, response.text
        error = response.json()["error"]
        assert error["type"] == ("api_error" if protocol == "messages" else "network_error")
        assert error["code"] == "network_error"
        if protocol == "messages":
            assert response.json()["type"] == "error"
        return
    assert response.status_code == 200
    events = _events(response)
    assert "rate_limit" not in json.dumps(events)
    if protocol == "chat/completions":
        errors = [event["m365_error"] for event in events if "m365_error" in event]
        assert [error["type"] for error in errors] == ["network_error"]
        assert response.text.endswith("data: [DONE]\n\n")
    elif protocol == "messages":
        assert events[-1]["type"] == "error"
        assert events[-1]["error"]["type"] == "api_error"
        assert events[-1]["error"]["code"] == "network_error"
        assert not any(event["type"] == "message_stop" for event in events)
    else:
        assert events[-1]["type"] == "response.failed"
        assert events[-1]["response"]["error"]["code"] == "network_error"
        assert not any(event["type"] == "response.completed" for event in events)


@pytest.mark.parametrize("protocol", ["chat/completions", "messages", "responses"])
def test_native_stream_tcp_failure_retains_network_type(protocol, tmp_path, monkeypatch):
    import m365_copilot_openai_proxy.substrate_client as sc

    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        monkeypatch.setattr(sc, "_WS_BASE", f"ws://127.0.0.1:{reserved.getsockname()[1]}/")
        app = create_app(
            Settings(TOKEN_DIR=str(tmp_path), API_KEY="k", ADMIN_PASSWORD=""),
            copilot_client_factory=lambda **kwargs: _client(),
        )
        response = TestClient(app).post("/v1/" + protocol, headers={"Authorization": "Bearer k"}, json=_payload(protocol, tools=False, stream=True))
    events = _events(response)
    if protocol == "chat/completions":
        assert any(event.get("m365_error", {}).get("type") == "network_error" for event in events)
    elif protocol == "messages":
        assert events[-1]["error"]["code"] == "network_error"
    else:
        assert events[-1]["response"]["error"]["code"] == "network_error"


def test_unrelated_503_is_not_mislabeled_as_a_transport_failure():
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/configuration")
    async def configuration():
        raise HTTPException(status_code=503, detail="missing account configuration")

    response = TestClient(app).get("/configuration")
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "http_error"


@pytest.mark.parametrize("during_handshake", [False, True])
def test_m365_aborted_socket_never_finishes_as_success(during_handshake, monkeypatch):
    import websockets
    import m365_copilot_openai_proxy.substrate_client as sc
    from m365_copilot_openai_proxy.substrate_client import SubstrateNetworkError

    async def abort_handshake(reader, writer):
        await reader.read(4096)
        writer.transport.abort()

    async def abort_turn(ws):
        await ws.recv()
        await ws.send("{}" + sc.SIGNALR_SEP)
        await ws.recv()
        await ws.send(json.dumps({"type": 1, "target": "update", "arguments": [{"writeAtCursor": "partial answer"}]}) + sc.SIGNALR_SEP)
        ws.transport.abort()

    async def run():
        if during_handshake:
            server = await asyncio.start_server(abort_handshake, "127.0.0.1", 0)
        else:
            server = await websockets.serve(abort_turn, "127.0.0.1", 0)
        async with server:
            port = server.sockets[0].getsockname()[1]
            monkeypatch.setattr(sc, "_WS_BASE", f"ws://127.0.0.1:{port}")
            with pytest.raises(SubstrateNetworkError):
                await _client().chat("question", [])

    asyncio.run(run())


def test_http_upgrade_auth_refusal_is_not_a_network_failure(monkeypatch):
    import websockets
    import m365_copilot_openai_proxy.substrate_client as sc
    from m365_copilot_openai_proxy.substrate_client import SubstrateCopilotError, SubstrateNetworkError
    from m365_copilot_openai_proxy.routes_api_common import upstream_http_error

    async def run():
        async with websockets.serve(
            lambda ws: None, "127.0.0.1", 0,
            process_request=lambda connection, request: connection.respond(401, "authentication required"),
        ) as server:
            monkeypatch.setattr(sc, "_WS_BASE", f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
            with pytest.raises(SubstrateCopilotError) as caught:
                await _client().chat("question", [])
            assert not isinstance(caught.value, SubstrateNetworkError)
            assert upstream_http_error(caught.value).status_code == 502

    asyncio.run(run())


@pytest.mark.parametrize("close_code,raw_frame,status", [
    (1000, True, 502), (1008, True, 502), (1011, True, 502),
    (1008, False, 502), (1006, False, 503),
])
def test_consumer_close_frames_distinguish_refusal_from_broken_transport(close_code, raw_frame, status):
    from curl_cffi.requests import CurlWsFlag, WebSocketClosed
    from m365_copilot_openai_proxy.consumer_adapter import ConsumerClientAdapter
    from m365_copilot_openai_proxy.routes_api_common import upstream_http_error
    from m365_copilot_openai_proxy.substrate_client import SubstrateCopilotError
    from test_consumer_curl import _FakeSession, _FakeSocket

    class Socket(_FakeSocket):
        close_sent = False

        async def recv(self, *, timeout=None):
            if self.frames:
                return await super().recv(timeout=timeout)
            if raw_frame and not self.close_sent:
                self.close_sent = True
                return close_code.to_bytes(2, "big"), CurlWsFlag.CLOSE
            # A recorded close frame must retain priority even if the socket
            # subsequently aborts before curl_cffi finishes its shutdown.
            raise WebSocketClosed("closed", 1006 if raw_frame else close_code)

    session = _FakeSession()
    session.socket = Socket([b'{"event":"appendText","text":"partial"}'])
    client = ConsumerClientAdapter(ConsumerCopilotClient(session_factory=lambda **kwargs: session))
    with pytest.raises(SubstrateCopilotError) as caught:
        asyncio.run(client.chat("question", []))
    assert upstream_http_error(caught.value).status_code == status


def test_consumer_proxy_negotiation_failure_is_not_assumed_to_be_network_outage():
    from curl_cffi.const import CurlECode
    from curl_cffi.requests.exceptions import RequestException
    from m365_copilot_openai_proxy.consumer_adapter import ConsumerClientAdapter
    from m365_copilot_openai_proxy.routes_api_common import upstream_http_error
    from m365_copilot_openai_proxy.substrate_client import SubstrateCopilotError, SubstrateNetworkError
    from test_consumer_curl import _FakeSession

    session = _FakeSession(get_error=RequestException("SOCKS authentication rejected", CurlECode.PROXY))
    client = ConsumerClientAdapter(ConsumerCopilotClient(session_factory=lambda **kwargs: session))
    with pytest.raises(SubstrateCopilotError) as caught:
        asyncio.run(client.chat("question", []))
    assert not isinstance(caught.value, SubstrateNetworkError)
    assert upstream_http_error(caught.value).status_code == 502


@pytest.mark.parametrize("reply,status", [(None, 502), (1, 502), (2, 502), (3, 503), (4, 503), (5, 503), (6, 503), (7, 502), (8, 502)])
def test_m365_socks_reply_classification_uses_protocol_error_code(reply, status, monkeypatch):
    import functools
    import websockets
    import m365_copilot_openai_proxy.substrate_client as sc
    from m365_copilot_openai_proxy.routes_api_common import upstream_http_error

    async def run():
        sent_reply = asyncio.Event()

        async def reject_connection(reader, writer):
            try:
                greeting = await reader.readexactly(2)
                await reader.readexactly(greeting[1])
                if reply is None:
                    writer.write(b"\x05\xff")
                else:
                    writer.write(b"\x05\x00")
                    await writer.drain()
                    request = await reader.readexactly(4)
                    size = {1: 4, 4: 16}.get(request[3])
                    if size is None:
                        size = (await reader.readexactly(1))[0]
                    await reader.readexactly(size + 2)
                    writer.write(b"\x05" + bytes([reply]) + b"\x00\x01" + bytes(6))
                await writer.drain()
                sent_reply.set()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(reject_connection, "127.0.0.1", 0)
        async with server:
            proxy = f"socks5h://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            monkeypatch.setattr(sc, "_WS_BASE", "wss://copilot.invalid/chat")
            monkeypatch.setattr(websockets, "connect", functools.partial(websockets.connect, proxy=proxy))
            with pytest.raises(sc.SubstrateCopilotError) as caught:
                await _client().chat("question", [])
            assert sent_reply.is_set()
            assert upstream_http_error(caught.value).status_code == status

    asyncio.run(run())
