"""Shared fixtures: a TestClient over the real app and a valid-payment payload factory."""

import hashlib

import pytest
from app.main import app
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def paid():
    """Factory for a Conduit webhook body whose preimage hashes to its payment_hash."""

    def _make(skill_name: str, input_data: dict, preimage: str = "11" * 32) -> dict:
        payment_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
        return {
            "execution_id": "exec-test-1",
            "skill_name": skill_name,
            "input_data": input_data,
            "payment_proof": {"payment_hash": payment_hash, "payment_preimage": preimage},
            "timestamp": "2026-06-11T00:00:00Z",
        }

    return _make
