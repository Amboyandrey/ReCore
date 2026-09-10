"""The memory layer (ReMind): scope encoding, the workspace's mem0 credential, curated vs.
personal memories, and — the part that matters most — that one user's personal memories can never
be listed, searched into, or deleted by anyone else, owner included.
"""

import uuid

import pytest
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import EncryptedSecret, decrypt_secret
from app.core.errors import InsufficientRole, MemoryNotConfigured, MemoryNotFound, MemoryUpstreamError
from app.memory import mem0
from app.models import Assistant, FeatureFlag, FlagScope, Role, User, Workspace, WorkspaceMember
from app.services.assistants import create_assistant
from app.services.flags import set_override
from app.services.memory import (
    add_curated,
    curated_agent_id,
    delete_credential,
    delete_memory,
    get_credential_row,
    has_credential,
    list_curated,
    list_personal,
    personal_agent_id,
    record_turn,
    retrieve_for_turn,
    set_credential,
    user_entity_id,
)

OWNER = {"email": "owner@example.com", "password": "correct horse battery staple"}


async def _owner_with_workspace(client: AsyncClient) -> str:
    """Sign up and log in as the owner, create a workspace, and return its id."""
    await client.post("/api/v1/auth/signup", json=OWNER)
    await client.post("/api/v1/auth/login", json=OWNER)
    created = await client.post("/api/v1/workspaces", json={"name": "Acme"})
    return str(created.json()["id"])


async def _enable_memory_flag(db: AsyncSession, redis: Redis, *, workspace_id: str) -> None:
    """Flip the `memory` flag on for one workspace — the same lever an admin's flag panel pulls."""
    flag = await db.scalar(select(FeatureFlag).where(FeatureFlag.key == "memory"))
    assert flag is not None
    await set_override(
        db, redis, flag_id=flag.id, scope=FlagScope.WORKSPACE, scope_id=uuid.UUID(workspace_id), value=True
    )
    await db.commit()  # this test's `db` session must commit for the client's own connection to see it


async def _user_with_workspace(db: AsyncSession, *, email: str, slug: str) -> tuple[User, Workspace]:
    user = User(email=email, password_hash="hashed")
    db.add(user)
    await db.flush()
    workspace = Workspace(slug=slug, name=slug, owner_id=user.id)
    db.add(workspace)
    await db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=Role.OWNER))
    await db.flush()
    return user, workspace


async def _member(db: AsyncSession, *, workspace_id: uuid.UUID, email: str) -> User:
    user = User(email=email, password_hash="hashed")
    db.add(user)
    await db.flush()
    db.add(WorkspaceMember(workspace_id=workspace_id, user_id=user.id, role=Role.MEMBER))
    await db.flush()
    return user


async def _assistant(db: AsyncSession, *, workspace_id: uuid.UUID, created_by: User) -> Assistant:
    return await create_assistant(
        db,
        workspace_id=workspace_id,
        created_by=created_by,
        name="Bot",
        instructions="Be helpful.",
        model_id=None,
        tool_ids=[],
        memory_enabled=True,
    )


async def _set_key(db: AsyncSession, *, workspace_id: uuid.UUID, created_by: User) -> None:
    await set_credential(db, workspace_id=workspace_id, created_by=created_by, api_key="m0-test-key")


# ---------- Scope encoding ----------


def test_curated_and_personal_use_distinct_namespaces() -> None:
    """Different agent_id values, not one shared namespace distinguished by user_id — see the
    module docstring in services/memory.py for why that distinction matters."""
    ws, aid = uuid.uuid4(), uuid.uuid4()
    curated = curated_agent_id(ws, aid)
    personal = personal_agent_id(ws, aid)

    assert curated != personal
    assert "curated" in curated
    assert "curated" not in personal


