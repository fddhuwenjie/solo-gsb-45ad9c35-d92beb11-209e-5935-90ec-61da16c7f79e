"""Spatial QC endpoint tests: group centring, median polish effects,
corrected readings, convergence trajectory, permutation p-value,
determinism and the 422 validity gates."""
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

# 4 rows x 4 cols, 16 wells, two groups interleaved checkerboard-style.
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


def _payload(readings, **over):
    payload = {
        "plate": {"type": "custom", "rows": 4, "columns": 4},
        "layout": LAYOUT_4X4,
        "samples": SAMPLES_4X4,
        "readings": readings,
        "seed": 7,
        "permutations": 100,
        "tolerance": 1e-6,
        "max_iterations": 100,
    }
    payload.update(over)
    return payload


def _additive_readings(row_bump=5.0, missing=()):
    """Group baselines differ by 100; row A carries a shared additive bump."""
    readings = {}
    for i, (sid, well) in enumerate(LAYOUT_4X4.items()):
        base = 100.0 if sid.startswith("gA") else 0.0
        noise = (hash_deterministic(sid) % 7) - 3  # -3..3
        readings[well] = base + (row_bump if well[0] == "A" else 0.0) + noise
    for well in missing:
        readings[well] = None
    return readings


def hash_deterministic(text: str) -> int:
    import hashlib

    return int(hashlib.md5(text.encode()).hexdigest()[:6], 16)


# --------------------------------------------------------------------- 200


def test_basic_response_shape_and_effects():
    r = client.post("/api/v1/arrangements/spatial-qc",
                    json=_payload(_additive_readings()))
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {
        "seed", "plate", "parameters", "groups", "effects",
        "readings", "convergence", "permutation_test",
    }
    assert body["plate"] == {"type": "custom", "rows": 4, "columns": 4}
    assert body["parameters"] == {
        "permutations": 100, "tolerance": 1e-6, "max_iterations": 100,
    }

    effects = body["effects"]
    assert set(effects) == {
        "overall", "row_effects", "col_effects", "max_abs_row_col_effect",
    }
    assert len(effects["row_effects"]) == 4
    assert len(effects["col_effects"]) == 4
    # The row-A bump (~5) dominates any column effect; integer noise and
    # the additive split make the estimated bump slightly smaller than 5.
    assert effects["row_effects"][0] == pytest.approx(5.0, abs=0.75)
    assert effects["max_abs_row_col_effect"] == pytest.approx(
        max(abs(v) for v in effects["row_effects"] + effects["col_effects"]),
        abs=1e-9,
    )


def test_group_median_centring_removes_between_group_baseline():
    # GA occupies rows A/B, GB rows C/D: without centring the raw data
    # exhibits a ~50 row effect; centring by group median removes it all.
    layout = {
        **{f"a{i}": f"{chr(65 + i // 4)}{i % 4 + 1}" for i in range(8)},
        **{f"b{i}": f"{chr(65 + i // 4 + 2)}{i % 4 + 1}" for i in range(8)},
    }
    samples = ([{"id": f"a{i}", "group": "GA"} for i in range(8)]
               + [{"id": f"b{i}", "group": "GB"} for i in range(8)])
    readings = {w: 100.0 for w in list(layout.values())[:8]}
    readings.update({w: 50.0 for w in list(layout.values())[8:]})
    r = client.post("/api/v1/arrangements/spatial-qc", json=_payload(
        readings, layout=layout, samples=samples, permutations=50))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["effects"]["max_abs_row_col_effect"] == 0.0
    assert body["effects"]["overall"] == 0.0
    assert body["permutation_test"]["p_value"] == 1.0
    # Corrected readings keep each group's own median.
    corrected = {rec["well"]: rec["corrected_reading"]
                 for rec in body["readings"]}
    assert corrected["A1"] == 100.0
    assert corrected["C1"] == 50.0


