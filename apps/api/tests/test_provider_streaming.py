"""Streaming for all three adapters, against canned SSE bodies shaped like each provider's real
wire format. See test_provider_openai_compatible.py's module docstring for why MockTransport."""

import base64
import json

import httpx

from app.providers.anthropic import AnthropicProvider
from app.providers.base import (
    ChatMessage,
    Done,
    ImagePart,
    StreamError,
    TextDelta,
    ToolCall,
    ToolCallRequest,
    ToolDefinition,
    Usage,
)
from app.providers.google import GoogleProvider
from app.providers.openai_compatible import OpenAICompatibleProvider


def _sse(*lines: str) -> bytes:
    """Join raw SSE data lines the way a real server would, blank-line-terminated."""
    return ("\n\n".join(lines) + "\n\n").encode()


MESSAGES = [ChatMessage(role="user", content="Hi there")]
_IMAGE = ImagePart(mime="image/png", data=b"fake-png-bytes")
_IMAGE_MESSAGES = [ChatMessage(role="user", content="What's this?", images=(_IMAGE,))]
_TOOL = ToolDefinition(
    name="get_weather",
    description="Get the current weather for a city.",
    parameters={
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
)
_TOOL_CALL_MESSAGES = [
    ChatMessage(role="user", content="What's the weather in Paris?"),
    ChatMessage(
        role="assistant",
        content="",
        tool_calls=(ToolCall(id="call_1", name="get_weather", arguments={"city": "Paris"}),),
    ),
    ChatMessage(role="tool", content="Sunny, 22C", tool_call_id="call_1", tool_name="get_weather"),
]


# ---------- OpenAI-compatible ----------


async def test_openai_stream_yields_text_then_usage_then_done() -> None:
    """A realistic OpenAI SSE body becomes TextDelta -> TextDelta -> Usage -> Done, in order."""
    body = _sse(
        'data: {"choices":[{"delta":{"content":"Hel"}}]}',
        'data: {"choices":[{"delta":{"content":"lo"}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
        '"usage":{"prompt_tokens":5,"completion_tokens":2}}',
        "data: [DONE]",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    provider = OpenAICompatibleProvider(api_key="k", transport=httpx.MockTransport(handler))
    chunks = [c async for c in provider.stream(model="gpt-5", messages=MESSAGES, max_tokens=100)]

    assert chunks[0] == TextDelta(text="Hel")
    assert chunks[1] == TextDelta(text="lo")
    assert chunks[2] == Usage(input_tokens=5, output_tokens=2)
    assert chunks[3] == Done(finish_reason="stop")


async def test_openai_stream_reports_a_rejected_request() -> None:
    """A 4xx response before any SSE body becomes a StreamError, not a crash."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b'{"error":"invalid api key"}')

    provider = OpenAICompatibleProvider(api_key="k", transport=httpx.MockTransport(handler))
    chunks = [c async for c in provider.stream(model="gpt-5", messages=MESSAGES, max_tokens=100)]

    assert len(chunks) == 1
    assert isinstance(chunks[0], StreamError)
    assert "401" in chunks[0].message


# ---------- Anthropic ----------


async def test_anthropic_stream_yields_text_then_usage_then_done() -> None:
    """A realistic Anthropic SSE body — separate message_start/delta/stop events — parses correctly."""
    body = _sse(
        'data: {"type":"message_start","message":{"usage":{"input_tokens":8}}}',
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hi"}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"!"}}',
        'data: {"type":"content_block_stop","index":0}',
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}',
        'data: {"type":"message_stop"}',
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    provider = AnthropicProvider(api_key="k", transport=httpx.MockTransport(handler))
    chunks = [
        c async for c in provider.stream(model="claude-opus-5", messages=MESSAGES, max_tokens=100)
    ]

    assert [c for c in chunks if isinstance(c, TextDelta)] == [TextDelta(text="Hi"), TextDelta(text="!")]
    assert Usage(input_tokens=8, output_tokens=3) in chunks
    assert Done(finish_reason="end_turn") in chunks


async def test_anthropic_stream_extracts_the_system_prompt() -> None:
    """A system message is sent as Anthropic's top-level `system` field, not a messages turn."""
    seen_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, content=_sse('data: {"type":"message_stop"}'))

    provider = AnthropicProvider(api_key="k", transport=httpx.MockTransport(handler))
    messages = [ChatMessage(role="system", content="Be terse."), ChatMessage(role="user", content="Hi")]
    async for _ in provider.stream(model="claude-opus-5", messages=messages, max_tokens=50):
        pass

    body = json.loads(seen_requests[0].content)
    assert body["system"] == "Be terse."
    assert body["messages"] == [{"role": "user", "content": "Hi"}]


async def test_anthropic_stream_reports_an_error_event() -> None:
    """An in-stream error event (as opposed to a rejected request) becomes a StreamError too."""
    body = _sse('data: {"type":"error","error":{"message":"overloaded"}}')

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    provider = AnthropicProvider(api_key="k", transport=httpx.MockTransport(handler))
    chunks = [
        c async for c in provider.stream(model="claude-opus-5", messages=MESSAGES, max_tokens=100)
    ]

    assert chunks == [StreamError(message="overloaded")]


# ---------- Google ----------


async def test_google_stream_yields_text_then_usage_then_done() -> None:
    """A realistic Gemini SSE body parses text out of candidates[].content.parts[].text."""
    body = _sse(
        'data: {"candidates":[{"content":{"parts":[{"text":"Hi"}],"role":"model"}}]}',
        'data: {"candidates":[{"content":{"parts":[{"text":"!"}],"role":"model"},'
        '"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":4,"candidatesTokenCount":2}}',
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    provider = GoogleProvider(api_key="k", transport=httpx.MockTransport(handler))
    chunks = [
        c async for c in provider.stream(model="gemini-2.5-pro", messages=MESSAGES, max_tokens=100)
    ]

    assert [c for c in chunks if isinstance(c, TextDelta)] == [TextDelta(text="Hi"), TextDelta(text="!")]
    assert Usage(input_tokens=4, output_tokens=2) in chunks
    assert Done(finish_reason="STOP") in chunks


async def test_google_stream_maps_assistant_role_to_model() -> None:
    """Google calls the assistant's role "model", not "assistant", in conversation history."""
    seen_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, content=_sse('data: {"candidates":[]}'))

    provider = GoogleProvider(api_key="k", transport=httpx.MockTransport(handler))
    messages = [
        ChatMessage(role="user", content="Hi"),
        ChatMessage(role="assistant", content="Hello"),
        ChatMessage(role="user", content="How are you?"),
    ]
    async for _ in provider.stream(model="gemini-2.5-pro", messages=messages, max_tokens=50):
        pass

    body = json.loads(seen_requests[0].content)
    assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]


