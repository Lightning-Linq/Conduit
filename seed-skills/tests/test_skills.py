"""Per-skill behaviour for the batch-one keyless skills."""


def test_hash_digest_known_vector(client, paid):
    r = client.post("/skills/hash-digest", json=paid("hash-digest", {"text": "hello world"}))
    out = r.json()["output"]
    assert out["algorithm"] == "sha256"
    assert out["hex"] == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"


def test_hash_digest_rejects_unknown_algorithm(client, paid):
    r = client.post(
        "/skills/hash-digest",
        json=paid("hash-digest", {"text": "x", "algorithm": "rot13"}),
    )
    assert r.status_code == 400


def test_text_stats_counts(client, paid):
    r = client.post("/skills/text-stats", json=paid("text-stats", {"text": "Hello world. Bye."}))
    out = r.json()["output"]
    assert out["words"] == 3
    assert out["sentences"] == 2


def test_json_to_csv(client, paid):
    rows = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
    r = client.post(
        "/skills/json-csv",
        json=paid("json-csv", {"direction": "json_to_csv", "data": rows}),
    )
    csv_text = r.json()["output"]["csv"]
    assert "a,b" in csv_text
    assert "1,2" in csv_text


def test_csv_to_json(client, paid):
    r = client.post(
        "/skills/json-csv",
        json=paid("json-csv", {"direction": "csv_to_json", "csv": "a,b\n1,2\n"}),
    )
    assert r.json()["output"]["data"] == [{"a": "1", "b": "2"}]


def test_entity_extract_finds_email_and_url(client, paid):
    text = "mail me at a@b.com or visit https://x.io today"
    r = client.post("/skills/entity-extract", json=paid("entity-extract", {"text": text}))
    out = r.json()["output"]
    assert "a@b.com" in out["emails"]
    assert "https://x.io" in out["urls"]
