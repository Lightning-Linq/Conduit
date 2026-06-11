"""Contract-level tests: payment gate, dispatch, output shape — via the real ASGI app."""


def test_health(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_catalog_lists_batch_one(client):
    names = {s["name"] for s in client.get("/skills").json()["skills"]}
    assert {"hash-digest", "text-stats", "json-csv", "entity-extract"} <= names


def test_unknown_skill_404(client, paid):
    r = client.post("/skills/does-not-exist", json=paid("does-not-exist", {}))
    assert r.status_code == 404


def test_missing_payment_proof_402(client):
    body = {"execution_id": "e1", "skill_name": "hash-digest", "input_data": {"text": "x"}}
    r = client.post("/skills/hash-digest", json=body)
    assert r.status_code == 402


def test_wrong_preimage_402(client):
    body = {
        "execution_id": "e1",
        "skill_name": "hash-digest",
        "input_data": {"text": "x"},
        "payment_proof": {"payment_hash": "ab" * 32, "payment_preimage": "cd" * 32},
    }
    r = client.post("/skills/hash-digest", json=body)
    assert r.status_code == 402


def test_output_envelope(client, paid):
    r = client.post("/skills/hash-digest", json=paid("hash-digest", {"text": "hi"}))
    assert r.status_code == 200
    assert "output" in r.json()


def test_execute_dispatches_by_body_name(client, paid):
    r = client.post("/execute", json=paid("text-stats", {"text": "one two three"}))
    assert r.status_code == 200
    assert r.json()["output"]["words"] == 3


def test_skill_input_error_is_400(client, paid):
    r = client.post("/skills/hash-digest", json=paid("hash-digest", {}))  # missing text
    assert r.status_code == 400
