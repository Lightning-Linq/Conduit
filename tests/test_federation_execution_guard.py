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
from conduit.api.routers.marketplace import RequestExecutionRequest
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


class TestExecutionGuard:
    async def test_rejects_cached_remote_skill(self, monkeypatch):
        monkeypatch.setattr(mkt.settings, "federation_enabled", True)

        async def yes(session, skill_id):
            return True

        monkeypatch.setattr(mkt, "is_cached_skill", yes)
        req = RequestExecutionRequest(skill_id=str(uuid.uuid4()))
        with pytest.raises(HTTPException) as exc:
            await mkt.request_skill_execution(req, session=AsyncMock())
        assert exc.value.status_code == 501
        assert "Federation #3" in exc.value.detail

    async def test_local_skill_passes_guard(self, monkeypatch):
        # Not cached -> guard stays silent; falls through to the normal local lookup,
        # which 404s here (mocked session returns no row) — proving no over-fire.
        monkeypatch.setattr(mkt.settings, "federation_enabled", True)

        async def no(session, skill_id):
            return False

        monkeypatch.setattr(mkt, "is_cached_skill", no)
        req = RequestExecutionRequest(skill_id=str(uuid.uuid4()))
        with pytest.raises(HTTPException) as exc:
            await mkt.request_skill_execution(req, session=_session_scalar(None))
        assert exc.value.status_code == 404
