"""Certificate retest sampling plan endpoint tests.

A plan is drawn from the *current* revision of an ACTIVE certificate: the
layout, grouping, scan and readings are restored from that revision's frozen
snapshot and only samples with a valid reading whose well is not excluded
may be selected. Group quotas are met exactly; subject to that, covered
rows+columns are maximized, then same-batch adjacent pairs minimized, then
the seed fixes a deterministic result. Reading the certificate, freezing the
plan and saving it with the idempotency record happens in one SQLite
transaction — a revoked certificate, a stale revision, insufficient samples
or an unsolvable constraint saves nothing. Same key + same content replays
the recorded response; same key + different content is 409.
"""
import concurrent.futures
import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import certificates
from app.main import app

client = TestClient(app)

# 4 rows x 4 cols, 16 wells, two groups interleaved checkerboard-style;
# integer noise gives small, well-converged row/column effects.
LAYOUT_4X4 = {
    "gA1": "A1", "gA2": "A3", "gB1": "A2", "gB2": "A4",
    "gB3": "B1", "gB4": "B3", "gA3": "B2", "gA4": "B4",
    "gA5": "C1", "gA6": "C3", "gB5": "C2", "gB6": "C4",
    "gB7": "D1", "gB8": "D3", "gA7": "D2", "gA8": "D4",
}
SAMPLES_4X4 = [
    {"id": s, "group": "GA" if s.startswith("gA") else "GB"}
    for s in LAYOUT_4X4
]
PLATE_4X4 = {"type": "custom", "rows": 4, "columns": 4}
GA_WELLS = {s: w for s, w in LAYOUT_4X4.items() if s.startswith("gA")}


@pytest.fixture(autouse=True)
def fresh_store(tmp_path, monkeypatch):
    """Isolate every test in its own SQLite database."""
    monkeypatch.setenv("CERT_DB_PATH", str(tmp_path / "certificates.db"))
    certificates.reset_stores()
    yield tmp_path / "certificates.db"
    certificates.reset_stores()


def _hash(text: str) -> int:
    return int(hashlib.md5(text.encode()).hexdigest()[:6], 16)


def _readings(row_bump=0.0):
    out = {}
    for sid, well in LAYOUT_4X4.items():
        base = 100.0 if sid.startswith("gA") else 0.0
        noise = (_hash(sid) % 7) - 3  # -3..3
        out[well] = base + (row_bump if well[0] == "A" else 0.0) + noise
    return out


def _release_payload(**over):
    payload = {
        "plate": PLATE_4X4,
        "target": dict(LAYOUT_4X4),
        "scan": {w: s for s, w in LAYOUT_4X4.items()},
        "blocked_wells": [],
        "samples": [dict(s) for s in SAMPLES_4X4],
        "readings": _readings(),
        "seed": 7,
        "max_spatial_effect": 10.0,
        "min_p_value": 0.001,
        "permutations": 100,
        "tolerance": 1e-6,
        "max_iterations": 100,
    }
    payload.update(over)
    return payload


def _create_cert(**release_over):
    r = client.post("/api/v1/certificates", json={
        "batch_id": "BATCH-1",
        "request_key": "create-1",
        "release": _release_payload(**release_over),
    })
    assert r.status_code == 201, r.text
    return r.json()


def _plan_payload(**over):
    payload = {
        "batch_id": "BATCH-1",
        "request_key": "plan-1",
        "expected_revision": 1,
        "group_counts": {"GA": 2, "GB": 2},
        "excluded_wells": [],
        "seed": 42,
    }
    payload.update(over)
    return payload


def _plan(**over):
    return client.post("/api/v1/certificates/retest-plan", json=_plan_payload(**over))


def _plan_rows(db_path):
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT plan_id, batch_id, certificate_id, revision, digest, "
        "request_key, seed, plan FROM retest_plans ORDER BY created_at, plan_id"
    ).fetchall()
    conn.close()
    return rows


# --------------------------------------------------------------------- happy


