"""A scripted stand-in for the Anthropic client so the conversation loop can be tested offline."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def tool_block(name: str, tool_input: dict[str, Any], block_id: str = "toolu_1") -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=block_id, name=name, input=tool_input)


def message(content: list, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(content=content, stop_reason=stop_reason, parsed_output=None)


class _Stream:
    def __init__(self, msg: SimpleNamespace):
        self.msg = msg

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def gen():
            for block in self.msg.content:
                if block.type == "text":
                    for word in block.text.split(" "):
                        yield SimpleNamespace(type="text", text=word + " ")
                elif block.type == "tool_use":
                    yield SimpleNamespace(type="content_block_start", content_block=block)
        return gen()

    async def get_final_message(self):
        return self.msg


class FakeMessages:
    def __init__(self, script: list[SimpleNamespace] | None = None, default_text: str = "Certainly, sir."):
        self.script = list(script or [])
        self.default_text = default_text
        self.calls: list[dict[str, Any]] = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        msg = self.script.pop(0) if self.script else message([text_block(self.default_text)])
        return _Stream(msg)

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        schema = kwargs["output_format"]
        return SimpleNamespace(stop_reason="end_turn", parsed_output=schema.model_validate(self.parse_result))

    parse_result: dict[str, Any] = {}


class FakeClient:
    def __init__(self, script=None, default_text: str = "Certainly, sir."):
        self.beta = SimpleNamespace(messages=FakeMessages(script, default_text))
