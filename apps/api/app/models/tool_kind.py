"""What kind of tool a row is — what decides whether it's executed in-process or over HTTP."""

import enum


class ToolKind(enum.StrEnum):
    BUILTIN = "builtin"
    HTTP = "http"
