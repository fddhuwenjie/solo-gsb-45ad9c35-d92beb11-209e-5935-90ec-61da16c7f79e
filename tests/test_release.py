"""Batch release endpoint tests.

Stage 1 (scan agreement) reuses the reconciliation rules: every kind of
disagreement is 422 with problem categories and the misplaced-well list and
no verdict. Stage 2 (spatial QC gates) returns 200 RELEASED only when the
polish converged, the maximal row/column effect is within the limit and the
permutation p-value reaches the threshold; otherwise 200 NOT_RELEASED with
reasons and no QC summary. Structural spatial-QC problems remain 422.
"""
import hashlib
import json

from fastapi.testclient import TestClient

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


def _hash(text: str) -> int:
    return int(hashlib.md5(text.encode()).hexdigest()[:6], 16)


def _readings(row_bump=0.0):
    out = {}
    for sid, well in LAYOUT_4X4.items():
        base = 100.0 if sid.startswith("gA") else 0.0
        noise = (_hash(sid) % 7) - 3  # -3..3
        out[well] = base + (row_bump if well[0] == "A" else 0.0) + noise
    return out


def _payload(**over):
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


# --------------------------------------------------------------------- 200 RELEASED


def test_released_when_scan_matches_and_all_gates_pass():
    r = client.post("/api/v1/arrangements/batch-release", json=_payload())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] == "RELEASED"
    assert "reasons" not in body
    gates = body["gates"]
    assert gates["converged"] is True
    assert gates["effect_within_limit"] is True
    assert gates["p_value_available"] is True
    assert gates["p_value_meets_threshold"] is True
    assert gates["max_abs_row_col_effect"] <= gates["max_spatial_effect"]
    assert gates["observed_p_value"] >= gates["min_p_value"]


def test_released_body_carries_scan_and_qc_summaries():
    payload = _payload()
    body = client.post("/api/v1/arrangements/batch-release", json=payload).json()
    scan = body["scan_summary"]
    assert scan == {
        "scanned_wells": 16,
        "planned_samples": 16,
        "misplaced_wells": [],
        "blocked_wells": [],
        "blocked_wells_occupied": 0,
        "matches_plan": True,
    }
    qc = body["spatial_qc_summary"]
    assert set(qc) == {
        "seed", "plate", "parameters", "groups", "effects",
        "readings", "convergence", "permutation_test",
    }
    assert qc["effects"]["max_abs_row_col_effect"] == body["gates"]["max_abs_row_col_effect"]
    assert qc["permutation_test"]["p_value"] == body["gates"]["observed_p_value"]
    assert body["thresholds"] == {
        "max_spatial_effect": 10.0,
        "min_p_value": 0.001,
        "permutations": 100,
        "tolerance": 1e-6,
        "max_iterations": 100,
    }


def test_release_is_deterministic():
    payload = _payload()
    b1 = client.post("/api/v1/arrangements/batch-release", json=payload).json()
    b2 = client.post("/api/v1/arrangements/batch-release", json=payload).json()
    assert b1 == b2


# ------------------------------------------------- 200 NOT_RELEASED (QC gates)


def test_effect_gate_failure_is_not_released_without_qc_summary():
    r = client.post("/api/v1/arrangements/batch-release",
                    json=_payload(max_spatial_effect=0.01))
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "NOT_RELEASED"
    assert body["reasons"] == ["SPATIAL_EFFECT_EXCEEDED"]
    assert body["gates"]["effect_within_limit"] is False
    assert body["gates"]["max_abs_row_col_effect"] > 0.01
    # No partial conclusion: the spatial QC detail summary is withheld.
    assert "spatial_qc_summary" not in body
    assert "scan_summary" not in body


def test_p_value_gate_failure_is_not_released():
    r = client.post("/api/v1/arrangements/batch-release",
                    json=_payload(min_p_value=0.99))
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "NOT_RELEASED"
    assert body["reasons"] == ["P_VALUE_BELOW_THRESHOLD"]
    assert body["gates"]["p_value_meets_threshold"] is False
    assert body["gates"]["observed_p_value"] < 0.99
    assert "spatial_qc_summary" not in body


def test_zero_permutations_cannot_release():
    r = client.post("/api/v1/arrangements/batch-release",
                    json=_payload(permutations=0))
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "NOT_RELEASED"
    assert body["reasons"] == ["P_VALUE_UNAVAILABLE"]
    assert body["gates"]["p_value_available"] is False
    assert body["gates"]["observed_p_value"] is None
    assert body["gates"]["p_value_meets_threshold"] is False