def test_missing_readings_allowed_null_and_omitted():
    readings = _additive_readings(missing=("D4",))
    del readings["C3"]  # omitted well is also a missing reading
    r = client.post("/api/v1/arrangements/spatial-qc",
                    json=_payload(readings, permutations=20))
    assert r.status_code == 200, r.text
    body = r.json()
    wells = {rec["well"] for rec in body["readings"]}
    assert "D4" not in wells and "C3" not in wells
    assert len(body["readings"]) == 14
    summary = {g["group"]: g["valid_readings"] for g in body["groups"]}
    # gA8 (D4) and gA5 (C3) are both GA -> 6 GA, 8 GB valid readings.
    assert summary == {"GA": 6, "GB": 8}


def test_corrected_readings_strip_fitted_spatial_surface():
    body = client.post("/api/v1/arrangements/spatial-qc",
                       json=_payload(_additive_readings())) .json()
    eff = body["effects"]
    for rec in body["readings"]:
        r = ord(rec["well"][0]) - ord("A")
        c = int(rec["well"][1:]) - 1
        surface = (eff["overall"] + eff["row_effects"][r]
                   + eff["col_effects"][c])
        assert rec["reading"] - rec["corrected_reading"] == pytest.approx(
            surface, abs=1e-7)


def test_convergence_trajectory():
    body = client.post("/api/v1/arrangements/spatial-qc",
                       json=_payload(_additive_readings())) .json()
    conv = body["convergence"]
    assert conv["converged"] is True
    assert conv["iterations"] == len(conv["trajectory"])
    assert conv["trajectory"][0]["iteration"] == 1
    for step in conv["trajectory"]:
        assert step["max_correction"] == pytest.approx(
            max(step["row_correction"], step["col_correction"]), abs=1e-12)
    assert conv["trajectory"][-1]["max_correction"] <= 1e-6


def test_p_value_is_plus_one_corrected_and_deterministic():
    payload = _payload(_additive_readings(), permutations=200, seed=11)
    b1 = client.post("/api/v1/arrangements/spatial-qc", json=payload).json()
    b2 = client.post("/api/v1/arrangements/spatial-qc", json=payload).json()
    assert b1 == b2
    pt = b1["permutation_test"]
    assert pt["permutations"] == 200
    assert pt["observed_statistic"] == b1["effects"]["max_abs_row_col_effect"]
    # Plus-one correction: p = (1 + exceedances) / (1 + n), always in (0, 1].
    # Responses round floats to 10 decimal places, so compare accordingly.
    assert pt["p_value"] == pytest.approx(
        (1 + pt["exceedances"]) / 201, abs=1e-9)
    assert 0.0 < pt["p_value"] <= 1.0


def test_zero_permutations_skips_test():
    body = client.post("/api/v1/arrangements/spatial-qc",
                       json=_payload(_additive_readings(), permutations=0)) .json()
    pt = body["permutation_test"]
    assert pt["permutations"] == 0
    assert pt["p_value"] is None
    assert pt["exceedances"] == 0


def test_different_seeds_change_permutation_stream():
    p1 = _payload(_additive_readings(), permutations=200, seed=1)
    p2 = _payload(_additive_readings(), permutations=200, seed=2)
    b1 = client.post("/api/v1/arrangements/spatial-qc", json=p1).json()
    b2 = client.post("/api/v1/arrangements/spatial-qc", json=p2).json()
    # Effects come from the same data; only the permutation stream changes.
    assert b1["effects"] == b2["effects"]
    for b in (b1, b2):
        assert 0.0 < b["permutation_test"]["p_value"] <= 1.0


# --------------------------------------------------------------------- 422


def _post(readings, **over):
    return client.post("/api/v1/arrangements/spatial-qc",
                       json=_payload(readings, **over))


def test_group_with_single_valid_reading_is_422():
    samples = [dict(s, group="GZ") if s["id"] == "gA8" else s
               for s in SAMPLES_4X4]
    r = client.post("/api/v1/arrangements/spatial-qc", json=_payload(
        _additive_readings(), samples=samples))
    assert r.status_code == 422
    body = r.json()
    assert "SPATIAL_GROUP_INSUFFICIENT" in body["infeasibility_categories"]
    assert "effects" not in body and "readings" not in body


