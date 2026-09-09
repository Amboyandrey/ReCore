"""How one tool call turned out — whether the model's next turn is reading a real result or an
error message standing in for one."""

import enum


class ToolInvocationStatus(enum.StrEnum):
    SUCCESS = "success"
    ERROR = "error"