def test_user_entity_id_is_scoped_to_workspace_and_assistant() -> None:
    """The assistant is folded into this value itself — see its own docstring for why personal
    scoping doesn't rely on a separate agent_id tag the way curated does."""
    ws1, ws2, aid1, aid2, user = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert user_entity_id(ws1, aid1, user) != user_entity_id(ws2, aid1, user)
    assert user_entity_id(ws1, aid1, user) != user_entity_id(ws1, aid2, user)


# ---------- Credential ----------


async def test_set_and_check_credential(db: AsyncSession) -> None:
    user, workspace = await _user_with_workspace(db, email="cred1@example.com", slug="cred-ws1")

    assert await has_credential(db, workspace_id=workspace.id) is False
    await set_credential(db, workspace_id=workspace.id, created_by=user, api_key="m0-abc123")
    assert await has_credential(db, workspace_id=workspace.id) is True

    row = await get_credential_row(db, workspace_id=workspace.id)
    assert row is not None
    assert decrypt_secret(EncryptedSecret(row.ciphertext, row.nonce, row.wrapped_key)) == "m0-abc123"


async def test_setting_a_credential_again_rotates_it_in_place(db: AsyncSession) -> None:
    user, workspace = await _user_with_workspace(db, email="cred2@example.com", slug="cred-ws2")
    await set_credential(db, workspace_id=workspace.id, created_by=user, api_key="m0-old")

    await set_credential(db, workspace_id=workspace.id, created_by=user, api_key="m0-new")

    row = await get_credential_row(db, workspace_id=workspace.id)
    assert row is not None
    assert decrypt_secret(EncryptedSecret(row.ciphertext, row.nonce, row.wrapped_key)) == "m0-new"


async def test_deleting_a_credential_removes_it(db: AsyncSession) -> None:
    user, workspace = await _user_with_workspace(db, email="cred3@example.com", slug="cred-ws3")
    await set_credential(db, workspace_id=workspace.id, created_by=user, api_key="m0-x")

    await delete_credential(db, workspace_id=workspace.id)

    assert await has_credential(db, workspace_id=workspace.id) is False


# ---------- Curated memories ----------


