"""Tests for the deterministic dilution-gradient planning endpoint."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.dilution import dilution_plan
from app.main import app
from app.schemas import DilutionPlanRequest

client = TestClient(app)

PATH = "/api/v1/arrangements/dilution-plan"


def _payload(**overrides) -> dict:
    payload = {
        "plate": {"type": "96"},
        "layout": {"S1": "A1", "S2": "B2", "S3": "C3"},
        "sources": [
            {"sample_id": "S1", "concentration": 100.0, "available_volume_ul": 500.0},
            {"sample_id": "S2", "concentration": 200.0, "available_volume_ul": 50.0},
            {"sample_id": "S3", "concentration": 50.0, "available_volume_ul": 1000.0},
        ],
        "targets": [
            {"well": "A1", "target_concentration": 25.0, "final_volume_ul": 200.0},
            {"well": "B2", "target_concentration": 10.0, "final_volume_ul": 100.0},
            {"well": "C3", "target_concentration": 50.0, "final_volume_ul": 200.0},
        ],
        "well_capacity_ul": 300.0,
        "diluent": {"id": "water"},
        "pipette": {"resolution_ul": 1.0, "min_aspirate_ul": 5.0},
        "concentration_tolerance": 0.01,
    }
    payload.update(overrides)
    return payload


def test_plan_ok_volumes_concentrations_and_instructions():
    r = client.post(PATH, json=_payload())
    assert r.status_code == 200
    body = r.json()

    wells = {w["well"]: w for w in body["wells"]}
    # A1: 100 -> 25 at 200 µL => 50 stock + 150 diluent, exact.
    assert wells["A1"]["stock_volume_ul"] == 50.0
    assert wells["A1"]["diluent_volume_ul"] == 150.0
    assert wells["A1"]["actual_concentration"] == 25.0
    assert wells["A1"]["relative_concentration_error"] == 0.0
    # B2: 200 -> 10 at 100 µL => 5 stock + 95 diluent (at the min aspirate).
    assert wells["B2"]["stock_volume_ul"] == 5.0
    assert wells["B2"]["diluent_volume_ul"] == 95.0
    # C3: 50 -> 50 at 200 µL => pure stock, no diluent step emitted.
    assert wells["C3"]["stock_volume_ul"] == 200.0
    assert wells["C3"]["diluent_volume_ul"] == 0.0
    assert wells["C3"]["steps"] == [5]

    # Wells are emitted row-major; steps are global and contiguous.
    assert [w["well"] for w in body["wells"]] == ["A1", "B2", "C3"]
    assert [i["step"] for i in body["instructions"]] == [1, 2, 3, 4, 5]
    # Each well: diluent first (when present), then stock.
    assert body["instructions"][0]["action"] == "DISPENSE_DILUENT"
    assert body["instructions"][1] == {
        "step": 2, "action": "TRANSFER_STOCK", "well": "A1",
        "sample_id": "S1", "source_id": "S1", "volume_ul": 50.0,
    }
    # Pure-stock well has only a stock transfer.
    assert body["instructions"][-1]["action"] == "TRANSFER_STOCK"
    assert all(i["well"] == "C3" for i in body["instructions"][-1:])


def test_source_remaining_and_shared_inventory():
    body = client.post(PATH, json=_payload()).json()
    sources = {s["sample_id"]: s for s in body["sources"]}
    assert sources["S1"]["used_volume_ul"] == 50.0
    assert sources["S1"]["remaining_volume_ul"] == 450.0
    assert sources["S2"]["used_volume_ul"] == 5.0
    assert sources["S2"]["remaining_volume_ul"] == 45.0
    assert sources["S3"]["remaining_volume_ul"] == 800.0
    assert body["verification"] == {
        "all_wells_within_tolerance": True,
        "all_volumes_on_resolution_grid": True,
        "all_final_volumes_within_capacity": True,
        "all_sources_within_inventory": True,
    }


def test_one_source_shared_by_two_wells_depletes_one_pool():
    payload = _payload(
        layout={"S1": "A1", "S2": "A2"},
        sources=[{"sample_id": "S1", "concentration": 100.0, "available_volume_ul": 60.0}],
        targets=[
            {"well": "A1", "sample_id": "S1",
             "target_concentration": 50.0, "final_volume_ul": 60.0},
            {"well": "A2", "sample_id": "S1",
             "target_concentration": 50.0, "final_volume_ul": 60.0},
        ],
    )
    # Each well needs 30 µL from the single shared stock; 60 µL -> feasible.
    body = client.post(PATH, json=payload).json()
    used = {s["sample_id"]: s for s in body["sources"]}["S1"]
    assert used["used_volume_ul"] == 60.0
    assert used["remaining_volume_ul"] == 0.0
    assert used["feeds_wells"] == ["A1", "A2"]
    assert {w["well"]: w["sample_id"] for w in body["wells"]} == {
        "A1": "S1", "A2": "S1",
    }

    # One microlitre less of stock fails the whole request — no partial plan.
    payload["sources"][0]["available_volume_ul"] = 59.9
    r = client.post(PATH, json=payload)
    assert r.status_code == 422
    err = r.json()
    assert err["infeasibility_categories"] == ["DILUTION_SOURCE_STOCK_INSUFFICIENT"]
    assert "S1" in err["messages"][0]
    assert "wells" not in err and "instructions" not in err


def test_determinism_regardless_of_input_ordering():
    first = client.post(PATH, json=_payload()).json()
    reordered = _payload(
        sources=list(reversed(_payload()["sources"])),
        targets=list(reversed(_payload()["targets"])),
    )
    second = client.post(PATH, json=reordered).json()
    assert first == second
    # Direct calls are deterministic too.
    assert dilution_plan(DilutionPlanRequest(**_payload())) == first


def test_rounding_picks_nearest_grid_point_within_tolerance():
    # stock 100, target 25.3, final 100, resolution 1: ideal 25.3 -> draw 25.
    payload = _payload(
        layout={"S1": "A1"},
        sources=[{"sample_id": "S1", "concentration": 100.0, "available_volume_ul": 1000.0}],
        targets=[{"well": "A1", "target_concentration": 25.3, "final_volume_ul": 100.0}],
        pipette={"resolution_ul": 1.0, "min_aspirate_ul": 0},
        concentration_tolerance=0.02,
    )
    w = client.post(PATH, json=payload).json()["wells"][0]
    assert w["stock_volume_ul"] == 25.0
    assert w["actual_concentration"] == 25.0
    assert w["within_tolerance"] is True


def test_midpoint_tie_breaks_to_smaller_stock_draw():
    # ideal draw 35 µL is exactly midway between grid points 30 and 40;
    # both within a 20% tolerance -> inventory-minimal 30 µL is chosen.
    payload = _payload(
        layout={"S1": "A1"},
        sources=[{"sample_id": "S1", "concentration": 100.0, "available_volume_ul": 1000.0}],
        targets=[{"well": "A1", "target_concentration": 35.0, "final_volume_ul": 100.0}],
        pipette={"resolution_ul": 10.0, "min_aspirate_ul": 0},
        concentration_tolerance=0.20,
    )
    w = client.post(PATH, json=payload).json()["wells"][0]
    assert w["stock_volume_ul"] == 30.0
    assert w["diluent_volume_ul"] == 70.0


def test_decimal_resolution_grid():
    payload = _payload(
        layout={"S1": "A1"},
        sources=[{"sample_id": "S1", "concentration": 37.5, "available_volume_ul": 1000.0}],
        targets=[{"well": "A1", "target_concentration": 11.3, "final_volume_ul": 75.0}],
        pipette={"resolution_ul": 0.5, "min_aspirate_ul": 1.0},
        concentration_tolerance=0.005,
    )
    body = client.post(PATH, json=payload).json()
    w = body["wells"][0]
    # 22.5 + 52.5 = 75; both multiples of 0.5.
    assert w["stock_volume_ul"] == 22.5
    assert w["diluent_volume_ul"] == 52.5
    assert w["relative_concentration_error"] <= 0.005
    assert body["verification"]["all_volumes_on_resolution_grid"] is True


def test_tolerance_exceeded_after_rounding_is_422():
    payload = _payload(
        layout={"S1": "A1"},
        sources=[{"sample_id": "S1", "concentration": 100.0, "available_volume_ul": 1000.0}],
        targets=[{"well": "A1", "target_concentration": 5.5, "final_volume_ul": 100.0}],
        pipette={"resolution_ul": 10.0, "min_aspirate_ul": 0},
        concentration_tolerance=0.01,
    )
    r = client.post(PATH, json=payload)
    assert r.status_code == 422
    err = r.json()
    assert err["infeasibility_categories"] == ["DILUTION_ROUNDING_TOLERANCE_EXCEEDED"]
    assert "best achievable" in err["messages"][0]
    assert "instructions" not in err


def test_final_volume_off_grid_is_422():
    payload = _payload(
        targets=[{"well": "A1", "target_concentration": 50.0, "final_volume_ul": 100.5}],
    )
    r = client.post(PATH, json=payload)
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == ["DILUTION_ROUNDING_NO_GRID_SOLUTION"]


def test_capacity_exceeded_is_422():
    payload = _payload(
        targets=[{"well": "A1", "target_concentration": 50.0, "final_volume_ul": 300.5}],
    )
    r = client.post(PATH, json=payload)
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == ["DILUTION_TARGET_CAPACITY_EXCEEDED"]


def test_concentration_above_stock_is_422():
    payload = _payload(
        layout={"S1": "A1"},
        sources=[{"sample_id": "S1", "concentration": 100.0, "available_volume_ul": 1000.0}],
        targets=[{"well": "A1", "target_concentration": 120.0, "final_volume_ul": 100.0}],
    )
    r = client.post(PATH, json=payload)
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == [
        "DILUTION_CONCENTRATION_UNACHIEVABLE"
    ]


def test_sub_minimum_aspirate_is_422_without_partial_plan():
    # 5 µL final, 10 µL minimum aspirate: neither a mixed split nor pure
    # stock can be pipetted.
    payload = _payload(
        layout={"S1": "A1"},
        sources=[{"sample_id": "S1", "concentration": 100.0, "available_volume_ul": 1000.0}],
        targets=[{"well": "A1", "target_concentration": 50.0, "final_volume_ul": 5.0}],
        pipette={"resolution_ul": 5.0, "min_aspirate_ul": 10.0},
    )
    r = client.post(PATH, json=payload)
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == [
        "DILUTION_ROUNDING_NO_GRID_SOLUTION"
    ]


def test_pure_stock_bypasses_diluent_minimum():
    # Undiluted target: zero diluent step is omitted, so the 25 µL stock
    # draw is legal despite the 10 µL diluent minimum.
    payload = _payload(
        layout={"S1": "A1"},
        sources=[{"sample_id": "S1", "concentration": 100.0, "available_volume_ul": 1000.0}],
        targets=[{"well": "A1", "target_concentration": 100.0, "final_volume_ul": 25.0}],
        pipette={"resolution_ul": 5.0, "min_aspirate_ul": 10.0},
    )
    body = client.post(PATH, json=payload).json()
    assert body["wells"][0]["diluent_volume_ul"] == 0.0
    assert [i["action"] for i in body["instructions"]] == ["TRANSFER_STOCK"]


def test_unknown_source_well_and_duplicates_are_422():
    # Layout occupant missing from sources.
    r = client.post(PATH, json=_payload(
        sources=[{"sample_id": "S2", "concentration": 200.0, "available_volume_ul": 50.0},
                 {"sample_id": "S3", "concentration": 50.0, "available_volume_ul": 1000.0}],
    ))
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == ["DILUTION_SOURCE_UNKNOWN"]

    # Target well outside the plate / not occupied by the layout.
    r = client.post(PATH, json=_payload(
        targets=[{"well": "A9", "target_concentration": 1.0, "final_volume_ul": 100.0}],
    ))
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == ["WELL_REFERENCE_INVALID"]

    # Duplicated target well.
    r = client.post(PATH, json=_payload(
        sources=[{"sample_id": "S1", "concentration": 100.0, "available_volume_ul": 1000.0}],
        targets=[
            {"well": "A1", "target_concentration": 50.0, "final_volume_ul": 100.0},
            {"well": "A1", "target_concentration": 25.0, "final_volume_ul": 100.0},
        ],
    ))
    assert r.status_code == 422
    assert "DILUTION_TARGET_WELLS_DUPLICATED" in r.json()["infeasibility_categories"]

    # Duplicated source entry.
    r = client.post(PATH, json=_payload(
        sources=[
            {"sample_id": "S1", "concentration": 100.0, "available_volume_ul": 1000.0},
            {"sample_id": "S1", "concentration": 100.0, "available_volume_ul": 1000.0},
        ],
        targets=[{"well": "A1", "target_concentration": 50.0, "final_volume_ul": 100.0}],
    ))
    assert r.status_code == 422
    assert "DILUTION_SOURCES_DUPLICATED" in r.json()["infeasibility_categories"]


def test_multiple_problems_are_all_reported_together():
    # Well A1 over capacity *and* another well's concentration unreachable;
    # every reason must appear in a single 422 response.
    payload = _payload(
        sources=[
            {"sample_id": "S1", "concentration": 100.0, "available_volume_ul": 500.0},
            {"sample_id": "S2", "concentration": 200.0, "available_volume_ul": 50.0},
        ],
        targets=[
            {"well": "A1", "target_concentration": 50.0, "final_volume_ul": 400.0},
            {"well": "B2", "target_concentration": 999.0, "final_volume_ul": 100.0},
        ],
    )
    r = client.post(PATH, json=payload)
    assert r.status_code == 422
    cats = r.json()["infeasibility_categories"]
    assert cats == [
        "DILUTION_TARGET_CAPACITY_EXCEEDED",
        "DILUTION_CONCENTRATION_UNACHIEVABLE",
    ]
    assert len(r.json()["messages"]) == 2


def test_request_validation_errors_keep_422_shape():
    r = client.post(PATH, json={"layout": {"S1": "A1"}})
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == ["REQUEST_VALIDATION_ERROR"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("concentration_tolerance", 0.0),
        ("well_capacity_ul", 0.0),
    ],
)
def test_non_positive_parameters_rejected(field, value):
    payload = _payload(**{field: value})
    r = client.post(PATH, json=payload)
    assert r.status_code == 422
