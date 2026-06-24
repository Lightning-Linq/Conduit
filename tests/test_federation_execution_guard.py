"""Tests for the Federation #2 cross-node execution guard (Task 7).

Discovery is federated, but execution + payment routing across nodes is Federation #3.
A request to execute a cached (remote) skill must be rejected with a clear error, not
treated as local. is_cached_skill is the detector; the guard fires before any invoice work.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from conduit.api.routers import marketplace as mkt
from conduit.models.skill import Skill
from conduit.services.federation_catalog import is_cached_skill


def _session_scalar(value) -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    session.execute = AsyncMock(return_value=result)
    return session


class TestIsCachedSkill:
    async def test_true_when_row_exists(self):
        assert await is_cached_skill(_session_scalar("skill-x"), "skill-x") is True

    async def test_false_when_absent(self):
        assert await is_cached_skill(_session_scalar(None), "missing") is False


def _local_skill() -> Skill:
    return Skill(
        id=uuid.uuid4(), provider_name="Me", name="Local", description="d",
        category="data", price_sats=0, is_active=True,
    )


class TestExecutionGuard:
    async def test_local_skill_wins_even_if_cached(self, monkeypatch):
        # The blocker fix: a remote node shadowing a local skill's (public) UUID must
        # NOT block its local execution. Local lookup wins; the guard never fires.
        monkeypatch.setattr(mkt.settings, "federation_enabled", True)

        async def shadow_exists(session, skill_id):
            return True

        monkeypatch.setattr(mkt, "is_cached_skill", shadow_exists)
        local = _local_skill()
        got = await mkt._resolve_local_skill_or_error(
            _session_scalar(local), str(uuid.uuid4())
        )
        assert got is local  # no 501 despite the shadow

    async def test_remote_only_skill_is_cross_node(self, monkeypatch):
        # Not local, but cached from a peer -> a clear Federation #3 error (501).
        monkeypatch.setattr(mkt.settings, "federation_enabled", True)

        async def yes(session, skill_id):
            return True

        monkeypatch.setattr(mkt, "is_cached_skill", yes)
        with pytest.raises(HTTPException) as exc:
            await mkt._resolve_local_skill_or_error(_session_scalar(None), str(uuid.uuid4()))
        assert exc.value.status_code == 501 and "Federation #3" in exc.value.detail

    async def test_unknown_skill_is_404(self, monkeypatch):
        # Neither local nor cached -> the normal 404 (not converted to 501).
        monkeypatch.setattr(mkt.settings, "federation_enabled", True)

        async def no(session, skill_id):
            return False

        monkeypatch.setattr(mkt, "is_cached_skill", no)
        with pytest.raises(HTTPException) as exc:
            await mkt._resolve_local_skill_or_error(_session_scalar(None), str(uuid.uuid4()))
        assert exc.value.status_code == 404
