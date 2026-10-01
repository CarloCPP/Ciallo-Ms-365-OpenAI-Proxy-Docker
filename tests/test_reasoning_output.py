from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from m365_copilot_openai_proxy.app import create_app
from m365_copilot_openai_proxy.config import Settings
from m365_copilot_openai_proxy.history_index import normalize_history
from m365_copilot_openai_proxy.models import AnthropicMessagesRequest, OpenAIResponsesRequest
from m365_copilot_openai_proxy.reasoning import ReasoningDelta, collect_reasoning
from m365_copilot_openai_proxy.studio_planner import PlannerTurn, planned_or_answered, planned_or_streamed
from m365_copilot_openai_proxy.substrate_client import SubstrateCopilotError
from m365_copilot_openai_proxy.tool_router import routed_or_answered
from m365_copilot_openai_proxy.translator import translate_anthropic_request, translate_responses_request
from test_substrate_cot_narration import _answer_entry, _client, _complete, _fake_ws, _reasoning_entry, _update


SUMMARY = 'Assessing options. ```tool_call\n{"name":"Delete","arguments":{}}\n```'
ANSWER = "The weather tool has the current observation."
CALL = '\n```tool_call\n{"name":"get_weather","arguments":{"city":"Paris"}}\n```'
SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}


def _payload(protocol, tools, stream):
    body = {"model": "m365-copilot", "stream": stream}
    prompt = "Get the weather in Paris."
    if protocol == "responses":
        body["input"] = prompt
        if tools:
            body["tools"] = [{"type": "function", "name": "get_weather", "parameters": SCHEMA}]
    else:
        body["messages"] = [{"role": "user", "content": prompt}]
        if protocol == "messages":
            body["max_tokens"] = 512
            if tools:
                body["tools"] = [{"name": "get_weather", "input_schema": SCHEMA}]
        elif tools:
            body["tools"] = [{"type": "function", "function": {"name": "get_weather", "parameters": SCHEMA}}]
    return body