def test_plan_success_exact_quotas_and_full_coverage():
    created = _create_cert()
    r = _plan()
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["plan_id"].startswith("retest-")
    assert body["batch_id"] == "BATCH-1"
    assert body["certificate_id"] == created["certificate_id"]
    assert body["status"] == "ACTIVE"
    assert body["revision"] == 1
    assert body["digest"] == created["digest"]
    assert body["seed"] == 42
    assert body["group_counts"] == {"GA": 2, "GB": 2}

    # Every group quota is satisfied exactly.
    assert len(body["selected"]) == 4
    by_group = {}
    for entry in body["selected"]:
        by_group.setdefault(entry["group"], []).append(entry["sample_id"])
    assert len(by_group["GA"]) == 2
    assert len(by_group["GB"]) == 2
    for group in body["groups"]:
        assert group["requested"] == group["selected"]
        assert group["eligible"] == 8

    # 4 samples can cover all 4 rows and 4 columns with no adjacent pair.
    assert body["coverage"]["covered_rows"] == 4
    assert body["coverage"]["covered_columns"] == 4
    assert body["coverage"]["covered_total"] == 8
    assert body["adjacency"]["same_batch_adjacent_pairs_count"] == 0
    assert body["adjacency"]["same_batch_adjacent_pairs"] == []
    assert body["solver"]["status"] == "OPTIMAL"
    assert [s["stage"] for s in body["solver"]["stages"]] == [
        "1_coverage", "2_adjacency", "3_tiebreak",
    ]

    # Selected entries carry sample id, well and group; wells match layout.
    for entry in body["selected"]:
        assert LAYOUT_4X4[entry["sample_id"]] == entry["well"]
        assert entry["group"] == ("GA" if entry["sample_id"].startswith("gA") else "GB")


def test_plan_selected_wells_are_row_major_sorted():
    _create_cert()
    body = _plan().json()
    wells = [e["well"] for e in body["selected"]]
    assert wells == sorted(wells, key=lambda w: (w[0], int(w[1:])))


def test_plan_only_selects_samples_with_valid_readings():
    readings = _readings()
    readings["A1"] = None  # gA1 loses its valid reading
    del readings["A3"]     # gA2's reading is missing entirely
    _create_cert(readings=readings)
    r = _plan(group_counts={"GA": 2})
    assert r.status_code == 201, r.text
    selected_ids = {e["sample_id"] for e in r.json()["selected"]}
    assert "gA1" not in selected_ids
    assert "gA2" not in selected_ids
    groups = {g["group"]: g for g in r.json()["groups"]}
    assert groups["GA"]["eligible"] == 6  # 8 GA samples minus the two unread


def test_plan_respects_excluded_wells_case_insensitively():
    _create_cert()
    r = _plan(group_counts={"GA": 2}, excluded_wells=["a1", "B2", "C3"])
    assert r.status_code == 201, r.text
    body = r.json()
    wells = {e["well"] for e in body["selected"]}
    assert wells.isdisjoint({"A1", "B2", "C3"})
    # Excluded wells are echoed back in canonical (uppercase) form.
    assert body["excluded_wells"] == ["A1", "B2", "C3"]
    groups = {g["group"]: g for g in body["groups"]}
    assert groups["GA"]["eligible"] == 5  # A1, B2, C3 all held GA samples


def test_plan_coverage_is_maximized_before_adjacency():
    _create_cert()
    # GA-eligible wells become {A1, A3, B2}; picking {A1, A3} would cover a
    # single row, so the optimum must include B2 (2 rows + 2 columns).
    r = _plan(group_counts={"GA": 2},
              excluded_wells=["B4", "C1", "C3", "D2", "D4"],
              request_key="plan-cov")
    assert r.status_code == 201, r.text
    body = r.json()
    selected_ids = {e["sample_id"] for e in body["selected"]}
    assert "gA3" in selected_ids  # the sample at B2
    assert len(selected_ids) == 2
    assert body["coverage"]["covered_rows"] == 2
    assert body["coverage"]["covered_columns"] == 2
    assert body["coverage"]["covered_total"] == 4
    assert body["adjacency"]["same_batch_adjacent_pairs_count"] == 0


