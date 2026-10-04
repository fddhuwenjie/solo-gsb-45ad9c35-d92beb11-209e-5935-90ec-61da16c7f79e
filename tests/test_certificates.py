"""Batch certificate endpoint tests.

Creation reuses the release verification: only a RELEASED verdict is
certified, freezing layout/scan/readings/thresholds and the blinded-view
digest into one SQLite transaction. Re-verification restores the frozen
parameters; on success it appends an immutable per-revision evidence
snapshot (fresh scan/readings) in the same transaction and returns the
digest recomputed over it — older revisions stay auditable — while any
misplacement, QC failure or view-digest change revokes the certificate
(first failure reason returned, no new revision appended). A revoked
certificate can never be restored and stale revisions can never overwrite.
Idempotency: the same request_key with identical content replays the
recorded response; different content is 409.
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


def _view():
    """Blinded operational view over the layout wells (row-major order)."""
    wells = sorted(LAYOUT_4X4.values(), key=lambda w: (w[0], int(w[1:])))
    return [{"well": w, "blind_id": f"BL-{i:02d}"} for i, w in enumerate(wells)]


def _create_payload(**over):
    payload = {
        "batch_id": "BATCH-1",
        "request_key": "create-1",
        "release": _release_payload(),
        "view": _view(),
    }
    payload.update(over)
    return payload


def _reverify_payload(**over):
    payload = {
        "batch_id": "BATCH-1",
        "request_key": "reverify-1",
        "expected_revision": 1,
        "scan": {w: s for s, w in LAYOUT_4X4.items()},
        "readings": _readings(),
        "view": _view(),
    }
    payload.update(over)
    return payload


def _create_cert(**over):
    r = client.post("/api/v1/certificates", json=_create_payload(**over))
    assert r.status_code == 201, r.text
    return r.json()


def _revision_rows(db_path):
    """All stored revision snapshots plus the certificate's current row."""
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT revision, digest, view_digest, snapshot "
        "FROM certificate_revisions ORDER BY revision"
    ).fetchall()
    current = conn.execute(
        "SELECT revision, digest, snapshot, status FROM certificates"
    ).fetchone()
    conn.close()
    return rows, current


def _snapshot_digest(snapshot: dict) -> str:
    return hashlib.sha256(certificates._canonical_json(snapshot)).hexdigest()


# --------------------------------------------------------------------- create


def test_create_success_returns_certificate_fields():
    r = client.post("/api/v1/certificates", json=_create_payload())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["certificate_id"].startswith("cert-")
    assert body["batch_id"] == "BATCH-1"
    assert body["status"] == "ACTIVE"
    assert body["revision"] == 1
    assert len(body["digest"]) == 64 and int(body["digest"], 16) >= 0
    assert len(body["view_digest"]) == 64


def test_create_without_view_has_null_view_digest():
    payload = _create_payload()
    del payload["view"]
    r = client.post("/api/v1/certificates", json=payload)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "ACTIVE"
    assert body["view_digest"] is None