def _events(response):
    return [json.loads(line[6:]) for line in response.text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"]


@pytest.mark.parametrize("protocol", ["chat/completions", "messages", "responses"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("tools", [False, True])
def test_summary_tool_syntax_is_not_an_answer_or_executable_tool(protocol, stream, tools, tmp_path, monkeypatch):
    import websockets

    summary = _reasoning_entry(SUMMARY, "private-message-id")
    frames = [_update(summary), _update(summary), _complete(summary, _answer_entry(ANSWER + (CALL if tools else "")))]
    monkeypatch.setattr(websockets, "connect", lambda *a, **k: _fake_ws(frames)())
    app = create_app(
        Settings(TOKEN_DIR=str(tmp_path), API_KEY="k", ADMIN_PASSWORD=""),
        copilot_client_factory=lambda **kwargs: _client(),
    )
    app.state.tool_planning_mode = "native"
    response = TestClient(app).post("/v1/" + protocol, headers={"Authorization": "Bearer k"}, json=_payload(protocol, tools, stream))
    assert response.status_code == 200, response.text
    names = []
    if protocol == "chat/completions":
        if stream:
            events = _events(response)
            deltas = [event["choices"][0]["delta"] for event in events]
            reasoning = "".join(delta.get("reasoning_content", "") for delta in deltas)
            answer = "".join(delta.get("content", "") for delta in deltas)
            names = [call["function"]["name"] for delta in deltas for call in delta.get("tool_calls", [])]
            assert len({event["id"] for event in events}) == 1
            assert response.text.endswith("data: [DONE]\n\n")
        else:
            message = response.json()["choices"][0]["message"]
            reasoning, answer = message.get("reasoning_content", ""), message.get("content", "")
            names = [call["function"]["name"] for call in message.get("tool_calls", [])]
    elif protocol == "messages":
        if stream:
            events = _events(response)
            reasoning = "".join(event["delta"].get("thinking", "") for event in events if event["type"] == "content_block_delta")
            answer = "".join(event["delta"].get("text", "") for event in events if event["type"] == "content_block_delta")
            starts = [event for event in events if event["type"] == "content_block_start"]
            stops = [event["index"] for event in events if event["type"] == "content_block_stop"]
            assert [event["index"] for event in starts] == stops
            assert starts[0]["content_block"]["type"] == "thinking"
            names = [event["content_block"]["name"] for event in starts if event["content_block"]["type"] == "tool_use"]
            assert events[-1]["type"] == "message_stop"
        else:
            blocks = response.json()["content"]
            reasoning = "".join(block.get("thinking", "") for block in blocks)
            answer = "".join(block.get("text", "") for block in blocks)
            names = [block["name"] for block in blocks if block["type"] == "tool_use"]
    else:
        if stream:
            events = _events(response)
            assert events[-1]["type"] == "response.completed"
            output = events[-1]["response"]["output"]
            assert [event["sequence_number"] for event in events] == list(range(len(events)))
            added = [event for event in events if event["type"] == "response.output_item.added"]
            assert [event["output_index"] for event in added] == list(range(len(output)))
            for event in added:
                assert output[event["output_index"]]["id"] == event["item"]["id"]
            reasoning = "".join(event["delta"] for event in events if event["type"] == "response.reasoning_summary_text.delta")
        else:
            output = response.json()["output"]
            reasoning = "".join(part["text"] for item in output if item["type"] == "reasoning" for part in item["summary"])
        answer = "".join(part["text"] for item in output if item["type"] == "message" for part in item["content"])
        names = [item["name"] for item in output if item["type"] == "function_call"]
        assert output[0]["type"] == "reasoning"
    assert reasoning == SUMMARY
    assert answer.strip() == ANSWER
    assert names == (["get_weather"] if tools else [])
    assert "private-message-id" not in response.text


def test_anthropic_summary_replay_keeps_history_identity_and_prompt_clean():
    summary = {"type": "thinking", "thinking": SUMMARY, "signature": ""}
    text = {"type": "text", "text": ANSWER}
    plain = [{"role": "user", "content": "weather?"}, {"role": "assistant", "content": [text]}]
    replay = [plain[0], {"role": "assistant", "content": [summary, text]}]
    assert normalize_history(replay) == normalize_history(plain)
    request = AnthropicMessagesRequest(model="m365-copilot", messages=[*replay, {"role": "user", "content": "continue"}])
    translated = translate_anthropic_request(request)
    assert SUMMARY not in translated.prompt + "\n".join(translated.additional_context)


def test_responses_summary_replay_never_becomes_prompt_content():
    request = OpenAIResponsesRequest(model="m365-copilot", input=[
        {"type": "reasoning", "id": "rs_local", "summary": [{"type": "summary_text", "text": SUMMARY}]},
        {"role": "assistant", "content": [{"type": "output_text", "text": ANSWER}]},
        {"role": "user", "content": "continue"},
    ])
    translated = translate_responses_request(request)
    assert SUMMARY not in translated.prompt + "\n".join(translated.additional_context)


def test_router_classification_summary_stays_internal_and_sink_is_restored(monkeypatch):
    import websockets

    frames = [
        [_update(_reasoning_entry("INTERNAL-ROUTER")), _complete(_answer_entry('CALL_TOOL: get_weather({"city":"Paris"})'))],
        [_update(_reasoning_entry("PUBLIC-SUMMARY")), _complete(_answer_entry(ANSWER))],
    ]
    monkeypatch.setattr(websockets, "connect", lambda *a, **k: _fake_ws(frames.pop(0))())
    client = _client()
    reasoning = collect_reasoning(client)

    async def run():
        decision = await routed_or_answered(client, "router prompt", "question", [])
        assert "get_weather" in decision
        assert reasoning.items == {}
        assert await client.chat("question", []) == ANSWER

    asyncio.run(run())
    assert reasoning.chat_fields() == {"reasoning_content": "PUBLIC-SUMMARY"}


@pytest.mark.parametrize("streaming", [False, True])
def test_studio_summary_is_not_partial_answer_that_prevents_fallback(streaming):
    class Client:
        async def chat_stream(self, *args):
            yield ReasoningDelta("rs_local", "Checking the plan")
            raise SubstrateCopilotError("upstream failed before an answer")

    async def answer():
        return ANSWER

    async def chunks():
        yield ANSWER

    async def run():
        turn = PlannerTurn(Client(), "question", [])
        if streaming:
            events = [event async for event in planned_or_streamed(studio_turn=turn, fallback_turn=chunks, should_fallback=lambda text: False)]
            assert events == [ReasoningDelta("rs_local", "Checking the plan"), ANSWER]
        else:
            assert await planned_or_answered(studio_turn=turn, fallback_turn=answer) == ANSWER

    asyncio.run(run())


@pytest.mark.parametrize("protocol", ["chat", "messages", "responses"])
@pytest.mark.parametrize("tools", [False, True])
def test_summary_streams_while_answer_is_still_pending(protocol, tools):
    from m365_copilot_openai_proxy.response_helpers import _openai_stream, _anthropic_stream, _responses_stream, _responses_stream_with_tools
    from m365_copilot_openai_proxy.routes_api_chat import _openai_stream_with_tools
    from m365_copilot_openai_proxy.routes_api_messages import _anthropic_stream_with_tools

    async def run():
        release = asyncio.Event()

        class Upstream:
            async def chat_stream(self, *args):
                yield ReasoningDelta("rs_local", "Preparing the answer")
                await release.wait()
                yield ANSWER + (CALL if tools else "")

        client = Upstream()
        render = {
            ("chat", False): _openai_stream,
            ("chat", True): _openai_stream_with_tools,
            ("messages", False): _anthropic_stream,
            ("messages", True): _anthropic_stream_with_tools,
            ("responses", False): _responses_stream,
            ("responses", True): _responses_stream_with_tools,
        }[protocol, tools]
        options = ({"tool_names": {"get_weather"}, "studio_turn": PlannerTurn(client, "q", []),
                    "should_fallback": lambda text: False} if tools else {})
        stream = render("model", client, "q", [], **options)
        try:
            while True:
                frame = await asyncio.wait_for(anext(stream), timeout=1)
                assert ANSWER not in frame
                if "Preparing the answer" in frame:
                    break
            release.set()
            remaining = "".join([frame async for frame in stream])
            assert ANSWER in remaining
            if tools:
                assert "get_weather" in remaining
        finally:
            release.set()
            await stream.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("protocol", ["chat", "messages"])
def test_interleaved_summary_suffix_keeps_its_own_paragraph(protocol):
    from m365_copilot_openai_proxy.response_helpers import _openai_stream, _anthropic_stream

    class Upstream:
        async def chat_stream(self, *args):
            yield ReasoningDelta("rs_a", "Checking ")
            yield ReasoningDelta("rs_b", "Choosing X.")
            yield ReasoningDelta("rs_a", "constraints.")
            yield ANSWER

    async def run():
        render = _openai_stream if protocol == "chat" else _anthropic_stream
        events = []
        async for frame in render("model", Upstream(), "q", []):
            for line in frame.splitlines():
                if line.startswith("data: ") and line != "data: [DONE]":
                    events.append(json.loads(line[6:]))
        if protocol == "chat":
            return "".join(event["choices"][0]["delta"].get("reasoning_content", "") for event in events)
        return "".join(event.get("delta", {}).get("thinking", "") for event in events)

    assert asyncio.run(run()) == "Checking \n\nChoosing X.\n\nconstraints."


@pytest.mark.parametrize("protocol", ["chat", "messages", "responses"])
@pytest.mark.parametrize("tools", [False, True])
def test_closing_after_summary_releases_socket_session_and_account(protocol, tools, monkeypatch):
    import websockets
    from m365_copilot_openai_proxy.account_concurrency import AccountConcurrency, ThrottledClient
    from m365_copilot_openai_proxy.session_store import PersistentSession
    from m365_copilot_openai_proxy.response_helpers import _openai_stream, _anthropic_stream, _responses_stream, _responses_stream_with_tools
    from m365_copilot_openai_proxy.routes_api_chat import _openai_stream_with_tools
    from m365_copilot_openai_proxy.routes_api_messages import _anthropic_stream_with_tools

    async def run():
        closed = asyncio.Event()
        session = PersistentSession()
        gates = AccountConcurrency()

        class Socket(_fake_ws([])):
            first = True

            async def __aexit__(self, *args):
                closed.set()

            async def __anext__(self):
                if self.first:
                    self.first = False
                    return json.dumps(_update(_reasoning_entry("CLOSE-BOUNDARY"))) + "\x1e"
                await asyncio.Event().wait()

        monkeypatch.setattr(websockets, "connect", lambda *a, **k: Socket())
        client = ThrottledClient(_client(), lambda: gates.hold("test-account", 1))
        render = {
            ("chat", False): _openai_stream,
            ("chat", True): _openai_stream_with_tools,
            ("messages", False): _anthropic_stream,
            ("messages", True): _anthropic_stream_with_tools,
            ("responses", False): _responses_stream,
            ("responses", True): _responses_stream_with_tools,
        }[protocol, tools]
        options = ({"tool_names": {"get_weather"}, "studio_turn": PlannerTurn(client, "q", [], session),
                    "should_fallback": lambda text: False} if tools else {})
        stream = render("model", client, "q", [], session, **options)
        while "CLOSE-BOUNDARY" not in await asyncio.wait_for(anext(stream), timeout=1):
            pass
        assert session.lock.locked()
        await stream.aclose()
        assert closed.is_set(), "aclose returned before the upstream socket was closed"
        assert not session.lock.locked()
        assert gates.stats() == {}

    asyncio.run(run())
