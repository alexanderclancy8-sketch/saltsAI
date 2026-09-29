"""Tool registry plus a small mock filesystem so the harness is runnable with no
real disk or network access - swap MockFS/the handlers for real ones to go live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


class ToolHandler(Protocol):
    def __call__(self, tool_input: dict[str, Any]) -> str: ...


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    is_write: bool = False  # True gates this tool behind the HITL approval gate

    def to_anthropic(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(f"no tool registered called {name!r}") from None

    def as_anthropic_tools(self) -> list[dict[str, Any]]:
        return [t.to_anthropic() for t in self._tools.values()]


class MockFS:
    """An in-memory 'filesystem' the mock tools read/write, so a demo run never
    touches the real disk. Seed it directly (`fs.files["config.json"] = "..."`)
    before a run."""

    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.files: dict[str, str] = dict(files or {})

    def read_file(self, tool_input: dict[str, Any]) -> str:
        path = tool_input["path"]
        if path not in self.files:
            return f"error: no such file {path!r}"
        return self.files[path]

    def write_file(self, tool_input: dict[str, Any]) -> str:
        path, content = tool_input["path"], tool_input["content"]
        self.files[path] = content
        return f"wrote {len(content)} bytes to {path}"

    def list_files(self, tool_input: dict[str, Any]) -> str:
        return "\n".join(sorted(self.files)) or "(empty)"


def build_default_registry(fs: MockFS) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(Tool(
        name="read_file",
        description="Read the full contents of a file by path.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        handler=fs.read_file,
        is_write=False,
    ))
    registry.register(Tool(
        name="list_files",
        description="List every file path that currently exists.",
        input_schema={"type": "object", "properties": {}},
        handler=fs.list_files,
        is_write=False,
    ))
    registry.register(Tool(
        name="write_file",
        description="Overwrite a file with new content, creating it if it doesn't exist. "
                     "This is a write operation and requires human approval.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
        handler=fs.write_file,
        is_write=True,
    ))
    return registry