def test_create_not_released_creates_no_certificate():
    payload = _create_payload()
    payload["release"]["max_spatial_effect"] = 0.01  # effect gate fails
    r = client.post("/api/v1/certificates", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["certificate_id"] is None
    assert body["status"] == "NOT_CERTIFIED"
    assert body["verdict"] == "NOT_RELEASED"
    assert body["reasons"] == ["SPATIAL_EFFECT_EXCEEDED"]
    # Nothing was persisted: re-verification finds no certificate.
    r = client.post("/api/v1/certificates/reverify", json=_reverify_payload())
    assert r.status_code == 404
    assert r.json()["error"] == "CERTIFICATE_NOT_FOUND"


def test_create_scan_mismatch_is_422_and_creates_nothing():
    payload = _create_payload()
    del payload["release"]["scan"]["A1"]
    r = client.post("/api/v1/certificates", json=payload)
    assert r.status_code == 422
    body = r.json()
    assert "SAMPLES_MISSING" in body["infeasibility_categories"]
    assert body["misplaced_wells"] == [
        {"well": "A1", "expected": "gA1", "actual": None}]
    assert "certificate_id" not in body
    r = client.post("/api/v1/certificates/reverify", json=_reverify_payload())
    assert r.status_code == 404


def test_create_replay_same_key_same_content_returns_recorded_response():
    payload = _create_payload()
    b1 = client.post("/api/v1/certificates", json=payload)
    b2 = client.post("/api/v1/certificates", json=payload)
    assert b1.status_code == 201 and b2.status_code == 201
    assert b1.json() == b2.json()


def test_create_same_key_different_content_is_409():
    _create_cert()
    payload = _create_payload()
    payload["release"]["seed"] = 8  # same request_key, different content
    r = client.post("/api/v1/certificates", json=payload)
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "REQUEST_KEY_CONFLICT"
    assert body["request_key"] == "create-1"


def test_create_second_certificate_for_same_batch_is_409():
    first = _create_cert()
    r = client.post("/api/v1/certificates",
                    json=_create_payload(request_key="create-2"))
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "CERTIFICATE_ALREADY_EXISTS"
    assert body["certificate_id"] == first["certificate_id"]
    assert body["status"] == "ACTIVE"


def test_create_invalid_view_is_422_and_creates_nothing():
    payload = _create_payload()
    payload["view"][1]["blind_id"] = payload["view"][0]["blind_id"]  # duplicate
    r = client.post("/api/v1/certificates", json=payload)
    assert r.status_code == 422
    assert "CERTIFICATE_VIEW_INVALID" in r.json()["infeasibility_categories"]

    payload = _create_payload(request_key="create-2")
    payload["view"][0]["well"] = "Z9"  # outside the plate
    r = client.post("/api/v1/certificates", json=payload)
    assert r.status_code == 422
    assert "WELL_REFERENCE_INVALID" in r.json()["infeasibility_categories"]

    r = client.post("/api/v1/certificates/reverify", json=_reverify_payload())
    assert r.status_code == 404


def test_create_not_certified_response_is_recorded_under_the_key():
    payload = _create_payload()
    payload["release"]["max_spatial_effect"] = 0.01
    b1 = client.post("/api/v1/certificates", json=payload)
    b2 = client.post("/api/v1/certificates", json=payload)
    assert b1.status_code == 200 and b2.status_code == 200
    assert b1.json() == b2.json()
    # Same key, different content -> 409; fresh key -> the batch can certify.
    payload["release"]["seed"] = 8
    assert client.post("/api/v1/certificates", json=payload).status_code == 409
    r = client.post("/api/v1/certificates",
                    json=_create_payload(request_key="create-2"))
    assert r.status_code == 201


# ------------------------------------------------------------------- reverify


def test_reverify_success_increments_revision_atomically():
    created = _create_cert()
    r = client.post("/api/v1/certificates/reverify", json=_reverify_payload())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ACTIVE"
    assert body["revision"] == 2
    assert body["certificate_id"] == created["certificate_id"]
    assert body["digest"] == created["digest"]
    assert body["view_digest"] == created["view_digest"]

    r = client.post("/api/v1/certificates/reverify", json=_reverify_payload(
        request_key="reverify-2", expected_revision=2))
    assert r.status_code == 200
    assert r.json()["revision"] == 3


def test_reverify_replay_does_not_double_increment():
    _create_cert()
    payload = _reverify_payload()
    b1 = client.post("/api/v1/certificates/reverify", json=payload)
    b2 = client.post("/api/v1/certificates/reverify", json=payload)
    assert b1.status_code == 200 and b2.status_code == 200
    assert b1.json() == b2.json()
    assert b2.json()["revision"] == 2
    # The replay did not burn a revision: 2 is still the current one.
    r = client.post("/api/v1/certificates/reverify", json=_reverify_payload(
        request_key="reverify-2", expected_revision=2))
    assert r.status_code == 200
    assert r.json()["revision"] == 3


def test_reverify_same_key_different_content_is_409():
    _create_cert()
    client.post("/api/v1/certificates/reverify", json=_reverify_payload())
    payload = _reverify_payload(expected_revision=2)
    payload["readings"] = _readings(row_bump=0.5)  # same key, new content
    r = client.post("/api/v1/certificates/reverify", json=payload)
    assert r.status_code == 409
    assert r.json()["error"] == "REQUEST_KEY_CONFLICT"


def test_reverify_stale_revision_is_409_and_never_overwrites():
    _create_cert()
    client.post("/api/v1/certificates/reverify", json=_reverify_payload())
    # Current revision is 2; resubmitting against the old revision 1 fails.
    r = client.post("/api/v1/certificates/reverify", json=_reverify_payload(
        request_key="reverify-2", expected_revision=1))
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "CERTIFICATE_REVISION_MISMATCH"
    assert body["current_revision"] == 2
    assert body["expected_revision"] == 1
    # The certificate is untouched: the correct revision still verifies.
    r = client.post("/api/v1/certificates/reverify", json=_reverify_payload(
        request_key="reverify-3", expected_revision=2))
    assert r.status_code == 200
    assert r.json()["revision"] == 3


def test_reverify_unknown_batch_is_404():
    r = client.post("/api/v1/certificates/reverify", json=_reverify_payload())
    assert r.status_code == 404
    assert r.json()["error"] == "CERTIFICATE_NOT_FOUND"


def test_reverify_closed_loop_misplacement_revokes_with_first_reason():
    created = _create_cert()
    payload = _reverify_payload()
    payload["scan"]["A1"], payload["scan"]["A2"] = "gB1", "gA1"  # swap
    r = client.post("/api/v1/certificates/reverify", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "REVOKED"
    assert body["revision"] == 1  # never incremented
    assert body["digest"] == created["digest"]
    revocation = body["revocation"]
    assert revocation["reason"] == "CLOSED_LOOP_MISPLACEMENT"
    assert revocation["cycles"] == [["A1", "A2"]]
    assert {"well": "A1", "expected": "gA1", "actual": "gB1"} in \
        revocation["misplaced_wells"]


def test_reverify_missing_sample_revokes():
    _create_cert()
    payload = _reverify_payload()
    del payload["scan"]["A1"]
    r = client.post("/api/v1/certificates/reverify", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "REVOKED"
    assert body["revocation"]["reason"] == "SAMPLES_MISSING"


def test_reverify_qc_gate_failure_revokes():
    _create_cert()
    # Fresh readings carry a strong row-A effect that breaches the frozen
    # max_spatial_effect=10.0 threshold.
    r = client.post("/api/v1/certificates/reverify", json=_reverify_payload(
        readings=_readings(row_bump=100.0)))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "REVOKED"
    assert body["revocation"]["reason"] == "SPATIAL_EFFECT_EXCEEDED"


def test_reverify_structural_reading_problem_revokes():
    _create_cert()
    payload = _reverify_payload()
    payload["readings"]["a1"] = 1.0  # duplicates "A1" after normalisation
    r = client.post("/api/v1/certificates/reverify", json=payload)
    assert r.status_code == 200
    assert r.json()["status"] == "REVOKED"
    assert r.json()["revocation"]["reason"] == "SPATIAL_READING_DUPLICATED"


def test_reverify_view_digest_change_revokes():
    _create_cert()
    payload = _reverify_payload()
    payload["view"][0]["blind_id"] = "BL-tampered"
    r = client.post("/api/v1/certificates/reverify", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "REVOKED"
    assert body["revocation"]["reason"] == "VIEW_DIGEST_MISMATCH"


def test_reverify_with_matching_view_passes():
    _create_cert()
    r = client.post("/api/v1/certificates/reverify", json=_reverify_payload())
    assert r.status_code == 200
    assert r.json()["status"] == "ACTIVE"


def test_reverify_without_view_passes():
    _create_cert()  # certificate carries a view digest
    payload = _reverify_payload()
    del payload["view"]
    r = client.post("/api/v1/certificates/reverify", json=payload)
    assert r.status_code == 200
    assert r.json()["status"] == "ACTIVE"


def test_reverify_invalid_view_is_422_and_leaves_certificate_active():
    _create_cert()
    payload = _reverify_payload()
    payload["view"][0]["well"] = "Z9"
    r = client.post("/api/v1/certificates/reverify", json=payload)
    assert r.status_code == 422
    assert "WELL_REFERENCE_INVALID" in r.json()["infeasibility_categories"]
    # Not a verification failure: the certificate is still ACTIVE at rev 1.
    r = client.post("/api/v1/certificates/reverify", json=_reverify_payload(
        request_key="reverify-2"))
    assert r.status_code == 200
    assert r.json()["revision"] == 2


def test_revoked_certificate_can_never_be_restored():
    _create_cert()
    payload = _reverify_payload()
    del payload["scan"]["A1"]
    r = client.post("/api/v1/certificates/reverify", json=payload)
    assert r.json()["status"] == "REVOKED"

    # Replaying the revoking request returns the recorded REVOKED response.
    replay = client.post("/api/v1/certificates/reverify", json=payload)
    assert replay.status_code == 200
    assert replay.json() == r.json()

    # Any new re-verification is refused; revocation is final.
    r = client.post("/api/v1/certificates/reverify", json=_reverify_payload(
        request_key="reverify-2"))
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "CERTIFICATE_REVOKED"
    assert body["revoked_reason"] == "SAMPLES_MISSING"

    # And no new certificate can be created for the batch either.
    r = client.post("/api/v1/certificates",
                    json=_create_payload(request_key="create-2"))
    assert r.status_code == 409
    assert r.json()["error"] == "CERTIFICATE_ALREADY_EXISTS"
    assert r.json()["status"] == "REVOKED"


# ------------------------------------------------------- revision snapshots


def test_reverify_success_appends_immutable_revision_with_new_digest(fresh_store):
    created = _create_cert()
    new_readings = _readings(row_bump=0.5)  # passes the frozen gates
    r = client.post("/api/v1/certificates/reverify",
                    json=_reverify_payload(readings=new_readings))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ACTIVE"
    assert body["revision"] == 2
    # The digest now covers the revision-2 evidence, not the creation data.
    assert body["digest"] != created["digest"]
    assert body["view_digest"] == created["view_digest"]

    rows, current = _revision_rows(fresh_store)
    assert [row[0] for row in rows] == [1, 2]
    rev1 = json.loads(rows[0][3])
    rev2 = json.loads(rows[1][3])
    # Revision 1 keeps the creation-time evidence, unchanged and auditable.
    assert rev1["readings"] == _readings()
    assert rev1["scan"] == {w: s for s, w in LAYOUT_4X4.items()}
    # Revision 2 freezes the fresh scan/readings over the unchanged plan.
    assert rev2["readings"] == new_readings
    assert rev2["scan"] == rev1["scan"]
    assert rev2["target"] == rev1["target"]
    assert rev2["samples"] == rev1["samples"]
    assert rev2["thresholds"] == rev1["thresholds"]
    assert rev2["view_digest"] == rev1["view_digest"]
    # Each stored digest covers its own revision snapshot.
    assert rows[0][1] == created["digest"] == _snapshot_digest(rev1)
    assert rows[1][1] == body["digest"] == _snapshot_digest(rev2)
    # The certificate itself points at the latest revision and evidence.
    assert current[0] == 2
    assert current[1] == body["digest"]
    assert json.loads(current[2])["readings"] == new_readings


def test_reverify_identical_evidence_keeps_digest_but_appends_revision(fresh_store):
    created = _create_cert()
    r = client.post("/api/v1/certificates/reverify", json=_reverify_payload())
    assert r.status_code == 200
    # Same scan/readings -> same evidence content -> same digest, but the
    # new revision is still appended to the audit trail.
    assert r.json()["digest"] == created["digest"]
    rows, _ = _revision_rows(fresh_store)
    assert [row[0] for row in rows] == [1, 2]
    assert json.loads(rows[0][3]) == json.loads(rows[1][3])


def test_reverify_revision_evidence_survives_restart(fresh_store):
    created = _create_cert()
    first = client.post("/api/v1/certificates/reverify",
                        json=_reverify_payload(readings=_readings(row_bump=0.5)))
    assert first.status_code == 200
    digest_v2 = first.json()["digest"]
    assert digest_v2 != created["digest"]

    # Simulate a service restart: the store is rebuilt from the same file.
    certificates.reset_stores()

    conn = sqlite3.connect(str(fresh_store))
    row = conn.execute(
        "SELECT revision, digest, snapshot FROM certificates").fetchone()
    conn.close()
    # The current snapshot is the revision-2 evidence, not creation data.
    assert row[0] == 2
    assert row[1] == digest_v2
    assert json.loads(row[2])["readings"] == _readings(row_bump=0.5)

    # The service keeps verifying from the restored state.
    r = client.post("/api/v1/certificates/reverify", json=_reverify_payload(
        request_key="reverify-2", expected_revision=2,
        readings=_readings(row_bump=0.25)))
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 3
    assert r.json()["digest"] not in (created["digest"], digest_v2)
    rows, current = _revision_rows(fresh_store)
    assert [row[0] for row in rows] == [1, 2, 3]
    assert current[0] == 3 and current[1] == r.json()["digest"]


def test_reverify_replay_does_not_append_extra_revision(fresh_store):
    _create_cert()
    payload = _reverify_payload(readings=_readings(row_bump=0.5))
    b1 = client.post("/api/v1/certificates/reverify", json=payload)
    b2 = client.post("/api/v1/certificates/reverify", json=payload)
    assert b1.status_code == 200 and b2.status_code == 200
    assert b1.json() == b2.json()
    assert b2.json()["revision"] == 2
    # The replay returned the recorded response without appending a revision.
    rows, _ = _revision_rows(fresh_store)
    assert [row[0] for row in rows] == [1, 2]


def test_reverify_replay_after_restart_returns_recorded_response(fresh_store):
    _create_cert()
    payload = _reverify_payload(readings=_readings(row_bump=0.5))
    first = client.post("/api/v1/certificates/reverify", json=payload)
    assert first.status_code == 200

    certificates.reset_stores()  # restart on the same database file

    replay = client.post("/api/v1/certificates/reverify", json=payload)
    assert replay.status_code == 200
    assert replay.json() == first.json()
    rows, _ = _revision_rows(fresh_store)
    assert [row[0] for row in rows] == [1, 2]


def test_reverify_concurrent_same_expected_revision_exactly_one_wins(fresh_store):
    _create_cert()
    payload_a = _reverify_payload(request_key="race-a",
                                  readings=_readings(row_bump=0.25))
    payload_b = _reverify_payload(request_key="race-b",
                                  readings=_readings(row_bump=0.5))

    def _post(payload):
        # One client per thread; the store serializes the transactions.
        return TestClient(app).post("/api/v1/certificates/reverify", json=payload)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(_post, [payload_a, payload_b]))

    ok = [r for r in responses if r.status_code == 200]
    conflicts = [r for r in responses if r.status_code == 409]
    assert len(ok) == 1 and len(conflicts) == 1
    assert ok[0].json()["status"] == "ACTIVE"
    assert ok[0].json()["revision"] == 2
    body = conflicts[0].json()
    assert body["error"] == "CERTIFICATE_REVISION_MISMATCH"
    assert body["current_revision"] == 2
    # Exactly one revision was appended despite the race.
    rows, _ = _revision_rows(fresh_store)
    assert [row[0] for row in rows] == [1, 2]

    # The loser's state-dependent rejection did not burn its request key:
    # the same key with the corrected revision verifies successfully.
    loser = payload_a if responses[0].status_code == 409 else payload_b
    loser["expected_revision"] = 2
    r = client.post("/api/v1/certificates/reverify", json=loser)
    assert r.status_code == 200
    assert r.json()["revision"] == 3


def test_reverify_stale_expected_revision_after_restart_is_409(fresh_store):
    _create_cert()
    r = client.post("/api/v1/certificates/reverify",
                    json=_reverify_payload(readings=_readings(row_bump=0.5)))
    assert r.status_code == 200

    certificates.reset_stores()  # restart on the same database file

    r = client.post("/api/v1/certificates/reverify", json=_reverify_payload(
        request_key="reverify-stale", expected_revision=1,
        readings=_readings(row_bump=0.25)))
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "CERTIFICATE_REVISION_MISMATCH"
    assert body["current_revision"] == 2
    assert body["expected_revision"] == 1
    # The rejected request appended nothing; the current revision verifies.
    rows, _ = _revision_rows(fresh_store)
    assert [row[0] for row in rows] == [1, 2]
    r = client.post("/api/v1/certificates/reverify", json=_reverify_payload(
        request_key="reverify-current", expected_revision=2,
        readings=_readings(row_bump=0.25)))
    assert r.status_code == 200
    assert r.json()["revision"] == 3


def test_reverify_revocation_appends_no_new_revision(fresh_store):
    created = _create_cert()
    r = client.post("/api/v1/certificates/reverify",
                    json=_reverify_payload(readings=_readings(row_bump=100.0)))
    assert r.status_code == 200
    assert r.json()["status"] == "REVOKED"
    assert r.json()["digest"] == created["digest"]
    rows, current = _revision_rows(fresh_store)
    # Only the creation revision exists; the certificate keeps the last
    # good evidence and its digest.
    assert [row[0] for row in rows] == [1]
    assert current[3] == "REVOKED"
    assert current[0] == 1
    assert current[1] == created["digest"]


def test_store_open_backfills_revision_rows_for_legacy_databases(fresh_store):
    created = _create_cert()
    # Simulate a database written before per-revision snapshots existed.
    conn = sqlite3.connect(str(fresh_store))
    conn.execute("DELETE FROM certificate_revisions")
    conn.commit()
    conn.close()

    certificates.reset_stores()  # reopen -> migration anchors current state
    certificates.get_store()  # lazily reopens the store and runs the migration

    rows, _ = _revision_rows(fresh_store)
    assert [row[0] for row in rows] == [1]
    assert rows[0][1] == created["digest"]
    # The next re-verification appends revision 2 on top of the anchor.
    r = client.post("/api/v1/certificates/reverify",
                    json=_reverify_payload(readings=_readings(row_bump=0.5)))
    assert r.status_code == 200
    assert r.json()["revision"] == 2
    rows, _ = _revision_rows(fresh_store)
    assert [row[0] for row in rows] == [1, 2]


# ------------------------------------------------------------- confidentiality


def test_certificate_stores_no_view_entries_or_plaintext_blind_mapping(fresh_store):
    _create_cert()
    conn = sqlite3.connect(str(fresh_store))
    row = conn.execute(
        "SELECT snapshot, view_digest, digest FROM certificates").fetchone()
    conn.close()
    snapshot, view_digest, digest = row
    stored = json.loads(snapshot)
    # The snapshot freezes layout/scan/readings/thresholds plus the view
    # DIGEST only — never the view entries (well -> blind_id) themselves.
    assert stored["view_digest"] == view_digest
    assert "view" not in stored
    assert set(stored) == {
        "batch_id", "plate", "target", "scan", "readings", "samples",
        "blocked_wells", "seed", "thresholds", "view_digest",
    }
    assert len(view_digest) == 64 and len(digest) == 64
    # No blind_id appears anywhere in the database files (main db or WAL).
    for suffix in ("", "-wal", "-shm"):
        path = fresh_store.with_name(fresh_store.name + suffix)
        if path.exists():
            assert b"BL-" not in path.read_bytes()


# ------------------------------------------------------------ request shape


def test_missing_request_key_is_request_validation_error():
    payload = _create_payload()
    del payload["request_key"]
    r = client.post("/api/v1/certificates", json=payload)
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == ["REQUEST_VALIDATION_ERROR"]


def test_reverify_missing_expected_revision_is_request_validation_error():
    payload = _reverify_payload()
    del payload["expected_revision"]
    r = client.post("/api/v1/certificates/reverify", json=payload)
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == ["REQUEST_VALIDATION_ERROR"]


def test_unknown_field_is_422():
    payload = _create_payload()
    payload["mapping_package"] = "secret"  # not part of the certificate contract
    r = client.post("/api/v1/certificates", json=payload)
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == ["REQUEST_VALIDATION_ERROR"]


def test_existing_endpoints_remain_available():
    r = client.post("/api/v1/arrangements/batch-release", json=_release_payload())
    assert r.status_code == 200
    assert r.json()["verdict"] == "RELEASED"
    assert client.get("/health").json() == {"status": "ok"}
