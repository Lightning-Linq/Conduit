"""Conduit seed skills — a keyless reference webhook provider.

Implements the Conduit skill-execution contract (see :mod:`app.contract`): Conduit
POSTs a paid execution to ``/skills/{skill_name}`` (or ``/execute`` with the name in
the body); we verify the payment preimage, run the keyless skill, and return
``{"output": ...}``. Doubles as a public provider template — fork it, drop a module
under ``app/skills/``, and point your Conduit listing's ``endpoint_url`` at
``https://<host>/skills/<name>``.

Run: ``uvicorn app.main:app``  (set ``REQUIRE_PAYMENT_PROOF=false`` for local testing).
"""

from __future__ import annotations

import inspect
import os

from fastapi import FastAPI, HTTPException

from app import skills as _skills  # noqa: F401  — import populates REGISTRY
from app.contract import WebhookRequest
from app.payment import verify_payment_proof
from app.registry import REGISTRY, SkillError

REQUIRE_PAYMENT_PROOF = os.getenv("REQUIRE_PAYMENT_PROOF", "true").lower() != "false"

app = FastAPI(title="Conduit Seed Skills", version="0.1.0")


@app.get("/")
async def health() -> dict:
    """Liveness + a count of registered skills."""
    return {"status": "ok", "skills": len(REGISTRY)}


@app.get("/skills")
async def catalog() -> dict:
    """The skill catalog: names, descriptions, and example inputs."""
    return {
        "skills": [
            {"name": s.name, "description": s.description, "input_example": s.input_example}
            for s in REGISTRY.values()
        ]
    }


async def _dispatch(skill_name: str, req: WebhookRequest) -> dict:
    skill = REGISTRY.get(skill_name)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"unknown skill: {skill_name}")
    if REQUIRE_PAYMENT_PROOF:
        proof = req.payment_proof
        if proof is None or not verify_payment_proof(proof.payment_hash, proof.payment_preimage):
            raise HTTPException(status_code=402, detail="invalid or missing payment proof")
    try:
        result = skill.handler(req.input_data)
        if inspect.isawaitable(result):
            result = await result
    except SkillError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"output": result}


@app.post("/skills/{skill_name}")
async def execute_path(skill_name: str, req: WebhookRequest) -> dict:
    """Run a skill addressed by URL path (the canonical per-skill endpoint_url)."""
    return await _dispatch(skill_name, req)


@app.post("/execute")
async def execute_body(req: WebhookRequest) -> dict:
    """Run a skill addressed by ``skill_name`` in the body (one URL for all skills)."""
    return await _dispatch(req.skill_name, req)
