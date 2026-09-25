"""What kind of tool a row is — what decides whether it's executed in-process, over HTTP, or
through an MCP server."""

import enum


class ToolKind(enum.StrEnum):
    BUILTIN = "builtin"
    HTTP = "http"
    MCP = "mcp"
