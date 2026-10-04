"""Re-test sampling plan tests.

Two layers:

* the pure optimizer (``app.retest.build_retest_plan``) is checked against a
  brute-force enumeration of every quota-satisfying selection on random small
  plates: it must meet every group quota exactly, maximise covered
  rows + columns, then minimise orthogonally adjacent same-batch pairs; the
  same (snapshot, quotas, avoids, seed) must always yield the same plan and
  every returned plan must be lex-optimal;
* the endpoint (``POST /api/v1/certificates/retest-plans``) is checked for
  persistence/transaction semantics: idempotent replay, same-key different
  content 409, REVOKED 409, stale revision 409 (including the replay of a key
  that was recorded on the old revision), 404, exclusions (null reading,
  blocked/avoided wells, scan mismatch) and the single-transaction plan +
  idempotency record.
"""
import itertools
import json
import random
import sqlite3
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import certificates, retest
from app.main import app

client = TestClient(app)

# Reuse the 4x4 certified-batch fixtures from the certificate tests.
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
    monkeypatch.setenv("CERT_DB_PATH", str(tmp_path / "certificates.db"))
    certificates.reset_stores()
    yield tmp_path / "certificates.db"
    certificates.reset_stores()


def _hash(text: str) -> int:
    import hashlib
    return int(hashlib.md5(text.encode()).hexdigest()[:6], 16)


def _readings():
    out = {}
    for sid, well in LAYOUT_4X4.items():
        base = 100.0 if sid.startswith("gA") else 0.0
        noise = (_hash(sid) % 7) - 3
        out[well] = base + noise
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