def test_plan_adjacency_is_minimized_within_max_coverage():
    _create_cert()
    # Eligible wells: A1(gA1/GA), A2(gB1/GB), B2(gA3/GA), B3(gB4/GB).
    # Max coverage is 4 via {A1,B3} (0 adjacent pairs) or {B2,B3} (1 pair);
    # the adjacency level must choose {A1, B3}.
    excluded = ["A3", "A4", "B1", "B4", "C1", "C2", "C3", "C4",
                "D1", "D2", "D3", "D4"]
    r = _plan(group_counts={"GA": 1, "GB": 1}, excluded_wells=excluded,
              request_key="plan-adj")
    assert r.status_code == 201, r.text
    body = r.json()
    assert {e["sample_id"] for e in body["selected"]} == {"gA1", "gB4"}
    assert body["coverage"]["covered_total"] == 4
    assert body["adjacency"]["same_batch_adjacent_pairs_count"] == 0


def test_plan_adjacency_reports_unavoidable_pair():
    _create_cert()
    # Eligible wells: only the 2x2 block A1, A2, B1, B2. Every GA+GB pick is
    # one adjacent pair covering 3 row/column lines.
    excluded = ["A3", "A4", "B3", "B4", "C1", "C2", "C3", "C4",
                "D1", "D2", "D3", "D4"]
    r = _plan(group_counts={"GA": 1, "GB": 1}, excluded_wells=excluded,
              request_key="plan-adj1")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["coverage"]["covered_total"] == 3
    assert body["adjacency"]["same_batch_adjacent_pairs_count"] == 1
    pair = body["adjacency"]["same_batch_adjacent_pairs"][0]
    assert pair == sorted(pair)
    selected_wells = {e["well"] for e in body["selected"]}
    assert set(pair) == selected_wells


def test_plan_is_deterministic_for_same_seed_and_input():
    _create_cert()
    b1 = _plan(request_key="plan-a", seed=9).json()
    b2 = _plan(request_key="plan-b", seed=9).json()
    # Different idempotency keys -> different plan ids, identical content.
    assert b1["plan_id"] != b2["plan_id"]
    for field in ("selected", "groups", "coverage", "adjacency", "group_counts"):
        assert b1[field] == b2[field]


def test_plan_seed_is_echoed_and_defaults_to_zero():
    _create_cert()
    payload = _plan_payload(request_key="plan-noseed")
    del payload["seed"]
    r = client.post("/api/v1/certificates/retest-plan", json=payload)
    assert r.status_code == 201
    assert r.json()["seed"] == 0


# --------------------------------------------------------------- persistence


def test_plan_is_persisted_with_revision_and_digest(fresh_store):
    created = _create_cert()
    body = _plan().json()
    rows = _plan_rows(fresh_store)
    assert len(rows) == 1
    plan_id, batch_id, cert_id, revision, digest, key, seed, plan_json = rows[0]
    assert plan_id == body["plan_id"]
    assert batch_id == "BATCH-1"
    assert cert_id == created["certificate_id"]
    assert revision == 1
    assert digest == created["digest"]
    assert key == "plan-1"
    assert seed == 42
    stored = json.loads(plan_json)
    # The frozen plan is the response minus the solver metadata.
    assert stored == {k: v for k, v in body.items() if k != "solver"}


def test_plan_does_not_change_certificate_state(fresh_store):
    _create_cert()
    _plan()
    # The certificate is untouched: still ACTIVE at revision 1, so a
    # re-verification against revision 1 succeeds and no plan row is added.
    r = client.post("/api/v1/certificates/reverify", json={
        "batch_id": "BATCH-1",
        "request_key": "reverify-1",
        "expected_revision": 1,
        "scan": {w: s for s, w in LAYOUT_4X4.items()},
        "readings": _readings(),
    })
    assert r.status_code == 200
    assert r.json()["revision"] == 2
    assert len(_plan_rows(fresh_store)) == 1


def test_plan_survives_restart(fresh_store):
    _create_cert()
    first = _plan().json()
    certificates.reset_stores()  # simulate a service restart
    replay = _plan()
    assert replay.status_code == 201
    assert replay.json() == first
    assert len(_plan_rows(fresh_store)) == 1


# ------------------------------------------------------------------ revision