def test_grid_not_covering_two_rows_or_cols_is_422():
    layout = {"s1": "A1", "s2": "A2", "s3": "A3"}
    samples = [{"id": "s1", "group": "G"},
               {"id": "s2", "group": "G"},
               {"id": "s3", "group": "G"}]
    readings = {"A1": 1.0, "A2": 2.0, "A3": 3.0}
    r = client.post("/api/v1/arrangements/spatial-qc", json=_payload(
        readings, plate={"type": "custom", "rows": 3, "columns": 3},
        layout=layout, samples=samples, permutations=0))
    assert r.status_code == 422
    assert "SPATIAL_GRID_INSUFFICIENT" in r.json()["infeasibility_categories"]


def test_non_convergence_is_422_without_partial_results():
    # 2x2 non-zero grid, one iteration, ultra-tight tolerance: the first
    # round correction is large, so the polish is declared unconverged.
    layout = {"s1": "A1", "s2": "A2", "s3": "B1", "s4": "B2"}
    samples = [{"id": f"s{i}", "group": "G"} for i in range(1, 5)]
    readings = {"A1": 1.0, "A2": 2.0, "B1": 3.0, "B2": 4.0}
    r = client.post("/api/v1/arrangements/spatial-qc", json=_payload(
        readings, plate={"type": "custom", "rows": 2, "columns": 2},
        layout=layout, samples=samples,
        permutations=0, tolerance=1e-12, max_iterations=1))
    assert r.status_code == 422
    body = r.json()
    assert body["infeasibility_categories"] == ["SPATIAL_NOT_CONVERGED"]
    assert "effects" not in body and "readings" not in body


def test_reading_on_unoccupied_well_is_422():
    # A well name outside the 4x4 plate: rejected as a bad well reference.
    r = _post({**_additive_readings(), "H1": 9.0})
    assert r.status_code == 422
    assert "WELL_REFERENCE_INVALID" in r.json()["infeasibility_categories"]

    # A well inside the plate but absent from the layout: rejected as a
    # reading for an unoccupied well.
    layout = dict(LAYOUT_4X4)
    del layout["gB5"]  # C2 now unoccupied
    samples = [s for s in SAMPLES_4X4 if s["id"] != "gB5"]
    r = client.post("/api/v1/arrangements/spatial-qc", json=_payload(
        {**_additive_readings(), "C2": 1.0}, layout=layout, samples=samples))
    assert r.status_code == 422
    assert "SPATIAL_READING_INVALID" in r.json()["infeasibility_categories"]


def test_unknown_sample_in_layout_is_422():
    layout = {**LAYOUT_4X4, "mystery": "D4"}
    del layout["gA8"]
    r = client.post("/api/v1/arrangements/spatial-qc", json=_payload(
        _additive_readings(), layout=layout))
    assert r.status_code == 422
    assert "SPATIAL_SAMPLE_UNKNOWN" in r.json()["infeasibility_categories"]


def test_unplaced_sample_is_422():
    samples = SAMPLES_4X4 + [{"id": "extra", "group": "GA"}]
    r = client.post("/api/v1/arrangements/spatial-qc", json=_payload(
        _additive_readings(), samples=samples))
    assert r.status_code == 422
    assert "SPATIAL_LAYOUT_CONFLICT" in r.json()["infeasibility_categories"]


def test_invalid_well_reference_is_422():
    r = _post({**_additive_readings(), "Z9": 1.0})
    assert r.status_code == 422
    assert "WELL_REFERENCE_INVALID" in r.json()["infeasibility_categories"]


def test_non_finite_reading_is_request_validation_error():
    import json

    raw = _payload(_additive_readings())
    raw["readings"]["A1"] = float("nan")
    r = client.post(
        "/api/v1/arrangements/spatial-qc",
        content=json.dumps(raw),  # Python emits the non-standard NaN token
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == ["REQUEST_VALIDATION_ERROR"]


def test_bad_payload_structure_is_422():
    r = client.post("/api/v1/arrangements/spatial-qc",
                    json={"plate": {"type": "96"}, "layout": {}})
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == ["REQUEST_VALIDATION_ERROR"]