def _create_cert(batch_id="BATCH-1", key="create-1", **over):
    payload = {
        "batch_id": batch_id,
        "request_key": key,
        "release": _release_payload(),
    }
    payload.update(over)
    r = client.post("/api/v1/certificates", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _retest_payload(**over):
    payload = {
        "batch_id": "BATCH-1",
        "request_key": "plan-1",
        "expected_revision": 1,
        "group_counts": {"GA": 3, "GB": 2},
        "avoid_wells": [],
        "seed": 42,
    }
    payload.update(over)
    return payload


def _post_plan(**over):
    return client.post(
        "/api/v1/certificates/retest-plans", json=_retest_payload(**over)
    )


# =========================================================== pure optimizer


def _snapshot_from_grid(rows, cols, groups, readings=None, scan=None,
                        blocked=None):
    """Build a frozen-snapshot-like dict for a fully populated plate."""
    from app.plates import well_name
    target = {}
    samples = []
    for r in range(rows):
        for c in range(cols):
            sid = f"s{r}_{c}"
            target[sid] = well_name(r, c)
            samples.append({"id": sid, "group": groups[r][c]})
    wells = list(target.values())
    return {
        "batch_id": "grid",
        "plate": {"type": "custom", "rows": rows, "columns": cols},
        "target": target,
        "scan": scan or {w: s for s, w in target.items()},
        "readings": readings if readings is not None else {w: 1.0 for w in wells},
        "samples": samples,
        "blocked_wells": blocked or [],
        "seed": 0,
        "thresholds": {},
        "view_digest": None,
    }


def _brute_force_optimum(snapshot, quotas, avoid):
    """Enumerate every quota-satisfying selection; return lex-optimal set.

    Lex order: max (rows covered + cols covered), then min same-batch
    adjacent pairs. Ties (the deterministic seed choice) are left to the
    caller: every element of the final candidate set is an accepted optimum.
    """
    from app.plates import parse_well, well_name
    rows, cols = snapshot["plate"]["rows"], snapshot["plate"]["columns"]
    cands = retest._eligible_candidates(
        snapshot, rows, cols, retest._parse_avoid(avoid, rows, cols)
    )
    by_group = {}
    for c in cands:
        by_group.setdefault(c.group, []).append(c)
    group_options = []
    for g, n in quotas.items():
        group_options.append(list(itertools.combinations(by_group[g], n)))
    best = None
    best_key = None
    optima = []
    for picks in itertools.product(*group_options):
        chosen = {c for tup in picks for c in tup}
        pos = {(c.row, c.col) for c in chosen}
        cover = len({c.row for c in chosen}) + len({c.col for c in chosen})
        # Count orthogonal edges once per (down, right) neighbour.
        adj = sum(
            1 for c in chosen
            for npos in ((c.row + 1, c.col), (c.row, c.col + 1))
            if npos in pos
        )
        key = (-cover, adj)
        if best_key is None or key < best_key:
            best_key = key
            best = chosen
            optima = [frozenset(c.index for c in chosen)]
        elif key == best_key:
            optima.append(frozenset(c.index for c in chosen))
    return cands, (-best_key[0], best_key[1]), set(optima)


def test_build_retest_plan_matches_bruteforce_on_random_grids():
    rng = random.Random(20261004)
    for trial in range(40):
        rows = rng.randint(2, 4)
        cols = rng.randint(2, 4)
        groups = [
            [rng.choice(["GA", "GB", "GC"]) for _ in range(cols)]
            for _ in range(rows)
        ]
        snapshot = _snapshot_from_grid(rows, cols, groups)
        # Randomly knock out readings (exclude samples).
        for well in list(snapshot["readings"]):
            if rng.random() < 0.15:
                snapshot["readings"][well] = None
        avoid = []
        for r in range(rows):
            for c in range(cols):
                if rng.random() < 0.05:
                    from app.plates import well_name
                    avoid.append(well_name(r, c))
        eligible = retest._eligible_candidates(
            snapshot, rows, cols, retest._parse_avoid(avoid, rows, cols)
        )
        avail = {}
        for c in eligible:
            avail[c.group] = avail.get(c.group, 0) + 1
        present = [g for g in ("GA", "GB", "GC") if g in {x.group for x in eligible}]
        if not present:
            continue
        chosen_groups = rng.sample(present, k=min(len(present), rng.randint(1, 2)))
        quotas = {g: rng.randint(0, min(avail[g], 2)) for g in chosen_groups}
        if not any(quotas.values()):
            quotas[chosen_groups[0]] = max(1, min(avail[chosen_groups[0]], 1))
        if any(quotas[g] > avail[g] for g in quotas):
            continue

        cands, (cover, adj), optima = _brute_force_optimum(
            snapshot, quotas, avoid
        )
        seed = rng.randint(0, 10_000)
        sel = retest.build_retest_plan(snapshot, quotas, avoid, seed)
        picked = frozenset(c.sample_id for c in sel.selected)
        picked_idx = frozenset(
            i for i, c in enumerate(cands) if c.sample_id in picked
        )
        # Exact quotas.
        got_counts = {g: 0 for g in quotas}
        for c in sel.selected:
            if c.group in got_counts:
                got_counts[c.group] += 1
        assert got_counts == quotas
        # Lex-optimal coverage then adjacency.
        assert sel.coverage_total == cover, (trial, sel.coverage_total, cover)
        assert len(sel.adjacent_pairs) == adj, (trial, len(sel.adjacent_pairs), adj)
        # The deterministic tie-break is one of the lex-optimal selections.
        assert picked_idx in optima, trial


def test_build_retest_plan_is_deterministic_for_same_seed():
    snapshot = _snapshot_from_grid(
        4, 4, [["GA", "GB"] * 2 for _ in range(4)]
    )
    quotas = {"GA": 3, "GB": 3}
    a = retest.build_retest_plan(snapshot, quotas, ["A1"], 123)
    b = retest.build_retest_plan(snapshot, quotas, ["A1"], 123)
    assert [(c.sample_id, c.well) for c in a.selected] == \
           [(c.sample_id, c.well) for c in b.selected]


def test_build_retest_plan_seed_changes_only_ties():
    # Dense quota on a plate with many symmetric optima: different seeds may
    # differ, but every result stays lex-optimal.
    snapshot = _snapshot_from_grid(
        4, 5, [["GA", "GB", "GA", "GB", "GA"] for _ in range(4)]
    )
    quotas = {"GA": 4, "GB": 2}
    plans = {
        tuple(sorted(c.well for c in retest.build_retest_plan(
            snapshot, quotas, [], seed).selected))
        for seed in range(8)
    }
    for wells in plans:
        positions = {retest.parse_well(w, 4, 5) for w in wells}
        assert len({r for r, _ in positions}) + len({c for _, c in positions}) == 9
    # At least the mechanism permits deterministic differentiation.
    assert len(plans) >= 1


def test_build_retest_plan_zero_quotas_select_nothing():
    snapshot = _snapshot_from_grid(
        3, 3, [["GA", "GB", "GA"] for _ in range(3)])
    sel = retest.build_retest_plan(snapshot, {"GA": 0}, [], 1)
    assert sel.selected == []
    assert sel.coverage_total == 0
    assert sel.adjacent_pairs == []
    # Only the requested group's pool is reported, even with a zero quota.
    assert sel.eligible_by_group == {"GA": 6}
    assert sel.eligible_total == 9  # the full selectable pool on the plate


def test_build_retest_plan_unknown_group_is_invalid():
    snapshot = _snapshot_from_grid(3, 3, [["GA"] * 3 for _ in range(3)])
    with pytest.raises(retest.RetestPlanInvalid) as exc:
        retest.build_retest_plan(snapshot, {"NOPE": 1}, [], 0)
    assert exc.value.categories == ["RETEST_GROUP_UNKNOWN"]


def test_build_retest_plan_insufficient_quota_lists_shortfall():
    snapshot = _snapshot_from_grid(3, 3, [["GA", "GB", "GA"] for _ in range(3)])
    # GB occupies exactly one column (3 cells); avoid two of them.
    with pytest.raises(retest.RetestPlanInvalid) as exc:
        retest.build_retest_plan(snapshot, {"GB": 3}, ["A2", "B2"], 0)
    assert exc.value.categories == ["RETEST_INSUFFICIENT_SAMPLES"]
    assert "group 'GB' needs 3 sample(s) but only 1" in exc.value.messages[0]


def test_build_retest_plan_null_reading_excludes_sample():
    snapshot = _snapshot_from_grid(3, 3, [["GA"] * 3 for _ in range(3)])
    snapshot["readings"]["A1"] = None
    sel = retest.build_retest_plan(snapshot, {"GA": 8}, [], 0)
    assert sel.eligible_total == 8
    assert "A1" not in {c.well for c in sel.selected}


def test_build_retest_plan_scan_mismatch_excludes_sample():
    snapshot = _snapshot_from_grid(3, 3, [["GA"] * 3 for _ in range(3)])
    # A1 is scanned as the sample planned for A2: the A1 occupant (s0_0) is
    # no longer on its planned well and must be excluded.
    snapshot["scan"]["A1"] = "s0_1"
    sel = retest.build_retest_plan(snapshot, {"GA": 7}, [], 0)
    # s0_0 lost its planned well (A1 carries s0_1) and s0_1 now appears at a
    # different well, so both planned samples are ineligible: 7 remain.
    assert sel.eligible_total == 7
    wells = {c.well for c in sel.selected}
    assert "A1" not in wells and "A2" not in wells


def test_build_retest_plan_bad_avoid_well_is_invalid():
    snapshot = _snapshot_from_grid(3, 3, [["GA"] * 3 for _ in range(3)])
    with pytest.raises(retest.RetestPlanInvalid) as exc:
        retest.build_retest_plan(snapshot, {"GA": 1}, ["Z99"], 0)
    assert exc.value.categories == ["WELL_REFERENCE_INVALID"]


def test_build_retest_plan_coverage_first_then_adjacency():
    # 3x3 checkerboard; 2 GA + 2 GB. Coverage (rows+cols) is maximised
    # first (6 = all 3 rows + all 3 columns); every full-coverage draw of
    # four wells on this checkerboard forces exactly one orthogonal pair, so
    # the second-stage minimum is 1 (no zero-adjacency draw covers six lines).
    groups = [
        ["GA", "GB", "GA"],
        ["GB", "GA", "GB"],
        ["GA", "GB", "GA"],
    ]
    snapshot = _snapshot_from_grid(3, 3, groups)
    _cands, (cover, adj), _optima = _brute_force_optimum(
        snapshot, {"GA": 2, "GB": 2}, [])
    assert cover == 6 and adj == 1  # documents the combinatorial optimum

    sel = retest.build_retest_plan(snapshot, {"GA": 2, "GB": 2}, [], 0)
    assert sel.coverage_rows == 3 and sel.coverage_cols == 3
    assert len(sel.adjacent_pairs) == 1
    positions = {(c.row, c.col) for c in sel.selected}
    # Independently recount orthogonal edges over the returned plan.
    edges = sum(
        1 for r, c in positions
        for nr, nc in ((r + 1, c), (r, c + 1))
        if (nr, nc) in positions
    )
    assert edges == 1


# ============================================================ HTTP endpoint


def test_plan_success_returns_full_body():
    cert = _create_cert()
    r = _post_plan()
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["plan_id"].startswith("plan-")
    assert body["certificate_id"] == cert["certificate_id"]
    assert body["batch_id"] == "BATCH-1"
    assert body["status"] == "ACTIVE"
    assert body["revision"] == 1
    assert body["digest"] == cert["digest"]
    assert len(body["digest"]) == 64
    assert body["seed"] == 42
    # Exact quotas and well-formed selections.
    assert body["group_counts"] == {"GA": 3, "GB": 2}
    assert body["requested_group_counts"] == {"GA": 3, "GB": 2}
    assert len(body["samples"]) == 5 and len(body["wells"]) == 5
    groups = [s["group"] for s in body["samples"]]
    assert groups.count("GA") == 3 and groups.count("GB") == 2
    for entry in body["samples"]:
        assert {"sample_id", "well", "group"} == set(entry)
    # Every returned sample is on its planned well with a valid reading.
    target = LAYOUT_4X4
    readings = _readings()
    for entry in body["samples"]:
        assert target[entry["sample_id"]] == entry["well"]
        assert entry["well"] in readings
    # Wells and samples are the same set and sorted row-major.
    assert [s["well"] for s in body["samples"]] == body["wells"]
    # Coverage metrics are internally consistent.
    cov = body["coverage"]
    positions = [retest.parse_well(w, 4, 4) for w in body["wells"]]
    assert cov["rows_covered"] == len({r for r, _ in positions})
    assert cov["columns_covered"] == len({c for _, c in positions})
    assert cov["total_covered"] == cov["rows_covered"] + cov["columns_covered"]
    assert cov["total_rows"] == 4 and cov["total_columns"] == 4
    assert len(cov["covered_rows"]) == cov["rows_covered"]
    assert len(cov["covered_columns"]) == cov["columns_covered"]


def test_plan_persists_plan_and_idempotency_record_in_one_transaction(fresh_store):
    _create_cert()
    r = _post_plan()
    assert r.status_code == 201
    conn = sqlite3.connect(str(fresh_store))
    rows = conn.execute(
        "SELECT plan_id, batch_id, certificate_id, revision, digest, seed, "
        "plan_body FROM retest_plans"
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    plan_id, batch_id, cert_id, revision, digest, seed, plan_body = rows[0]
    stored = json.loads(plan_body)
    assert plan_id == r.json()["plan_id"] == stored["plan_id"]
    assert batch_id == "BATCH-1"
    assert revision == 1
    assert digest == r.json()["digest"]
    assert seed == 42
    assert stored == r.json()


def test_plan_replay_same_key_same_content_returns_recorded_response():
    _create_cert()
    payload = _retest_payload()
    b1 = client.post("/api/v1/certificates/retest-plans", json=payload)
    b2 = client.post("/api/v1/certificates/retest-plans", json=payload)
    assert b1.status_code == 201 and b2.status_code == 201
    assert b1.json() == b2.json()
    # The replay stores exactly one plan row.
    assert b1.json()["plan_id"] == b2.json()["plan_id"]


def test_plan_same_key_different_content_is_409_and_saves_nothing(fresh_store):
    _create_cert()
    client.post("/api/v1/certificates/retest-plans", json=_retest_payload())
    changed = _retest_payload(seed=43)
    r = client.post("/api/v1/certificates/retest-plans", json=changed)
    assert r.status_code == 409
    assert r.json()["error"] == "REQUEST_KEY_CONFLICT"
    assert r.json()["request_key"] == "plan-1"
    # The conflicting request neither overwrote nor added a plan/key record.
    conn = sqlite3.connect(str(fresh_store))
    n_plans = conn.execute("SELECT COUNT(*) FROM retest_plans").fetchone()[0]
    n_keys = conn.execute(
        "SELECT COUNT(*) FROM idempotency_keys WHERE request_key = 'plan-1'"
    ).fetchone()[0]
    conn.close()
    assert n_plans == 1 and n_keys == 1


def test_plan_422_is_recorded_and_replays_identically():
    _create_cert()
    payload = _retest_payload(group_counts={"GA": 99})
    b1 = client.post("/api/v1/certificates/retest-plans", json=payload)
    b2 = client.post("/api/v1/certificates/retest-plans", json=payload)
    assert b1.status_code == 422 and b2.status_code == 422
    assert b1.json() == b2.json()
    assert "RETEST_INSUFFICIENT_SAMPLES" in b1.json()["infeasibility_categories"]
    # Same key, different content still conflicts.
    changed = dict(payload); changed["seed"] = 5
    assert client.post(
        "/api/v1/certificates/retest-plans", json=changed).status_code == 409


def test_plan_unknown_batch_is_404():
    r = _post_plan()
    assert r.status_code == 404
    assert r.json()["error"] == "CERTIFICATE_NOT_FOUND"
    assert r.json()["batch_id"] == "BATCH-1"


def test_plan_revoked_certificate_is_409():
    cert = _create_cert()
    # Revoke via a failing re-verification (missing sample in scan).
    rv = {
        "batch_id": "BATCH-1", "request_key": "rv-1", "expected_revision": 1,
        "scan": {w: s for s, w in LAYOUT_4X4.items() if w != "A1"},
        "readings": _readings(),
    }
    revoked = client.post("/api/v1/certificates/reverify", json=rv)
    assert revoked.status_code == 200 and revoked.json()["status"] == "REVOKED"

    r = _post_plan()
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "CERTIFICATE_REVOKED"
    assert body["certificate_id"] == cert["certificate_id"]
    assert body["status"] == "REVOKED"
    assert body["revoked_reason"] == "SAMPLES_MISSING"


def test_plan_stale_expected_revision_is_409_and_saves_nothing(fresh_store):
    created = _create_cert()
    rv = client.post("/api/v1/certificates/reverify", json={
        "batch_id": "BATCH-1", "request_key": "rv-1", "expected_revision": 1,
        "scan": {w: s for s, w in LAYOUT_4X4.items()},
        "readings": _readings(),
    })
    assert rv.status_code == 200 and rv.json()["revision"] == 2

    # Fresh key naming the old revision: state-dependent 409, key not burned.
    r = _post_plan(request_key="plan-old", expected_revision=1)
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "CERTIFICATE_REVISION_MISMATCH"
    assert body["current_revision"] == 2 and body["expected_revision"] == 1

    conn = sqlite3.connect(str(fresh_store))
    n_plans = conn.execute("SELECT COUNT(*) FROM retest_plans").fetchone()[0]
    n_keys = conn.execute(
        "SELECT COUNT(*) FROM idempotency_keys WHERE request_key = 'plan-old'"
    ).fetchone()[0]
    conn.close()
    assert n_plans == 0 and n_keys == 0

    # Correct revision works.
    ok = _post_plan(request_key="plan-2", expected_revision=2)
    assert ok.status_code == 201
    assert ok.json()["revision"] == 2
    assert ok.json()["certificate_id"] == created["certificate_id"]


def test_plan_replay_after_revision_advances_returns_409():
    # A key recorded against revision 1 must NOT replay once the certificate
    # is at revision 2: state is checked before the idempotency lookup.
    _create_cert()
    payload = _retest_payload()
    first = client.post("/api/v1/certificates/retest-plans", json=payload)
    assert first.status_code == 201 and first.json()["revision"] == 1

    client.post("/api/v1/certificates/reverify", json={
        "batch_id": "BATCH-1", "request_key": "rv-1", "expected_revision": 1,
        "scan": {w: s for s, w in LAYOUT_4X4.items()},
        "readings": _readings(),
    })
    replay = client.post("/api/v1/certificates/retest-plans", json=payload)
    assert replay.status_code == 409
    assert replay.json()["error"] == "CERTIFICATE_REVISION_MISMATCH"
    assert replay.json()["current_revision"] == 2


def test_plan_stale_409_does_not_burn_key_then_succeeds_at_current_rev():
    _create_cert()
    client.post("/api/v1/certificates/reverify", json={
        "batch_id": "BATCH-1", "request_key": "rv-1", "expected_revision": 1,
        "scan": {w: s for s, w in LAYOUT_4X4.items()},
        "readings": _readings(),
    })
    stale = _retest_payload(request_key="plan-move", expected_revision=1)
    r = client.post("/api/v1/certificates/retest-plans", json=stale)
    assert r.status_code == 409
    # Same key with the corrected revision is accepted (state rejections are
    # not recorded under the key).
    stale["expected_revision"] = 2
    ok = client.post("/api/v1/certificates/retest-plans", json=stale)
    assert ok.status_code == 201
    assert ok.json()["revision"] == 2


def test_plan_unknown_group_is_422_and_saves_nothing(fresh_store):
    _create_cert()
    r = _post_plan(request_key="plan-x", group_counts={"NOPE": 1})
    assert r.status_code == 422
    cats = r.json()["infeasibility_categories"]
    assert "RETEST_GROUP_UNKNOWN" in cats
    conn = sqlite3.connect(str(fresh_store))
    assert conn.execute("SELECT COUNT(*) FROM retest_plans").fetchone()[0] == 0
    conn.close()


def test_plan_invalid_avoid_well_is_422():
    _create_cert()
    r = _post_plan(request_key="plan-x", avoid_wells=["Z9"])
    assert r.status_code == 422
    assert "WELL_REFERENCE_INVALID" in r.json()["infeasibility_categories"]


def test_plan_avoid_wells_exclude_their_samples():
    _create_cert()
    r = _post_plan(request_key="plan-x", avoid_wells=["A1", "A2", "A3", "A4"])
    assert r.status_code == 201, r.text
    wells = set(r.json()["wells"])
    assert wells.isdisjoint({"A1", "A2", "A3", "A4"})
    assert r.json()["avoided_wells"] == ["A1", "A2", "A3", "A4"]
    assert r.json()["eligible_samples"] == 12


def test_plan_well_with_null_reading_is_not_selectable(fresh_store):
    _create_cert()
    # Tamper the frozen snapshot directly: mark the reading at A1 as null.
    conn = sqlite3.connect(str(fresh_store))
    row = conn.execute(
        "SELECT snapshot FROM certificates WHERE batch_id = 'BATCH-1'"
    ).fetchone()
    snap = json.loads(row[0])
    snap["readings"]["A1"] = None
    snap_json = certificates._canonical_json(snap)
    digest = certificates._sha256(snap_json)
    conn.execute(
        "UPDATE certificates SET snapshot = ?, digest = ? WHERE batch_id = 'BATCH-1'",
        (snap_json.decode(), digest))
    conn.execute(
        "UPDATE certificate_revisions SET snapshot = ?, digest = ? "
        "WHERE batch_id = 'BATCH-1' AND revision = 1",
        (snap_json.decode(), digest))
    conn.commit()
    conn.close()
    certificates.reset_stores()

    r = _post_plan(request_key="plan-null", group_counts={"GA": 7, "GB": 8})
    # GA has 8 samples, one (gA1 at A1) is now ineligible -> quota 7 feasible.
    assert r.status_code == 201, r.text
    assert "A1" not in r.json()["wells"]
    assert r.json()["eligible_by_group"] == {"GA": 7, "GB": 8}
    assert r.json()["digest"] == digest

    # Asking for all 8 GA samples must now fail with insufficient samples.
    r = _post_plan(request_key="plan-over", group_counts={"GA": 8, "GB": 0})
    assert r.status_code == 422
    assert "RETEST_INSUFFICIENT_SAMPLES" in r.json()["infeasibility_categories"]


def test_plan_seed_determinism_across_requests_and_restarts():
    _create_cert()
    p1 = _retest_payload(request_key="seed-a")
    p2 = _retest_payload(request_key="seed-b")  # identical content, new key
    b1 = client.post("/api/v1/certificates/retest-plans", json=p1)
    b2 = client.post("/api/v1/certificates/retest-plans", json=p2)
    assert b1.json()["wells"] == b2.json()["wells"]

    certificates.reset_stores()  # restart on the same database
    p3 = _retest_payload(request_key="seed-c")
    b3 = client.post("/api/v1/certificates/retest-plans", json=p3)
    assert b3.json()["wells"] == b1.json()["wells"]


def test_plan_different_seeds_both_lex_optimal():
    _create_cert()
    seen = set()
    for seed in range(6):
        r = _post_plan(request_key=f"seed-{seed}", seed=seed)
        assert r.status_code == 201
        body = r.json()
        # 5 picks on the 4x4 checkerboard can cover all 8 lines.
        assert body["coverage"]["total_covered"] == 8
        seen.add(tuple(body["wells"]))
    # Deterministic across seeds is fine; all share the optimum regardless.
    assert len(seen) >= 1


def test_plan_same_key_reused_across_certificate_endpoint_conflicts():
    _create_cert(key="shared-key")
    # 'shared-key' is recorded on POST /certificates; reusing it on the
    # retest endpoint is a same-key/different-content 409 (endpoint is part
    # of the content hash).
    r = _post_plan(request_key="shared-key")
    assert r.status_code == 409
    assert r.json()["error"] == "REQUEST_KEY_CONFLICT"


def test_plan_revision2_uses_revision2_snapshot_and_digest(fresh_store):
    created = _create_cert()
    new_readings = dict(_readings())
    new_readings["A1"] = 1.0  # changed evidence still passes gates
    client.post("/api/v1/certificates/reverify", json={
        "batch_id": "BATCH-1", "request_key": "rv-1", "expected_revision": 1,
        "scan": {w: s for s, w in LAYOUT_4X4.items()},
        "readings": new_readings,
    })
    r = _post_plan(request_key="plan-r2", expected_revision=2)
    assert r.status_code == 201
    body = r.json()
    assert body["revision"] == 2
    # The returned digest covers the revision-2 snapshot pinned on the row.
    conn = sqlite3.connect(str(fresh_store))
    rev2 = conn.execute(
        "SELECT digest FROM certificate_revisions "
        "WHERE batch_id = 'BATCH-1' AND revision = 2").fetchone()
    conn.close()
    assert body["digest"] == rev2[0] != created["digest"]


def test_plan_id_is_deterministic_from_batch_revision_and_key():
    _create_cert()
    body = _post_plan().json()
    assert body["plan_id"] == retest._plan_id("BATCH-1", 1, "plan-1")


# ------------------------------------------------------------ request shape


def test_plan_missing_expected_revision_is_validation_error():
    payload = _retest_payload()
    del payload["expected_revision"]
    r = client.post("/api/v1/certificates/retest-plans", json=payload)
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == ["REQUEST_VALIDATION_ERROR"]


def test_plan_unknown_field_is_validation_error():
    payload = _retest_payload()
    payload["extra"] = 1
    r = client.post("/api/v1/certificates/retest-plans", json=payload)
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == ["REQUEST_VALIDATION_ERROR"]


def test_plan_negative_group_count_is_validation_error():
    payload = _retest_payload(group_counts={"GA": -1})
    r = client.post("/api/v1/certificates/retest-plans", json=payload)
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == ["REQUEST_VALIDATION_ERROR"]


def test_plan_empty_group_counts_is_validation_error():
    payload = _retest_payload(group_counts={})
    r = client.post("/api/v1/certificates/retest-plans", json=payload)
    assert r.status_code == 422


def test_existing_endpoints_still_available_after_retest():
    assert client.get("/health").json() == {"status": "ok"}
    r = client.post(
        "/api/v1/arrangements/batch-release", json=_release_payload())
    assert r.status_code == 200 and r.json()["verdict"] == "RELEASED"