async def test_add_curated_sends_infer_false_and_immutable_true(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, workspace = await _user_with_workspace(db, email="cur1@example.com", slug="cur-ws1")
    assistant = await _assistant(db, workspace_id=workspace.id, created_by=user)
    await _set_key(db, workspace_id=workspace.id, created_by=user)
    captured: dict[str, object] = {}

    async def fake_add(**kwargs: object) -> bool:
        captured.update(kwargs)
        return True

    monkeypatch.setattr(mem0, "add", fake_add)

    await add_curated(
        db,
        workspace_id=workspace.id,
        assistant_id=assistant.id,
        caller_id=user.id,
        is_owner=False,
        text="Always answer in French.",
    )

    assert captured["infer"] is False
    assert captured["immutable"] is True
    assert captured["agent_id"] == curated_agent_id(workspace.id, assistant.id)
    assert captured["messages"] == [{"role": "user", "content": "Always answer in French."}]


async def test_add_curated_rejects_a_non_creator_non_owner(db: AsyncSession) -> None:
    owner, workspace = await _user_with_workspace(db, email="cur2@example.com", slug="cur-ws2")
    assistant = await _assistant(db, workspace_id=workspace.id, created_by=owner)
    await _set_key(db, workspace_id=workspace.id, created_by=owner)
    other = await _member(db, workspace_id=workspace.id, email="cur2b@example.com")

    with pytest.raises(InsufficientRole):
        await add_curated(
            db,
            workspace_id=workspace.id,
            assistant_id=assistant.id,
            caller_id=other.id,
            is_owner=False,
            text="Sneaky fact.",
        )


async def test_add_curated_allows_a_workspace_owner_who_is_not_the_creator(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    creator, workspace = await _user_with_workspace(db, email="cur3@example.com", slug="cur-ws3")
    assistant = await _assistant(db, workspace_id=workspace.id, created_by=creator)
    await _set_key(db, workspace_id=workspace.id, created_by=creator)
    owner_member = await _member(db, workspace_id=workspace.id, email="cur3b@example.com")

    async def fake_add(**kwargs: object) -> bool:
        return True

    monkeypatch.setattr(mem0, "add", fake_add)

    # is_owner=True stands in for the router's ctx.role == Role.OWNER check.
    await add_curated(
        db,
        workspace_id=workspace.id,
        assistant_id=assistant.id,
        caller_id=owner_member.id,
        is_owner=True,
        text="Owner-added fact.",
    )  # no raise


async def test_add_curated_without_a_credential_raises(db: AsyncSession) -> None:
    user, workspace = await _user_with_workspace(db, email="cur4@example.com", slug="cur-ws4")
    assistant = await _assistant(db, workspace_id=workspace.id, created_by=user)

    with pytest.raises(MemoryNotConfigured):
        await add_curated(
            db, workspace_id=workspace.id, assistant_id=assistant.id,
            caller_id=user.id, is_owner=False, text="x",
        )


async def test_add_curated_raises_when_mem0_rejects_the_request(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deliberate, user-initiated action — unlike the chat pipeline's own writes, this must not
    silently report success (mem0.add() returning False, e.g. an invalid API key) as if the fact
    were actually queued. Caught live: the first version of this let exactly that happen."""
    user, workspace = await _user_with_workspace(db, email="cur6@example.com", slug="cur-ws6")
    assistant = await _assistant(db, workspace_id=workspace.id, created_by=user)
    await _set_key(db, workspace_id=workspace.id, created_by=user)

    async def rejecting_add(**kwargs: object) -> bool:
        del kwargs
        return False  # what mem0.add() itself returns on a non-2xx or transport failure

    monkeypatch.setattr(mem0, "add", rejecting_add)

    with pytest.raises(MemoryUpstreamError):
        await add_curated(
            db, workspace_id=workspace.id, assistant_id=assistant.id,
            caller_id=user.id, is_owner=False, text="x",
        )


async def test_list_curated_raises_on_a_mem0_outage(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed list must not be indistinguishable from a genuinely empty one — the settings page
    needs to tell "nothing here yet" apart from "couldn't check"."""
    user, workspace = await _user_with_workspace(db, email="cur7@example.com", slug="cur-ws7")
    assistant = await _assistant(db, workspace_id=workspace.id, created_by=user)
    await _set_key(db, workspace_id=workspace.id, created_by=user)

    async def failing_list(**kwargs: object) -> None:
        del kwargs
        return None

    monkeypatch.setattr(mem0, "list_memories", failing_list)

    with pytest.raises(MemoryUpstreamError):
        await list_curated(db, workspace_id=workspace.id, assistant_id=assistant.id)


async def test_list_curated_is_visible_to_any_member(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Viewing curated memories isn't restricted to the creator or owner — only adding/deleting is."""
    creator, workspace = await _user_with_workspace(db, email="cur5@example.com", slug="cur-ws5")
    assistant = await _assistant(db, workspace_id=workspace.id, created_by=creator)
    await _set_key(db, workspace_id=workspace.id, created_by=creator)
    other = await _member(db, workspace_id=workspace.id, email="cur5b@example.com")
    del other  # the point is list_curated takes no caller-identity param to restrict by

    async def fake_list(**kwargs: object) -> list[dict[str, object]]:
        return [{"id": "m1", "memory": "fact one"}]

    monkeypatch.setattr(mem0, "list_memories", fake_list)

    rows = await list_curated(db, workspace_id=workspace.id, assistant_id=assistant.id)
    assert rows == [{"id": "m1", "memory": "fact one"}]


# ---------- Personal memories — isolation ----------


async def test_list_personal_only_ever_queries_the_caller_s_own_entity(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, workspace = await _user_with_workspace(db, email="pers1@example.com", slug="pers-ws1")
    assistant = await _assistant(db, workspace_id=workspace.id, created_by=owner)
    await _set_key(db, workspace_id=workspace.id, created_by=owner)
    user_b = await _member(db, workspace_id=workspace.id, email="pers1b@example.com")
    captured: dict[str, object] = {}

    async def fake_list(**kwargs: object) -> list[dict[str, object]]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(mem0, "list_memories", fake_list)

    await list_personal(db, workspace_id=workspace.id, assistant_id=assistant.id, user_id=user_b.id)

    filters = captured["filters"]
    assert filters == {"user_id": user_entity_id(workspace.id, assistant.id, user_b.id)}
    assert user_entity_id(workspace.id, assistant.id, owner.id) not in str(filters)


async def test_a_workspace_owner_cannot_delete_another_member_s_personal_memory(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Personal scope ignores is_owner entirely — the confirmed, strictest reading of "strictly
    private, owners included": deleting one always means the caller's own entity, full stop."""
    owner, workspace = await _user_with_workspace(db, email="pers2@example.com", slug="pers-ws2")
    assistant = await _assistant(db, workspace_id=workspace.id, created_by=owner)
    await _set_key(db, workspace_id=workspace.id, created_by=owner)
    user_b = await _member(db, workspace_id=workspace.id, email="pers2b@example.com")

    # user_b's own memory exists in mem0...
    async def fake_list(**kwargs: object) -> list[dict[str, object]]:
        filters = kwargs["filters"]
        # ...but the owner's delete call scopes the list to the *owner's own* entity, which never
        # matches user_b's memory — simulating mem0 correctly reporting nothing for that filter.
        if user_entity_id(workspace.id, assistant.id, user_b.id) in str(filters):
            return [{"id": "mem-b-1", "memory": "user b's secret"}]
        return []

    monkeypatch.setattr(mem0, "list_memories", fake_list)

    with pytest.raises(MemoryNotFound):
        await delete_memory(
            db,
            workspace_id=workspace.id,
            assistant_id=assistant.id,
            memory_id="mem-b-1",
            scope="personal",
            caller_id=owner.id,  # the owner, not user_b
            is_owner=True,
        )


async def test_deleting_a_personal_memory_not_in_the_callers_own_list_404s(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verify-before-delete step: an id that isn't in *this caller's* own listing 404s, even
    if it's a perfectly real memory id that belongs to someone else."""
    owner, workspace = await _user_with_workspace(db, email="pers3@example.com", slug="pers-ws3")
    assistant = await _assistant(db, workspace_id=workspace.id, created_by=owner)
    await _set_key(db, workspace_id=workspace.id, created_by=owner)

    async def fake_list(**kwargs: object) -> list[dict[str, object]]:
        return [{"id": "mine-1", "memory": "my own fact"}]

    monkeypatch.setattr(mem0, "list_memories", fake_list)

    with pytest.raises(MemoryNotFound):
        await delete_memory(
            db, workspace_id=workspace.id, assistant_id=assistant.id,
            memory_id="not-mine", scope="personal", caller_id=owner.id, is_owner=True,
        )


async def test_deleting_a_curated_memory_that_exists_succeeds(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    creator, workspace = await _user_with_workspace(db, email="pers4@example.com", slug="pers-ws4")
    assistant = await _assistant(db, workspace_id=workspace.id, created_by=creator)
    await _set_key(db, workspace_id=workspace.id, created_by=creator)
    deleted: dict[str, object] = {}

    async def fake_list(**kwargs: object) -> list[dict[str, object]]:
        return [{"id": "c-1", "memory": "curated fact"}]

    async def fake_delete(**kwargs: object) -> bool:
        deleted.update(kwargs)
        return True

    monkeypatch.setattr(mem0, "list_memories", fake_list)
    monkeypatch.setattr(mem0, "delete", fake_delete)

    await delete_memory(
        db, workspace_id=workspace.id, assistant_id=assistant.id,
        memory_id="c-1", scope="curated", caller_id=creator.id, is_owner=False,
    )

    assert deleted == {"api_key": "m0-test-key", "memory_id": "c-1"}


async def test_deleting_a_memory_raises_when_mem0_cannot_be_reached_to_verify(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Distinct from MemoryNotFound: this is "couldn't even check," not "checked, wasn't there" —
    conflating the two would tell a user their memory doesn't exist when the truth is mem0 is
    just unreachable right now."""
    creator, workspace = await _user_with_workspace(db, email="pers6@example.com", slug="pers-ws6")
    assistant = await _assistant(db, workspace_id=workspace.id, created_by=creator)
    await _set_key(db, workspace_id=workspace.id, created_by=creator)

    async def failing_list(**kwargs: object) -> None:
        del kwargs
        return None

    monkeypatch.setattr(mem0, "list_memories", failing_list)

    with pytest.raises(MemoryUpstreamError):
        await delete_memory(
            db, workspace_id=workspace.id, assistant_id=assistant.id,
            memory_id="c-1", scope="curated", caller_id=creator.id, is_owner=False,
        )


async def test_deleting_a_memory_raises_when_the_delete_call_itself_fails(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    creator, workspace = await _user_with_workspace(db, email="pers7@example.com", slug="pers-ws7")
    assistant = await _assistant(db, workspace_id=workspace.id, created_by=creator)
    await _set_key(db, workspace_id=workspace.id, created_by=creator)

    async def fake_list(**kwargs: object) -> list[dict[str, object]]:
        del kwargs
        return [{"id": "c-1", "memory": "curated fact"}]

    async def failing_delete(**kwargs: object) -> bool:
        del kwargs
        return False

    monkeypatch.setattr(mem0, "list_memories", fake_list)
    monkeypatch.setattr(mem0, "delete", failing_delete)

    with pytest.raises(MemoryUpstreamError):
        await delete_memory(
            db, workspace_id=workspace.id, assistant_id=assistant.id,
            memory_id="c-1", scope="curated", caller_id=creator.id, is_owner=False,
        )


async def test_deleting_a_curated_memory_rejects_a_non_creator_non_owner(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    creator, workspace = await _user_with_workspace(db, email="pers5@example.com", slug="pers-ws5")
    assistant = await _assistant(db, workspace_id=workspace.id, created_by=creator)
    await _set_key(db, workspace_id=workspace.id, created_by=creator)
    other = await _member(db, workspace_id=workspace.id, email="pers5b@example.com")

    async def fake_list(**kwargs: object) -> list[dict[str, object]]:
        return [{"id": "c-1", "memory": "curated fact"}]

    monkeypatch.setattr(mem0, "list_memories", fake_list)

    with pytest.raises(InsufficientRole):
        await delete_memory(
            db, workspace_id=workspace.id, assistant_id=assistant.id,
            memory_id="c-1", scope="curated", caller_id=other.id, is_owner=False,
        )


# ---------- retrieve_for_turn: search filter shape, and post-filtering ----------


async def test_retrieve_for_turn_sends_the_expected_or_filter(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, workspace = await _user_with_workspace(db, email="ret1@example.com", slug="ret-ws1")
    assistant = await _assistant(db, workspace_id=workspace.id, created_by=owner)
    await _set_key(db, workspace_id=workspace.id, created_by=owner)
    captured: dict[str, object] = {}

    async def fake_search(**kwargs: object) -> list[dict[str, object]]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(mem0, "search", fake_search)

    await retrieve_for_turn(
        db, workspace_id=workspace.id, assistant_id=assistant.id, user_id=owner.id, query="hi"
    )

    filters = captured["filters"]
    curated_ns = curated_agent_id(workspace.id, assistant.id)
    entity = user_entity_id(workspace.id, assistant.id, owner.id)
    assert filters == {"OR": [{"agent_id": curated_ns}, {"user_id": entity}]}
    assert captured["rerank"] is True


async def test_retrieve_for_turn_drops_a_result_outside_the_caller_s_scopes(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if mem0's own filter had a bug and handed back another user's memory, the block built
    from it excludes anything whose agent_id/user_id doesn't match this exact caller's scopes."""
    owner, workspace = await _user_with_workspace(db, email="ret2@example.com", slug="ret-ws2")
    assistant = await _assistant(db, workspace_id=workspace.id, created_by=owner)
    await _set_key(db, workspace_id=workspace.id, created_by=owner)
    other_workspace_ns = curated_agent_id(uuid.uuid4(), uuid.uuid4())

    async def fake_search(**kwargs: object) -> list[dict[str, object]]:
        return [
            {"id": "leak-1", "memory": "someone else's fact", "agent_id": other_workspace_ns},
            {
                "id": "mine-1",
                "memory": "my own fact",
                "agent_id": personal_agent_id(workspace.id, assistant.id),
                "user_id": user_entity_id(workspace.id, assistant.id, owner.id),
            },
        ]

    monkeypatch.setattr(mem0, "search", fake_search)

    block = await retrieve_for_turn(
        db, workspace_id=workspace.id, assistant_id=assistant.id, user_id=owner.id, query="hi"
    )

    assert block is not None
    assert "my own fact" in block
    assert "someone else's fact" not in block


async def test_retrieve_for_turn_drops_a_personal_result_with_the_wrong_user_id(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent_id alone isn't enough for the personal scope — user_id must match too, or a mem0
    response mixing users under one namespace would leak across them."""
    owner, workspace = await _user_with_workspace(db, email="ret3@example.com", slug="ret-ws3")
    assistant = await _assistant(db, workspace_id=workspace.id, created_by=owner)
    await _set_key(db, workspace_id=workspace.id, created_by=owner)
    other_user_id = uuid.uuid4()

    async def fake_search(**kwargs: object) -> list[dict[str, object]]:
        return [
            {
                "id": "other-1",
                "memory": "belongs to someone else",
                "agent_id": personal_agent_id(workspace.id, assistant.id),
                "user_id": user_entity_id(workspace.id, assistant.id, other_user_id),
            }
        ]

    monkeypatch.setattr(mem0, "search", fake_search)

    block = await retrieve_for_turn(
        db, workspace_id=workspace.id, assistant_id=assistant.id, user_id=owner.id, query="hi"
    )

    assert block is None


async def test_retrieve_for_turn_finds_a_personal_memory_with_no_agent_id_tag_at_all(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact bug found live: mem0's own consolidation (what infer=True triggers) rewrites
    several raw statements into one new, tidied fact, and that new memory doesn't reliably carry
    forward every original agent_id tag — sometimes it has none at all. Personal-scope matching
    must not depend on that tag surviving, only on user_id (which mem0 does reliably keep)."""
    owner, workspace = await _user_with_workspace(db, email="ret6@example.com", slug="ret-ws6")
    assistant = await _assistant(db, workspace_id=workspace.id, created_by=owner)
    await _set_key(db, workspace_id=workspace.id, created_by=owner)

    async def fake_search(**kwargs: object) -> list[dict[str, object]]:
        del kwargs
        return [
            {
                "id": "consolidated-1",
                "memory": "User's bias (favorite TWICE member) is Sana",
                "user_id": user_entity_id(workspace.id, assistant.id, owner.id),
                # no agent_id at all — this is what mem0 actually returned live
            }
        ]

    monkeypatch.setattr(mem0, "search", fake_search)

    block = await retrieve_for_turn(
        db, workspace_id=workspace.id, assistant_id=assistant.id, user_id=owner.id, query="who is my bias"
    )

    assert block is not None
    assert "Sana" in block


async def test_retrieve_for_turn_returns_none_without_a_credential(db: AsyncSession) -> None:
    owner, workspace = await _user_with_workspace(db, email="ret4@example.com", slug="ret-ws4")
    assistant = await _assistant(db, workspace_id=workspace.id, created_by=owner)

    block = await retrieve_for_turn(
        db, workspace_id=workspace.id, assistant_id=assistant.id, user_id=owner.id, query="hi"
    )

    assert block is None


async def test_retrieve_for_turn_returns_none_on_a_mem0_outage(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, workspace = await _user_with_workspace(db, email="ret5@example.com", slug="ret-ws5")
    assistant = await _assistant(db, workspace_id=workspace.id, created_by=owner)
    await _set_key(db, workspace_id=workspace.id, created_by=owner)

    async def failing_search(**kwargs: object) -> None:
        return None  # what mem0.search() itself returns on any transport failure

    monkeypatch.setattr(mem0, "search", failing_search)

    block = await retrieve_for_turn(
        db, workspace_id=workspace.id, assistant_id=assistant.id, user_id=owner.id, query="hi"
    )

    assert block is None


# ---------- record_turn ----------


async def test_record_turn_writes_to_the_personal_namespace(
    db: AsyncSession, redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, workspace = await _user_with_workspace(db, email="rec1@example.com", slug="rec-ws1")
    assistant = await _assistant(db, workspace_id=workspace.id, created_by=owner)
    await _set_key(db, workspace_id=workspace.id, created_by=owner)
    await db.commit()  # record_turn opens its own session — needs this row committed to see it
    captured: dict[str, object] = {}

    async def fake_add(**kwargs: object) -> bool:
        captured.update(kwargs)
        return True

    monkeypatch.setattr(mem0, "add", fake_add)

    await record_turn(
        workspace_id=workspace.id,
        assistant_id=assistant.id,
        user_id=owner.id,
        user_message="Hello",
    )

    assert captured["agent_id"] == personal_agent_id(workspace.id, assistant.id)
    assert captured["user_id"] == user_entity_id(workspace.id, assistant.id, owner.id)
    # Plain infer=True — mem0's own classifier, with no bias fields and no pre-filtering of our
    # own layered on top. See record_turn's own docstring for why both of those were tried and
    # backed out.
    assert captured["infer"] is True
    assert captured["messages"] == [{"role": "user", "content": "Hello"}]


async def test_record_turn_is_a_no_op_without_a_credential(db: AsyncSession) -> None:
    owner, workspace = await _user_with_workspace(db, email="rec2@example.com", slug="rec-ws2")
    assistant = await _assistant(db, workspace_id=workspace.id, created_by=owner)
    await db.commit()

    # No mem0.add monkeypatch — if this tried to reach the real network it would time out and
    # fail the test; reaching the end without one proves it returned early.
    await record_turn(
        workspace_id=workspace.id, assistant_id=assistant.id, user_id=owner.id, user_message="Hello"
    )


# ---------- Router: flag gating ----------


async def test_memory_routes_404_while_the_flag_is_off(client: AsyncClient) -> None:
    workspace_id = await _owner_with_workspace(client)

    response = await client.put(
        f"/api/v1/workspaces/{workspace_id}/memory/credential", json={"api_key": "m0-x"}
    )

    assert response.status_code == 404


async def test_set_get_and_delete_credential_over_http(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    workspace_id = await _owner_with_workspace(client)
    await _enable_memory_flag(db, redis_client, workspace_id=workspace_id)

    set_resp = await client.put(
        f"/api/v1/workspaces/{workspace_id}/memory/credential", json={"api_key": "m0-real-key"}
    )
    assert set_resp.status_code == 200
    assert set_resp.json() == {"has_key": True}
    assert "m0-real-key" not in set_resp.text

    get_resp = await client.get(f"/api/v1/workspaces/{workspace_id}/memory/credential")
    assert get_resp.json() == {"has_key": True}

    delete_resp = await client.delete(f"/api/v1/workspaces/{workspace_id}/memory/credential")
    assert delete_resp.status_code == 204

    after = await client.get(f"/api/v1/workspaces/{workspace_id}/memory/credential")
    assert after.json() == {"has_key": False}