def test_non_convergence_is_not_released_with_spatial_not_converged_reason():
    # 2x2-style grid with a single iteration and ultra-tight tolerance.
    r = client.post("/api/v1/arrangements/batch-release", json=_payload(
        tolerance=1e-12, max_iterations=1))
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "NOT_RELEASED"
    assert body["reasons"] == ["SPATIAL_NOT_CONVERGED"]
    assert body["gates"]["converged"] is False
    assert body["gates"]["max_abs_row_col_effect"] is None
    assert "spatial_qc_summary" not in body


def test_effect_and_p_value_gates_can_fail_together():
    r = client.post("/api/v1/arrangements/batch-release",
                    json=_payload(max_spatial_effect=0.01, min_p_value=0.99))
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "NOT_RELEASED"
    assert set(body["reasons"]) == {
        "SPATIAL_EFFECT_EXCEEDED", "P_VALUE_BELOW_THRESHOLD"}
    assert len(body["messages"]) == 2


# ----------------------------------------------------------- 422 (stage 1: scan)


def test_missing_sample_422_lists_misplaced_well_and_no_verdict():
    payload = _payload()
    del payload["scan"]["A1"]  # planned S1 absent, A1 empty -> missing
    r = client.post("/api/v1/arrangements/batch-release", json=payload)
    assert r.status_code == 422
    body = r.json()
    assert "SAMPLES_MISSING" in body["infeasibility_categories"]
    assert body["misplaced_wells"] == [
        {"well": "A1", "expected": "gA1", "actual": None}]
    assert "verdict" not in body
    assert "spatial_qc_summary" not in body


def test_unplanned_sample_422_lists_misplaced_well():
    payload = _payload()
    payload["scan"]["A1"] = "STRANGER"
    r = client.post("/api/v1/arrangements/batch-release", json=payload)
    assert r.status_code == 422
    body = r.json()
    cats = body["infeasibility_categories"]
    assert "UNPLANNED_SAMPLES" in cats and "SAMPLES_MISSING" in cats
    assert {"well": "A1", "expected": "gA1", "actual": "STRANGER"} in body["misplaced_wells"]


def test_closed_loop_422_lists_cycle_and_misplaced_wells():
    payload = _payload()
    payload["scan"]["A1"], payload["scan"]["A2"] = "gB1", "gA1"
    r = client.post("/api/v1/arrangements/batch-release", json=payload)
    assert r.status_code == 422
    body = r.json()
    assert "CLOSED_LOOP_MISPLACEMENT" in body["infeasibility_categories"]
    assert "SCAN_LAYOUT_MISMATCH" in body["infeasibility_categories"]
    assert body["cycles"] == [["A1", "A2"]]
    wells = {(m["well"], m["expected"], m["actual"]) for m in body["misplaced_wells"]}
    assert ("A1", "gA1", "gB1") in wells
    assert ("A2", "gB1", "gA1") in wells


def test_open_chain_misplacement_422_without_cycle_category():
    # Same sample set, but S3/B1 sits on a non-target occupied chain with an
    # empty target well: use a 24-well plate layout for an unambiguous chain.
    layout = {"S1": "A1", "S2": "A2", "S3": "A3"}
    scan = {"C1": "S1", "A1": "S2", "A2": "S3"}  # chain, A3 empty
    samples = [{"id": "S1", "group": "G"}, {"id": "S2", "group": "G"},
               {"id": "S3", "group": "H"}]
    readings = {"C1": 1.0, "A1": 2.0, "A2": 3.0}
    r = client.post("/api/v1/arrangements/batch-release", json={
        "plate": {"type": "24"},
        "target": layout, "scan": scan, "samples": samples, "readings": readings,
        "seed": 1, "max_spatial_effect": 10.0, "min_p_value": 0.0,
        "permutations": 20,
    })
    assert r.status_code == 422
    body = r.json()
    assert body["infeasibility_categories"] == ["SCAN_LAYOUT_MISMATCH"]
    assert body["cycles"] == []
    wells = {m["well"] for m in body["misplaced_wells"]}
    assert wells == {"A1", "A2", "A3", "C1"}


def test_blocked_well_occupied_422():
    # Drop the planned owner of C3, block C3 and put an unplanned sample on
    # it: the scan then occupies a blocked well with an unplanned sample.
    payload = _payload()
    payload["blocked_wells"] = ["C3"]
    payload["target"] = {k: v for k, v in LAYOUT_4X4.items() if v != "C3"}
    payload["samples"] = [s for s in SAMPLES_4X4 if s["id"] != "gA6"]
    del payload["readings"]["C3"]
    payload["scan"]["C3"] = "X9"
    r = client.post("/api/v1/arrangements/batch-release", json=payload)
    assert r.status_code == 422
    cats = r.json()["infeasibility_categories"]
    assert "BLOCKED_WELL_OCCUPIED" in cats
    assert "UNPLANNED_SAMPLES" in cats