async def test_google_stream_reports_a_rejected_request() -> None:
    """A 400 response (Google's shape for a bad key) becomes a StreamError, not a crash."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=b'{"error":{"message":"API key not valid"}}')

    provider = GoogleProvider(api_key="k", transport=httpx.MockTransport(handler))
    chunks = [
        c async for c in provider.stream(model="gemini-2.5-pro", messages=MESSAGES, max_tokens=100)
    ]

    assert len(chunks) == 1
    assert isinstance(chunks[0], StreamError)
    assert "400" in chunks[0].message


# ---------- Images (all three providers) ----------


async def test_openai_sends_a_bare_string_when_there_are_no_images() -> None:
    """The regression this design exists to prevent: some OpenAI-compatible servers (older
    Ollama, vLLM) reject the block-array content form even for plain text."""
    seen_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, content=_sse("data: [DONE]"))

    provider = OpenAICompatibleProvider(api_key="k", transport=httpx.MockTransport(handler))
    async for _ in provider.stream(model="llama3", messages=MESSAGES, max_tokens=50):
        pass

    body = json.loads(seen_requests[0].content)
    assert body["messages"] == [{"role": "user", "content": "Hi there"}]


async def test_openai_sends_an_image_url_block_when_an_image_is_attached() -> None:
    """An attached image becomes a base64 data: URL alongside the text block."""
    seen_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, content=_sse("data: [DONE]"))

    provider = OpenAICompatibleProvider(api_key="k", transport=httpx.MockTransport(handler))
    async for _ in provider.stream(model="gpt-5", messages=_IMAGE_MESSAGES, max_tokens=50):
        pass

    body = json.loads(seen_requests[0].content)
    content = body["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "What's this?"}
    encoded = base64.b64encode(_IMAGE.data).decode("ascii")
    assert content[1] == {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}


async def test_anthropic_sends_a_bare_string_when_there_are_no_images() -> None:
    """Same regression guard as the OpenAI-compatible adapter, for Anthropic's payload."""
    seen_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, content=_sse('data: {"type":"message_stop"}'))

    provider = AnthropicProvider(api_key="k", transport=httpx.MockTransport(handler))
    async for _ in provider.stream(model="claude-opus-5", messages=MESSAGES, max_tokens=50):
        pass

    body = json.loads(seen_requests[0].content)
    assert body["messages"] == [{"role": "user", "content": "Hi there"}]