def test_plan_stale_revision_is_409_after_reverify(fresh_store):
    _create_cert()
    r = client.post("/api/v1/certificates/reverify", json={
        "batch_id": "BATCH-1",
        "request_key": "reverify-1",
        "expected_revision": 1,
        "scan": {w: s for s, w in LAYOUT_4X4.items()},
        "readings": _readings(row_bump=0.5),
    })
    assert r.status_code == 200 and r.json()["revision"] == 2

    # A request pinned to the old revision is rejected and saves nothing.
    r = _plan(expected_revision=1)
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "CERTIFICATE_REVISION_MISMATCH"
    assert body["current_revision"] == 2
    assert body["expected_revision"] == 1
    assert _plan_rows(fresh_store) == []

    # The current revision plans successfully.
    r = _plan(expected_revision=2, request_key="plan-2")
    assert r.status_code == 201
    assert r.json()["revision"] == 2


def test_plan_uses_current_revision_snapshot():
    # Re-verify with gA1's reading nulled: revision 2's eligible pool shrinks.
    _create_cert()
    readings = _readings()
    readings["A1"] = None
    r = client.post("/api/v1/certificates/reverify", json={
        "batch_id": "BATCH-1",
        "request_key": "reverify-1",
        "expected_revision": 1,
        "scan": {w: s for s, w in LAYOUT_4X4.items()},
        "readings": readings,
    })
    assert r.status_code == 200, r.text
    digest_v2 = r.json()["digest"]

    # GA now has exactly 7 eligible samples; a quota of 7 must draw all of
    # them (and can never include gA1).
    r = _plan(expected_revision=2, group_counts={"GA": 7}, request_key="plan-2")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["digest"] == digest_v2
    selected_ids = {e["sample_id"] for e in body["selected"]}
    assert len(selected_ids) == 7
    assert "gA1" not in selected_ids
    groups = {g["group"]: g for g in body["groups"]}
    assert groups["GA"]["eligible"] == 7

    # One more than the eligible pool is unsatisfiable.
    r = _plan(expected_revision=2, group_counts={"GA": 8}, request_key="plan-3")
    assert r.status_code == 422
    assert "RETEST_GROUP_QUOTA_EXCEEDED" in r.json()["infeasibility_categories"]


# ----------------------------------------------------------------- lifecycle


def test_plan_unknown_batch_is_404():
    r = _plan()
    assert r.status_code == 404
    assert r.json()["error"] == "CERTIFICATE_NOT_FOUND"


def test_plan_revoked_certificate_is_409_and_saves_nothing(fresh_store):
    _create_cert()
    # Revoke: the re-verification scan drops a planned sample.
    r = client.post("/api/v1/certificates/reverify", json={
        "batch_id": "BATCH-1",
        "request_key": "reverify-1",
        "expected_revision": 1,
        "scan": {w: s for s, w in LAYOUT_4X4.items() if w != "A1"},
        "readings": _readings(),
    })
    assert r.json()["status"] == "REVOKED"

    r = _plan()
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "CERTIFICATE_REVOKED"
    assert body["revoked_reason"] == "SAMPLES_MISSING"
    assert _plan_rows(fresh_store) == []


# ------------------------------------------------------------------ failures


def test_plan_quota_exceeding_eligible_pool_is_422_and_saves_nothing(fresh_store):
    _create_cert()
    r = _plan(group_counts={"GA": 9})
    assert r.status_code == 422
    body = r.json()
    assert body["infeasibility_categories"] == ["RETEST_GROUP_QUOTA_EXCEEDED"]
    assert any("GA" in m and "9" in m for m in body["messages"])
    assert _plan_rows(fresh_store) == []


def test_plan_unknown_group_is_422():
    _create_cert()
    r = _plan(group_counts={"GX": 1})
    assert r.status_code == 422
    body = r.json()
    assert body["infeasibility_categories"] == ["RETEST_GROUP_QUOTA_EXCEEDED"]
    assert any("GX" in m and "0 eligible" in m for m in body["messages"])


def test_plan_all_failing_groups_are_listed():
    _create_cert()
    r = _plan(group_counts={"GA": 20, "GB": 20})
    assert r.status_code == 422
    messages = r.json()["messages"]
    assert any("GA" in m for m in messages) and any("GB" in m for m in messages)


