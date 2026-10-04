"""Reconcile endpoint tests: validation (422), staging conflicts (409),
minimal deterministic move plans, and replay consistency."""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

PLATE = {"type": "24"}  # 4 rows x 6 cols: A1..D6


def _replay(scan, instructions):
    """Apply instructions to the scanned well->sample state."""
    state = dict(scan)
    for ins in instructions:
        frm, to, sid = ins["from"], ins["to"], ins["sample_id"]
        assert state.get(frm) == sid, f"step {ins['step']}: {sid} not at {frm}"
        assert to not in state, f"step {ins['step']}: destination {to} not empty"
        state[to] = state.pop(frm)
    return state


# --------------------------------------------------------------------- 200


def test_chain_moves_directly_into_empty_targets():
    # S2 sits on a non-target well; its target A2 is empty -> one move.
    r = client.post("/api/v1/arrangements/reconcile", json={
        "plate": PLATE,
        "target": {"S1": "A1", "S2": "A2"},
        "scan": {"A1": "S1", "B3": "S2"},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["move_count"] == 1
    assert body["cycles"] == []
    assert body["staging_well"] is None
    assert body["instructions"] == [
        {"step": 1, "sample_id": "S2", "from": "B3", "to": "A2"}
    ]
    assert body["final_mapping"] == {"S1": "A1", "S2": "A2"}
    assert body["misplaced_wells"] == [
        {"well": "A2", "expected": "S2", "actual": None},
        {"well": "B3", "expected": None, "actual": "S2"},
    ]
    assert body["verification"] == {
        "final_matches_target": True,
        "every_destination_empty_at_move": True,
        "staging_well_restored_empty": True,
    }


def test_longer_chain_resolved_backwards():
    # Chain C1 -> A1 -> A2 -> A3: the sample furthest along moves first.
    r = client.post("/api/v1/arrangements/reconcile", json={
        "plate": PLATE,
        "target": {"S1": "A1", "S2": "A2", "S3": "A3"},
        "scan": {"C1": "S1", "A1": "S2", "A2": "S3"},
    })
    assert r.status_code == 200
    body = r.json()
    assert [(i["sample_id"], i["from"], i["to"]) for i in body["instructions"]] == [
        ("S3", "A2", "A3"),
        ("S2", "A1", "A2"),
        ("S1", "C1", "A1"),
    ]
    assert body["move_count"] == 3
    assert body["final_mapping"] == {"S1": "A1", "S2": "A2", "S3": "A3"}


def test_cycle_uses_staging_well_and_restores_it():
    # 3-cycle: A1->A2->A3->A1, resolved via staging well B6.
    r = client.post("/api/v1/arrangements/reconcile", json={
        "plate": PLATE,
        "target": {"S1": "A1", "S2": "A2", "S3": "A3"},
        "scan": {"A1": "S2", "A2": "S3", "A3": "S1"},
        "staging_well": "B6",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["cycles"] == [["A1", "A2", "A3"]]
    assert body["staging_well"] == "B6"
    # k samples in one cycle -> k+1 moves (minimal).
    assert body["move_count"] == 4
    assert [(i["sample_id"], i["from"], i["to"]) for i in body["instructions"]] == [
        ("S2", "A1", "B6"),
        ("S1", "A3", "A1"),
        ("S3", "A2", "A3"),
        ("S2", "B6", "A2"),
    ]
    assert body["final_mapping"] == {"S1": "A1", "S2": "A2", "S3": "A3"}
    assert body["verification"]["staging_well_restored_empty"] is True


def test_two_cycle_never_swaps_occupied_wells():
    # A direct swap of two occupied wells is forbidden; staging breaks it.
    r = client.post("/api/v1/arrangements/reconcile", json={
        "plate": PLATE,
        "target": {"S1": "A1", "S2": "A2"},
        "scan": {"A1": "S2", "A2": "S1"},
        "staging_well": "C1",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["move_count"] == 3  # 2 misplaced samples + 1 cycle
    final_state = _replay(
        {"A1": "S2", "A2": "S1"}, body["instructions"]
    )
    assert final_state == {"A1": "S1", "A2": "S2"}
    assert body["verification"]["every_destination_empty_at_move"] is True


def test_chains_run_before_cycles():
    # One open chain (C2 -> B1) and one 2-cycle (A1 <-> A2).
    r = client.post("/api/v1/arrangements/reconcile", json={
        "plate": PLATE,
        "target": {"S1": "A1", "S2": "A2", "S3": "A3", "S4": "B1"},
        "scan": {"A1": "S2", "A2": "S1", "A3": "S3", "C2": "S4"},
        "staging_well": "D6",
    })
    assert r.status_code == 200
    body = r.json()
    steps = [(i["sample_id"], i["from"], i["to"]) for i in body["instructions"]]
    assert steps[0] == ("S4", "C2", "B1")  # chain first
    assert steps[1:] == [
        ("S2", "A1", "D6"),
        ("S1", "A2", "A1"),
        ("S2", "D6", "A2"),
    ]
    assert body["move_count"] == 4 == 3 + 1  # 3 misplaced samples + 1 cycle
    assert body["final_mapping"] == {"S1": "A1", "S2": "A2", "S3": "A3", "S4": "B1"}


def test_already_matching_scan_needs_no_moves():
    r = client.post("/api/v1/arrangements/reconcile", json={
        "plate": PLATE,
        "target": {"S1": "A1", "S2": "B2"},
        "scan": {"A1": "S1", "B2": "S2"},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["move_count"] == 0
    assert body["instructions"] == []
    assert body["misplaced_wells"] == []
    assert body["final_mapping"] == {"S1": "A1", "S2": "B2"}


def test_reconcile_is_deterministic():
    payload = {
        "plate": PLATE,
        "target": {"S1": "A1", "S2": "A2", "S3": "A3", "S4": "B1"},
        "scan": {"A1": "S2", "A2": "S1", "C2": "S4", "A3": "S3"},
        "staging_well": "D6",
    }
    r1 = client.post("/api/v1/arrangements/reconcile", json=payload).json()
    r2 = client.post("/api/v1/arrangements/reconcile", json=payload).json()
    assert r1 == r2


def test_replay_of_instructions_reaches_target():
    target = {f"S{i}": f"{r}{c}" for i, (r, c) in enumerate(
        [(r, c) for r in "AB" for c in "123"], start=1)}
    # Rotate every sample one well to the right (single 6-cycle) plus one
    # sample stranded off-target.
    wells = [target[s] for s in sorted(target, key=lambda s: int(s[1:]))]
    scan = {wells[(i + 1) % len(wells)]: f"S{i + 1}" for i in range(len(wells))}
    r = client.post("/api/v1/arrangements/reconcile", json={
        "plate": PLATE, "target": target, "scan": scan, "staging_well": "D6",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["move_count"] == len(target) + 1  # one 6-cycle
    assert _replay(scan, body["instructions"]) == {w: s for s, w in target.items()}
    assert body["final_mapping"] == target


# --------------------------------------------------------------------- 409


def test_cycle_without_staging_well_is_409_and_lists_cycles():
    r = client.post("/api/v1/arrangements/reconcile", json={
        "plate": PLATE,
        "target": {"S1": "A1", "S2": "A2"},
        "scan": {"A1": "S2", "A2": "S1"},
    })
    assert r.status_code == 409
    body = r.json()
    assert body["cycles"] == [["A1", "A2"]]
    assert "instructions" not in body
    assert body["staging_well"] is None
    assert body["problems"]


def test_multiple_cycles_all_listed_in_409():
    r = client.post("/api/v1/arrangements/reconcile", json={
        "plate": PLATE,
        "target": {"S1": "A1", "S2": "A2", "S3": "B1", "S4": "B2"},
        "scan": {"A1": "S2", "A2": "S1", "B1": "S4", "B2": "S3"},
    })
    assert r.status_code == 409
    assert r.json()["cycles"] == [["A1", "A2"], ["B1", "B2"]]


def test_unusable_staging_well_is_409():
    base = {
        "plate": PLATE,
        "target": {"S1": "A1", "S2": "A2"},
        "scan": {"A1": "S2", "A2": "S1"},
    }
    # Occupied staging well (A1 is also a target well here: both reported).
    r = client.post("/api/v1/arrangements/reconcile",
                    json={**base, "staging_well": "A1"})
    assert r.status_code == 409
    assert any("occupied" in p for p in r.json()["problems"])
    # Staging well that is a target well.
    r = client.post("/api/v1/arrangements/reconcile",
                    json={**base, "staging_well": "A2"})
    assert r.status_code == 409
    assert "target well" in r.json()["problems"][0]
    # Blocked staging well.
    r = client.post("/api/v1/arrangements/reconcile",
                    json={**base, "blocked_wells": ["C3"], "staging_well": "C3"})
    assert r.status_code == 409
    assert "blocked" in r.json()["problems"][0]
    # Staging well outside the plate.
    r = client.post("/api/v1/arrangements/reconcile",
                    json={**base, "staging_well": "Z9"})
    assert r.status_code == 409


def test_staging_well_ignored_when_no_cycle():
    # No closed loop: an absent/odd staging well must not matter.
    r = client.post("/api/v1/arrangements/reconcile", json={
        "plate": PLATE,
        "target": {"S1": "A1", "S2": "A2"},
        "scan": {"A1": "S1", "B3": "S2"},
    })
    assert r.status_code == 200
    assert r.json()["staging_well"] is None


# --------------------------------------------------------------------- 422


def test_duplicate_target_wells_422():
    r = client.post("/api/v1/arrangements/reconcile", json={
        "plate": PLATE,
        "target": {"S1": "A1", "S2": "a1"},  # same well after normalization
        "scan": {"A1": "S1", "A2": "S2"},
    })
    assert r.status_code == 422
    body = r.json()
    assert "TARGET_WELLS_DUPLICATED" in body["infeasibility_categories"]
    assert "instructions" not in body


def test_invalid_well_reference_422():
    r = client.post("/api/v1/arrangements/reconcile", json={
        "plate": PLATE,
        "target": {"S1": "Z9"},
        "scan": {"A1": "S1"},
    })
    assert r.status_code == 422
    assert "WELL_REFERENCE_INVALID" in r.json()["infeasibility_categories"]


def test_scan_sample_duplicated_422():
    r = client.post("/api/v1/arrangements/reconcile", json={
        "plate": PLATE,
        "target": {"S1": "A1", "S2": "A2"},
        "scan": {"A1": "S1", "A2": "S1"},
    })
    assert r.status_code == 422
    cats = r.json()["infeasibility_categories"]
    assert "SCAN_SAMPLES_DUPLICATED" in cats
    assert "SAMPLES_MISSING" in cats  # S2 never scanned


def test_scan_well_duplicated_after_normalization_422():
    r = client.post("/api/v1/arrangements/reconcile", json={
        "plate": PLATE,
        "target": {"S1": "A1", "S2": "A2"},
        "scan": {"A1": "S1", "a1 ": "S2"},
    })
    assert r.status_code == 422
    assert "SCAN_WELLS_DUPLICATED" in r.json()["infeasibility_categories"]


def test_missing_and_unplanned_samples_422():
    r = client.post("/api/v1/arrangements/reconcile", json={
        "plate": PLATE,
        "target": {"S1": "A1", "S2": "A2"},
        "scan": {"A1": "S1", "B4": "S9"},
    })
    assert r.status_code == 422
    cats = r.json()["infeasibility_categories"]
    assert "SAMPLES_MISSING" in cats
    assert "UNPLANNED_SAMPLES" in cats


def test_blocked_well_occupied_422():
    r = client.post("/api/v1/arrangements/reconcile", json={
        "plate": PLATE,
        "target": {"S1": "A2"},
        "scan": {"A1": "S1"},
        "blocked_wells": ["A1"],
    })
    assert r.status_code == 422
    assert "BLOCKED_WELL_OCCUPIED" in r.json()["infeasibility_categories"]


def test_target_on_blocked_well_422():
    r = client.post("/api/v1/arrangements/reconcile", json={
        "plate": PLATE,
        "target": {"S1": "A1"},
        "scan": {"A2": "S1"},
        "blocked_wells": ["A1"],
    })
    assert r.status_code == 422
    assert "TARGET_ON_BLOCKED" in r.json()["infeasibility_categories"]


def test_422_takes_precedence_over_409():
    # Inconsistent request AND an unstaged cycle: validation wins.
    r = client.post("/api/v1/arrangements/reconcile", json={
        "plate": PLATE,
        "target": {"S1": "A1", "S2": "A2"},
        "scan": {"A1": "S2", "A2": "S1", "B1": "S9"},
    })
    assert r.status_code == 422
    assert "UNPLANNED_SAMPLES" in r.json()["infeasibility_categories"]


def test_request_validation_error_422():
    r = client.post("/api/v1/arrangements/reconcile", json={"plate": PLATE})
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == ["REQUEST_VALIDATION_ERROR"]
    r = client.post("/api/v1/arrangements/reconcile",
                    json={"plate": PLATE, "target": {}})
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == ["REQUEST_VALIDATION_ERROR"]


def test_invalid_plate_dimension_422():
    r = client.post("/api/v1/arrangements/reconcile", json={
        "plate": {"type": "custom"},
        "target": {"S1": "A1"},
        "scan": {"A1": "S1"},
    })
    assert r.status_code == 422
    assert "PLATE_DIMENSION_INVALID" in r.json()["infeasibility_categories"]


def test_original_endpoint_untouched():
    r = client.post("/api/v1/arrangements", json={
        "plate": {"type": "24"},
        "samples": [{"id": f"s{i}", "group": "G", "batch": "B"} for i in range(5)],
        "seed": 7,
    })
    assert r.status_code == 200
    assert len(r.json()["layout"]) == 5