async def test_anthropic_sends_an_image_block_when_an_image_is_attached() -> None:
    """An attached image becomes Anthropic's base64 image source block alongside the text block."""
    seen_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, content=_sse('data: {"type":"message_stop"}'))

    provider = AnthropicProvider(api_key="k", transport=httpx.MockTransport(handler))
    async for _ in provider.stream(model="claude-opus-5", messages=_IMAGE_MESSAGES, max_tokens=50):
        pass

    body = json.loads(seen_requests[0].content)
    content = body["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "What's this?"}
    encoded = base64.b64encode(_IMAGE.data).decode("ascii")
    assert content[1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": encoded},
    }


async def test_google_sends_an_inline_data_part_when_an_image_is_attached() -> None:
    """An attached image becomes Google's inline_data part alongside the text part."""
    seen_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, content=_sse('data: {"candidates":[]}'))

    provider = GoogleProvider(api_key="k", transport=httpx.MockTransport(handler))
    async for _ in provider.stream(model="gemini-2.5-pro", messages=_IMAGE_MESSAGES, max_tokens=50):
        pass

    body = json.loads(seen_requests[0].content)
    parts = body["contents"][0]["parts"]
    assert parts[0] == {"text": "What's this?"}
    encoded = base64.b64encode(_IMAGE.data).decode("ascii")
    assert parts[1] == {"inline_data": {"mime_type": "image/png", "data": encoded}}


# ---------- Tool calling (all three providers) ----------


async def test_openai_sends_no_tools_key_when_none_are_offered() -> None:
    """The regression this design exists to prevent, same as the bare-string image case: an
    unexpected `tools` field could break an endpoint that doesn't expect it."""
    seen_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, content=_sse("data: [DONE]"))

    provider = OpenAICompatibleProvider(api_key="k", transport=httpx.MockTransport(handler))
    async for _ in provider.stream(model="gpt-5", messages=MESSAGES, max_tokens=50):
        pass

    assert "tools" not in json.loads(seen_requests[0].content)


async def test_openai_sends_the_tool_definition_when_offered() -> None:
    seen_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, content=_sse("data: [DONE]"))

    provider = OpenAICompatibleProvider(api_key="k", transport=httpx.MockTransport(handler))
    async for _ in provider.stream(model="gpt-5", messages=MESSAGES, max_tokens=50, tools=[_TOOL]):
        pass

    body = json.loads(seen_requests[0].content)
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": _TOOL.name,
                "description": _TOOL.description,
                "parameters": _TOOL.parameters,
            },
        }
    ]


async def test_openai_assembles_a_streamed_tool_call() -> None:
    """Arguments arrive in fragments across several chunks, keyed by index — reassembled into
    one complete ToolCallRequest once the finish_reason arrives."""
    body = _sse(
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function",'
        '"function":{"name":"get_weather","arguments":""}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"city\\""}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":":\\"Paris\\"}"}}]}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    provider = OpenAICompatibleProvider(api_key="k", transport=httpx.MockTransport(handler))
    chunks = [
        c async for c in provider.stream(model="gpt-5", messages=MESSAGES, max_tokens=50, tools=[_TOOL])
    ]

    assert chunks == [
        ToolCallRequest(calls=(ToolCall(id="call_1", name="get_weather", arguments={"city": "Paris"}),))
    ]


async def test_openai_replays_a_tool_call_and_its_result() -> None:
    """A past assistant tool_calls turn and the tool turn answering it round-trip into OpenAI's
    own `tool_calls`/`tool_call_id` message shape."""
    seen_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, content=_sse("data: [DONE]"))

    provider = OpenAICompatibleProvider(api_key="k", transport=httpx.MockTransport(handler))
    async for _ in provider.stream(model="gpt-5", messages=_TOOL_CALL_MESSAGES, max_tokens=50):
        pass

    messages = json.loads(seen_requests[0].content)["messages"]
    assert messages[1]["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
        }
    ]
    assert messages[2] == {"role": "tool", "tool_call_id": "call_1", "content": "Sunny, 22C"}


async def test_anthropic_sends_no_tools_key_when_none_are_offered() -> None:
    seen_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, content=_sse('data: {"type":"message_stop"}'))

    provider = AnthropicProvider(api_key="k", transport=httpx.MockTransport(handler))
    async for _ in provider.stream(model="claude-opus-5", messages=MESSAGES, max_tokens=50):
        pass

    assert "tools" not in json.loads(seen_requests[0].content)


async def test_anthropic_sends_the_tool_definition_when_offered() -> None:
    seen_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, content=_sse('data: {"type":"message_stop"}'))

    provider = AnthropicProvider(api_key="k", transport=httpx.MockTransport(handler))
    async for _ in provider.stream(
        model="claude-opus-5", messages=MESSAGES, max_tokens=50, tools=[_TOOL]
    ):
        pass

    body = json.loads(seen_requests[0].content)
    assert body["tools"] == [
        {"name": _TOOL.name, "description": _TOOL.description, "input_schema": _TOOL.parameters}
    ]