def test_scan_duplicate_samples_422():
    payload = _payload()
    payload["scan"]["A2"] = "gA1"  # gA1 now at two wells, gB1 missing
    r = client.post("/api/v1/arrangements/batch-release", json=payload)
    assert r.status_code == 422
    cats = r.json()["infeasibility_categories"]
    assert "SCAN_SAMPLES_DUPLICATED" in cats


def test_invalid_well_in_scan_422():
    payload = _payload()
    payload["scan"]["Z9"] = "gA1"
    r = client.post("/api/v1/arrangements/batch-release", json=payload)
    assert r.status_code == 422
    assert "WELL_REFERENCE_INVALID" in r.json()["infeasibility_categories"]


def test_scan_problem_short_circuits_spatial_qc():
    # Both a scan mismatch and unusable spatial data: stage 1 wins (422),
    # and no spatial effects are ever computed or returned.
    payload = _payload()
    del payload["scan"]["A1"]
    payload["readings"] = {"A1": 1.0}  # would be SPATIAL_* on its own
    r = client.post("/api/v1/arrangements/batch-release", json=payload)
    assert r.status_code == 422
    body = r.json()
    assert "SAMPLES_MISSING" in body["infeasibility_categories"]
    assert "gates" not in body


def test_exact_match_empty_misplaced_list_releases():
    # Already-matching scan with zero misplaced wells proceeds to QC.
    r = client.post("/api/v1/arrangements/batch-release", json=_payload())
    assert r.status_code == 200
    assert r.json()["scan_summary"]["misplaced_wells"] == []


# ----------------------------------------------------------- 422 (QC structure)


def test_spatial_group_insufficient_is_422():
    samples = [dict(s) for s in SAMPLES_4X4]
    samples[0]["group"] = "LONER"
    r = client.post("/api/v1/arrangements/batch-release",
                    json=_payload(samples=samples))
    assert r.status_code == 422
    assert "SPATIAL_GROUP_INSUFFICIENT" in r.json()["infeasibility_categories"]


def test_spatial_sample_unknown_is_422():
    # Layout references a sample absent from the samples list.
    target = dict(LAYOUT_4X4)
    target["mystery"] = target.pop("gA1")
    scan = {w: s for s, w in target.items()}
    r = client.post("/api/v1/arrangements/batch-release",
                    json=_payload(target=target, scan=scan))
    assert r.status_code == 422
    assert "SPATIAL_SAMPLE_UNKNOWN" in r.json()["infeasibility_categories"]


def test_spatial_unplaced_sample_is_422():
    samples = [dict(s) for s in SAMPLES_4X4] + [{"id": "extra", "group": "GA"}]
    r = client.post("/api/v1/arrangements/batch-release",
                    json=_payload(samples=samples))
    assert r.status_code == 422
    assert "SPATIAL_LAYOUT_CONFLICT" in r.json()["infeasibility_categories"]


# ----------------------------------------------------------- 422 (request shape)


def test_missing_required_field_is_request_validation_error():
    payload = _payload()
    del payload["max_spatial_effect"]
    r = client.post("/api/v1/arrangements/batch-release", json=payload)
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == ["REQUEST_VALIDATION_ERROR"]


def test_non_finite_reading_is_request_validation_error():
    payload = _payload()
    payload["readings"]["A1"] = float("nan")
    r = client.post(
        "/api/v1/arrangements/batch-release",
        content=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == ["REQUEST_VALIDATION_ERROR"]


def test_min_p_value_out_of_range_is_422():
    r = client.post("/api/v1/arrangements/batch-release",
                    json=_payload(min_p_value=1.5))
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == ["REQUEST_VALIDATION_ERROR"]


def test_unknown_field_is_422():
    payload = _payload()
    payload["staging_well"] = "D6"  # not part of the release contract
    r = client.post("/api/v1/arrangements/batch-release", json=payload)
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == ["REQUEST_VALIDATION_ERROR"]


def test_existing_endpoints_remain_available():
    r = client.post("/api/v1/arrangements/spatial-qc", json={
        "plate": PLATE_4X4,
        "layout": dict(LAYOUT_4X4),
        "samples": [dict(s) for s in SAMPLES_4X4],
        "readings": _readings(),
        "seed": 7, "permutations": 20,
    })
    assert r.status_code == 200
    r = client.post("/api/v1/arrangements/reconcile", json={
        "plate": {"type": "24"},
        "target": {"S1": "A1"},
        "scan": {"A1": "S1"},
    })
    assert r.status_code == 200
