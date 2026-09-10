"""Request and response shapes for the memory layer (ReMind). The mem0 API key never appears in
any response — only in the request that sets it, same as every other secret in this codebase."""

from typing import Literal

from pydantic import BaseModel, Field

MemoryScope = Literal["curated", "personal"]


class MemoryCredentialSet(BaseModel):
    """The one thing setting or rotating the workspace's mem0 key needs."""

    api_key: str = Field(min_length=1)


class MemoryCredentialOut(BaseModel):
    """Whether the workspace has a mem0 key configured — never the key itself."""

    has_key: bool


class CuratedMemoryCreate(BaseModel):
    """A fact to teach an assistant directly, stored verbatim rather than interpreted."""

    text: str = Field(min_length=1)


class MemoryOut(BaseModel):
    """One memory, as mem0 reports it back — pass-through fields a listing page renders."""

    id: str
    memory: str
    created_at: str | None = None


class MemoryQueuedOut(BaseModel):
    """What adding a memory returns — mem0's own `add` is asynchronous, so this confirms the
    request was accepted, not that the memory has finished being written."""

    queued: bool = True