async def test_anthropic_assembles_a_streamed_tool_call() -> None:
    """Input JSON arrives as incremental fragments (`input_json_delta`), keyed by content block
    index — reassembled into one ToolCallRequest at message_stop, with no Done alongside it."""
    body = _sse(
        'data: {"type":"message_start","message":{"usage":{"input_tokens":10}}}',
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"tool_use","id":"toolu_1","name":"get_weather"}}',
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"input_json_delta","partial_json":"{\\"city\\""}}',
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"input_json_delta","partial_json":":\\"Paris\\"}"}}',
        'data: {"type":"content_block_stop","index":0}',
        'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":5}}',
        'data: {"type":"message_stop"}',
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    provider = AnthropicProvider(api_key="k", transport=httpx.MockTransport(handler))
    chunks = [
        c
        async for c in provider.stream(
            model="claude-opus-5", messages=MESSAGES, max_tokens=50, tools=[_TOOL]
        )
    ]

    assert chunks == [
        Usage(input_tokens=10, output_tokens=5),
        ToolCallRequest(calls=(ToolCall(id="toolu_1", name="get_weather", arguments={"city": "Paris"}),)),
    ]


async def test_anthropic_replays_a_tool_call_and_its_result() -> None:
    """A past assistant tool_calls turn becomes a tool_use content block; the tool turn answering
    it becomes a user turn carrying a tool_result block — Anthropic has no "tool" role of its own."""
    seen_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, content=_sse('data: {"type":"message_stop"}'))

    provider = AnthropicProvider(api_key="k", transport=httpx.MockTransport(handler))
    async for _ in provider.stream(
        model="claude-opus-5", messages=_TOOL_CALL_MESSAGES, max_tokens=50
    ):
        pass

    messages = json.loads(seen_requests[0].content)["messages"]
    assert messages[1] == {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {"city": "Paris"}}],
    }
    assert messages[2] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "Sunny, 22C"}],
    }


async def test_google_sends_no_tools_key_when_none_are_offered() -> None:
    seen_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, content=_sse('data: {"candidates":[]}'))

    provider = GoogleProvider(api_key="k", transport=httpx.MockTransport(handler))
    async for _ in provider.stream(model="gemini-2.5-pro", messages=MESSAGES, max_tokens=50):
        pass

    assert "tools" not in json.loads(seen_requests[0].content)


async def test_google_sends_the_tool_definition_when_offered() -> None:
    seen_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, content=_sse('data: {"candidates":[]}'))

    provider = GoogleProvider(api_key="k", transport=httpx.MockTransport(handler))
    async for _ in provider.stream(
        model="gemini-2.5-pro", messages=MESSAGES, max_tokens=50, tools=[_TOOL]
    ):
        pass

    body = json.loads(seen_requests[0].content)
    assert body["tools"] == [
        {
            "functionDeclarations": [
                {"name": _TOOL.name, "description": _TOOL.description, "parameters": _TOOL.parameters}
            ]
        }
    ]


async def test_google_assembles_a_streamed_tool_call() -> None:
    """Google returns a whole functionCall in one part, no id of its own — this adapter
    synthesizes one so its output still fits the shared ToolCall shape."""
    body = _sse(
        'data: {"candidates":[{"content":{"parts":['
        '{"functionCall":{"name":"get_weather","args":{"city":"Paris"}}}]}}]}',
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    provider = GoogleProvider(api_key="k", transport=httpx.MockTransport(handler))
    chunks = [
        c
        async for c in provider.stream(
            model="gemini-2.5-pro", messages=MESSAGES, max_tokens=50, tools=[_TOOL]
        )
    ]

    assert len(chunks) == 1
    assert isinstance(chunks[0], ToolCallRequest)
    assert len(chunks[0].calls) == 1
    assert chunks[0].calls[0].name == "get_weather"
    assert chunks[0].calls[0].arguments == {"city": "Paris"}


async def test_google_replays_a_tool_call_and_its_result() -> None:
    """A past assistant tool_calls turn becomes a functionCall part; the tool turn answering it
    becomes a user turn carrying a functionResponse part matched by name, not id."""
    seen_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, content=_sse('data: {"candidates":[]}'))

    provider = GoogleProvider(api_key="k", transport=httpx.MockTransport(handler))
    async for _ in provider.stream(
        model="gemini-2.5-pro", messages=_TOOL_CALL_MESSAGES, max_tokens=50
    ):
        pass

    contents = json.loads(seen_requests[0].content)["contents"]
    assert contents[1] == {
        "role": "model",
        "parts": [{"functionCall": {"name": "get_weather", "args": {"city": "Paris"}}}],
    }
    assert contents[2] == {
        "role": "user",
        "parts": [{"functionResponse": {"name": "get_weather", "response": {"result": "Sunny, 22C"}}}],
    }