def test_plan_excluding_too_many_wells_is_422():
    _create_cert()
    # Exclude every GA well: the GA quota can no longer be met.
    r = _plan(group_counts={"GA": 1}, excluded_wells=sorted(GA_WELLS.values()))
    assert r.status_code == 422
    assert "RETEST_GROUP_QUOTA_EXCEEDED" in r.json()["infeasibility_categories"]


def test_plan_invalid_excluded_well_is_422_and_saves_nothing(fresh_store):
    _create_cert()
    r = _plan(excluded_wells=["A1", "Z9"])
    assert r.status_code == 422
    body = r.json()
    assert body["infeasibility_categories"] == ["WELL_REFERENCE_INVALID"]
    assert any("Z9" in m for m in body["messages"])
    assert _plan_rows(fresh_store) == []


def test_plan_request_validation_errors():
    _create_cert()
    # Missing request_key.
    payload = _plan_payload()
    del payload["request_key"]
    r = client.post("/api/v1/certificates/retest-plan", json=payload)
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == ["REQUEST_VALIDATION_ERROR"]

    # Empty group_counts.
    r = _plan(group_counts={})
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == ["REQUEST_VALIDATION_ERROR"]

    # Zero quota.
    r = _plan(group_counts={"GA": 0})
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == ["REQUEST_VALIDATION_ERROR"]

    # Negative seed / unknown field.
    r = _plan(seed=-1)
    assert r.status_code == 422
    payload = _plan_payload()
    payload["mapping_package"] = "secret"
    r = client.post("/api/v1/certificates/retest-plan", json=payload)
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == ["REQUEST_VALIDATION_ERROR"]


# ---------------------------------------------------------------- idempotency


def test_plan_replay_same_key_same_content_returns_recorded_response(fresh_store):
    _create_cert()
    b1 = _plan()
    b2 = _plan()
    assert b1.status_code == 201 and b2.status_code == 201
    assert b1.json() == b2.json()
    # The replay did not insert a second plan.
    assert len(_plan_rows(fresh_store)) == 1


def test_plan_same_key_different_content_is_409():
    _create_cert()
    assert _plan().status_code == 201
    r = _plan(seed=7)  # same request_key, different content
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "REQUEST_KEY_CONFLICT"
    assert body["request_key"] == "plan-1"


def test_plan_rejection_is_recorded_and_replayed():
    _create_cert()
    payload = _plan_payload(group_counts={"GA": 9})
    b1 = client.post("/api/v1/certificates/retest-plan", json=payload)
    b2 = client.post("/api/v1/certificates/retest-plan", json=payload)
    assert b1.status_code == 422 and b2.status_code == 422
    assert b1.json() == b2.json()
    # Same key, different content -> 409; a fresh key reports the quota again.
    payload["group_counts"] = {"GA": 8}
    assert client.post(
        "/api/v1/certificates/retest-plan", json=payload).status_code == 409
    r = _plan(group_counts={"GA": 8}, request_key="plan-2")
    assert r.status_code == 201


def test_plan_state_dependent_rejections_do_not_burn_the_key():
    # 404/409 rejections are not recorded: the same key works once the
    # certificate exists and the revision matches.
    r = _plan(expected_revision=1)
    assert r.status_code == 404
    _create_cert()
    r = _plan(expected_revision=2)  # stale revision, same key
    assert r.status_code == 409
    assert r.json()["error"] == "CERTIFICATE_REVISION_MISMATCH"
    r = _plan(expected_revision=1)  # corrected: the key is still usable
    assert r.status_code == 201


def test_plan_concurrent_requests_are_serialized(fresh_store):
    _create_cert()

    def _post(key):
        return TestClient(app).post(
            "/api/v1/certificates/retest-plan",
            json=_plan_payload(request_key=key),
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(_post, ["race-a", "race-b"]))
    assert all(r.status_code == 201 for r in responses)
    ids = {r.json()["plan_id"] for r in responses}
    assert len(ids) == 2
    assert len(_plan_rows(fresh_store)) == 2


# ------------------------------------------------------------------ existing


def test_existing_endpoints_remain_available():
    _create_cert()
    r = client.post("/api/v1/arrangements/batch-release", json=_release_payload())
    assert r.status_code == 200
    assert r.json()["verdict"] == "RELEASED"
    assert client.get("/health").json() == {"status": "ok"}
