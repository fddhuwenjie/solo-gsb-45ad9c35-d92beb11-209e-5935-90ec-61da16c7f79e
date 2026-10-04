"""API tests: response shape, metrics, validation, 422 semantics."""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

PAYLOAD = {
    "plate": {"type": "96"},
    "samples": [{"id": f"s{i}", "group": f"G{i % 3}", "batch": f"B{i % 4}"} for i in range(20)],
    "fixed": [{"sample_id": "s0", "well": "C5"}],
    "blocked_wells": ["A1"],
    "seed": 42,
}


def test_arrangement_ok():
    r = client.post("/api/v1/arrangements", json=PAYLOAD)
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"seed", "plate", "layout", "metrics", "validation", "solver"}
    assert len(body["layout"]) == 20
    assert body["layout"]["s0"] == "C5"
    assert "A1" not in body["layout"].values()
    assert body["validation"]["all_passed"] is True
    assert "level1_group_balance" in body["metrics"]
    assert "level2_batch_adjacency" in body["metrics"]
    l1 = body["metrics"]["level1_group_balance"]
    assert l1["global_max_spread"] <= 1
    l2 = body["metrics"]["level2_batch_adjacency"]
    assert l2["same_batch_adjacent_pairs_count"] == len(l2["same_batch_adjacent_pairs"])


def test_determinism():
    r1 = client.post("/api/v1/arrangements", json=PAYLOAD).json()
    r2 = client.post("/api/v1/arrangements", json=PAYLOAD).json()
    assert r1["layout"] == r2["layout"]


def test_infeasible_returns_422_with_categories_and_no_layout():
    bad = {
        "plate": {"type": "24"},
        "samples": [{"id": f"x{i}", "group": "G", "batch": "B"} for i in range(25)],
        "seed": 1,
    }
    r = client.post("/api/v1/arrangements", json=bad)
    assert r.status_code == 422
    body = r.json()
    assert "CAPACITY_EXCEEDED" in body["infeasibility_categories"]
    assert "layout" not in body


def test_validation_error_is_422():
    r = client.post("/api/v1/arrangements", json={"samples": []})
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == ["REQUEST_VALIDATION_ERROR"]


def test_health():
    assert client.get("/health").json() == {"status": "ok"}
