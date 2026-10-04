"""Re-test sampling plans drawn from the current revision of a certificate.

``POST /api/v1/certificates/retest-plans`` takes ``batch_id``,
``expected_revision``, an exact per-experiment-group sample quota, the wells
to avoid and a seed. The plan is drawn from the layout, sample grouping,
scan and readings frozen in the named revision snapshot of an ACTIVE
certificate: a sample is selectable only when its well holds it in the
frozen scan, the well is neither blocked nor on the avoid list and the well
has a valid (finite, non-null) reading. Every group quota must be met
exactly.

Selection optimality is lexicographic (CP-SAT):

1. maximise the number of covered rows plus covered columns;
2. minimise the number of orthogonally adjacent same-batch pairs among the
   selected wells;
3. a deterministic, seed-governed tie-break (the stage-2 optimum is fed back
   as a complete hint and the first solution instantiates it), so identical
   (snapshot, quotas, avoid wells, seed) always yields the identical plan.

The certificate state read, the frozen snapshot lookup, the plan insert and
the idempotency record all commit (or roll back) in one SQLite transaction.
State-dependent rejections (no certificate / REVOKED / stale revision) are
not recorded under the ``request_key``; content-dependent failures (unknown
group, insufficient eligible samples) are recorded like any other response.
"""
from __future__ import annotations

import json
import math
import os
import random
import time
from dataclasses import dataclass
from typing import Optional

from ortools.sat.python import cp_model

from .certificates import (
    STATUS_ACTIVE,
    STATUS_REVOKED,
    _content_hash,
    _get_certificate,
    _idem_record,
    _idem_replay,
    _sha256,
    _utcnow,
    get_store,
)
from .plates import parse_well, well_name
from .planner import SolverTimeout, _make_solver
from .schemas import (
    CERTIFICATE_NOT_FOUND,
    CERTIFICATE_REVISION_MISMATCH,
    CERTIFICATE_REVOKED,
    VIOLATION_RETEST_GROUP_UNKNOWN,
    VIOLATION_RETEST_INSUFFICIENT_SAMPLES,
    VIOLATION_RETEST_NO_SOLUTION,
    VIOLATION_WELL_REFERENCE_INVALID,
    RetestPlanRequest,
)

_ENDPOINT_RETEST = "POST /api/v1/certificates/retest-plans"


class RetestPlanInvalid(Exception):
    """No re-test plan can be drawn from the revision (HTTP 422).

    No plan row and no idempotency record is written by the pure builder;
    the endpoint transaction records the 422 under the request key.
    """

    def __init__(self, categories: list[str], messages: list[str]):
        self.categories = categories
        self.messages = messages
        super().__init__("; ".join(messages) if messages else "invalid re-test plan request")


def retest_invalid_body(exc: RetestPlanInvalid) -> dict:
    """Canonical 422 envelope for an undrawable re-test plan."""
    return {
        "detail": "No re-test sampling plan can be drawn from the certificate "
        "revision; no plan was saved.",
        "infeasibility_categories": exc.categories,
        "messages": exc.messages,
    }


@dataclass(frozen=True)
class _Candidate:
    index: int
    sample_id: str
    group: str
    row: int
    col: int

    @property
    def well(self) -> str:
        return well_name(self.row, self.col)


@dataclass
class RetestSelection:
    rows: int
    columns: int
    selected: list[_Candidate]
    adjacent_pairs: list[tuple[str, str]]
    eligible_by_group: dict[str, int]
    eligible_total: int
    coverage_rows: int
    coverage_cols: int
    coverage_total: int
    stages: list[tuple[str, int, str, bool, float]]


# ----------------------------------------------------------- snapshot -> pool


def _parse_avoid(avoid_wells: list[str], rows: int, cols: int) -> set[tuple[int, int]]:
    """Canonicalise the avoid list; a malformed/off-plate well is a 422."""
    categories: list[str] = []
    messages: list[str] = []
    avoided: set[tuple[int, int]] = set()
    for raw in avoid_wells:
        try:
            avoided.add(parse_well(raw, rows, cols))
        except ValueError as exc:
            if VIOLATION_WELL_REFERENCE_INVALID not in categories:
                categories.append(VIOLATION_WELL_REFERENCE_INVALID)
            messages.append(str(exc))
    if categories:
        raise RetestPlanInvalid(categories, messages)
    return avoided


def _eligible_candidates(
    snapshot: dict, rows: int, cols: int, avoided: set[tuple[int, int]]
) -> list[_Candidate]:
    """The selectable pool: planned samples still on their well with a valid
    reading at a well that is neither blocked nor avoided.
    """
    target: dict[str, str] = snapshot["target"]
    scan: dict[str, str] = snapshot["scan"]
    readings: dict[str, Optional[float]] = snapshot["readings"]
    group_of = {s["id"]: s["group"] for s in snapshot["samples"]}

    # Reverse view of the frozen scan: sample id -> every well it is scanned
    # at (a validated snapshot has exactly one, but sets detect a duplicate
    # occurrence defensively).
    scanned_wells: dict[str, set[str]] = {}
    for raw_well, sid in scan.items():
        try:
            pos = parse_well(raw_well, rows, cols)
        except ValueError:
            continue
        scanned_wells.setdefault(sid, set()).add(well_name(*pos))

    blocked: set[tuple[int, int]] = set()
    for raw in snapshot.get("blocked_wells", []):
        # Snapshots are frozen from validated requests; parse defensively.
        blocked.add(parse_well(raw, rows, cols))

    candidates: list[_Candidate] = []
    for sid, raw_well in target.items():
        pos = parse_well(raw_well, rows, cols)
        well = well_name(*pos)
        group = group_of.get(sid)
        if group is None:
            continue
        if pos in blocked or pos in avoided:
            continue
        # The frozen scan must place this sample at exactly its planned well:
        # both an empty/replaced planned well and an extra scanned occurrence
        # make the sample non-selectable.
        if scan.get(well) != sid or scanned_wells.get(sid) != {well}:
            continue
        # Only wells with a valid (present, non-null, finite) reading qualify.
        value = readings.get(well)
        if value is None or not isinstance(value, (int, float)) or not math.isfinite(value):
            continue
        candidates.append(_Candidate(len(candidates), sid, group, pos[0], pos[1]))

    candidates.sort(key=lambda c: (c.row, c.col, c.sample_id))
    # Re-index after sorting so variable order follows the row-major layout.
    return [
        _Candidate(i, c.sample_id, c.group, c.row, c.col)
        for i, c in enumerate(candidates)
    ]


# ------------------------------------------------------------------- CP-SAT


class _RetestModel:
    """One CP-SAT model for a lexicographic stage of the sampling plan."""

    def __init__(self, rows: int, cols: int, candidates: list[_Candidate],
                 quotas: dict[str, int]):
        self.rows = rows
        self.cols = cols
        self.candidates = candidates
        self.model = cp_model.CpModel()

        self.x = [self.model.new_bool_var(f"x_{i}") for i in range(len(candidates))]

        # Exact per-group quotas.
        by_group: dict[str, list] = {}
        for i, c in enumerate(candidates):
            by_group.setdefault(c.group, []).append(self.x[i])
        for group, quota in quotas.items():
            self.model.add(sum(by_group.get(group, [])) == quota)

        # Covered rows / columns.
        self.rr = [self.model.new_bool_var(f"rr_{r}") for r in range(rows)]
        self.cc = [self.model.new_bool_var(f"cc_{c}") for c in range(cols)]
        row_vars: dict[int, list] = {r: [] for r in range(rows)}
        col_vars: dict[int, list] = {c: [] for c in range(cols)}
        for i, cand in enumerate(candidates):
            row_vars[cand.row].append(self.x[i])
            col_vars[cand.col].append(self.x[i])
        for r in range(rows):
            xs = row_vars[r]
            if xs:
                self.model.add(self.rr[r] <= sum(xs))
                for var in xs:
                    self.model.add(self.rr[r] >= var)
            else:
                self.model.add(self.rr[r] == 0)
        for c in range(cols):
            xs = col_vars[c]
            if xs:
                self.model.add(self.cc[c] <= sum(xs))
                for var in xs:
                    self.model.add(self.cc[c] >= var)
            else:
                self.model.add(self.cc[c] == 0)
        self.coverage = self.model.new_int_var(0, rows + cols, "coverage")
        self.model.add(self.coverage == sum(self.rr) + sum(self.cc))

        # Orthogonal same-batch adjacency among candidate wells. Every frozen
        # sample belongs to the certified batch, so an edge is counted when
        # both endpoint wells are selected.
        pos_to_i = {(c.row, c.col): i for i, c in enumerate(candidates)}
        self.edges: list[tuple[int, int]] = []
        self.y: list = []
        for i, cand in enumerate(candidates):
            for nxt in ((cand.row + 1, cand.col), (cand.row, cand.col + 1)):
                j = pos_to_i.get(nxt)
                if j is None:
                    continue
                self.edges.append((i, j))
                y = self.model.new_bool_var(f"y_{len(self.edges)}")
                self.model.add(y <= self.x[i])
                self.model.add(y <= self.x[j])
                self.model.add(y >= self.x[i] + self.x[j] - 1)
                self.y.append(y)
        self.adjacency = self.model.new_int_var(0, max(len(self.edges), 0), "adjacency")
        self.model.add(self.adjacency == sum(self.y))

    # ----------------------------------------------------------- objectives
    def maximize_coverage(self) -> None:
        self.model.maximize(self.coverage)

    def pin_coverage(self, value: int) -> None:
        self.model.add(self.coverage == value)

    def minimize_adjacency(self) -> None:
        self.model.minimize(self.adjacency)

    def pin_adjacency(self, value: int) -> None:
        self.model.add(self.adjacency == value)

    # --------------------------------------------------------------- hints
    def add_hint(self, chosen: set[int]) -> None:
        """Provide values for every variable from a feasible selection."""
        for i in range(len(self.candidates)):
            self.model.add_hint(self.x[i], 1 if i in chosen else 0)
        covered_rows = {self.candidates[i].row for i in chosen}
        covered_cols = {self.candidates[i].col for i in chosen}
        for r in range(self.rows):
            self.model.add_hint(self.rr[r], 1 if r in covered_rows else 0)
        for c in range(self.cols):
            self.model.add_hint(self.cc[c], 1 if c in covered_cols else 0)
        for (i, j), y in zip(self.edges, self.y):
            self.model.add_hint(y, 1 if i in chosen and j in chosen else 0)
        self.model.add_hint(self.coverage, len(covered_rows) + len(covered_cols))
        self.model.add_hint(
            self.adjacency,
            sum(1 for i, j in self.edges if i in chosen and j in chosen),
        )

    def selected_indices(self, solver) -> list[int]:
        return [i for i in range(len(self.candidates)) if solver.value(self.x[i]) == 1]


def _greedy_feasible(candidates: list[_Candidate], quotas: dict[str, int],
                     seed: int) -> set[int]:
    """Deterministic feasible warm start, greedily spreading onto fresh
    rows/columns while filling the exact per-group quotas round-robin.
    """
    rng = random.Random(seed)
    by_group: dict[str, list[_Candidate]] = {}
    for c in candidates:
        by_group.setdefault(c.group, []).append(c)
    for group in by_group:
        by_group[group].sort(key=lambda c: rng.random())

    remaining = dict(quotas)
    chosen: set[int] = set()
    covered_rows: set[int] = set()
    covered_cols: set[int] = set()
    while any(n > 0 for n in remaining.values()):
        progressed = False
        for group in sorted(remaining):
            if remaining[group] <= 0:
                continue
            avail = [c for c in by_group[group] if c.index not in chosen]
            # Quota feasibility was checked before solving; guard anyway.
            assert avail, f"no eligible sample left for group {group!r}"

            def score(c: _Candidate) -> tuple[int, float]:
                return (
                    (1 if c.row not in covered_rows else 0)
                    + (1 if c.col not in covered_cols else 0),
                    rng.random(),
                )

            pick = max(avail, key=score)
            chosen.add(pick.index)
            covered_rows.add(pick.row)
            covered_cols.add(pick.col)
            remaining[group] -= 1
            progressed = True
        if not progressed:  # pragma: no cover - defensive
            break
    return chosen


def _solve_stage(stage: str, model: _RetestModel, seed: int, *,
                 first_solution: bool = False):
    det_limit = float(os.getenv("CP_DETERMINISTIC_TIME_SECONDS", "4"))
    wall_limit = float(os.getenv("CP_WALL_TIME_SECONDS", "12"))
    fast_det = float(os.getenv("CP_FAST_DETERMINISTIC_TIME_SECONDS", "1"))
    fast_wall = float(os.getenv("CP_FAST_WALL_TIME_SECONDS", "8"))
    if first_solution:
        solver = _make_solver(seed, fast_det, fast_wall,
                              stop_at_first=True, fast_presolve=True)
    else:
        solver = _make_solver(seed, det_limit, wall_limit)
    t0 = time.perf_counter()
    status = solver.solve(model.model)
    elapsed = time.perf_counter() - t0
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return solver, (
            stage,
            int(solver.objective_value) if not first_solution else -1,
            solver.status_name(status),
            status == cp_model.OPTIMAL,
            elapsed,
        )
    if status == cp_model.INFEASIBLE:
        raise RetestPlanInvalid(
            [VIOLATION_RETEST_NO_SOLUTION],
            [f"CP-SAT proved the sampling constraints infeasible at stage {stage}"],
        )
    raise SolverTimeout(stage, elapsed)


def build_retest_plan(
    snapshot: dict,
    group_counts: dict[str, int],
    avoid_wells: list[str],
    seed: int,
) -> RetestSelection:
    """Draw the deterministic optimal selection from a frozen snapshot.

    Raises ``RetestPlanInvalid`` (no plan saved) when an avoid well is
    malformed, a requested group is unknown to the revision or a group's
    eligible pool cannot meet its quota.
    """
    rows = snapshot["plate"]["rows"]
    cols = snapshot["plate"]["columns"]
    avoided = _parse_avoid(avoid_wells, rows, cols)
    candidates = _eligible_candidates(snapshot, rows, cols, avoided)

    snapshot_groups = {c.group for c in candidates}
    # Groups present in the snapshot but entirely ineligible still count as
    # known groups (their pool is simply empty).
    snapshot_groups.update(s["group"] for s in snapshot["samples"])

    categories: list[str] = []
    messages: list[str] = []
    unknown = [g for g in group_counts if g not in snapshot_groups]
    if unknown:
        categories.append(VIOLATION_RETEST_GROUP_UNKNOWN)
        for g in sorted(unknown):
            messages.append(
                f"group '{g}' does not exist in the certificate revision's "
                "sample grouping"
            )

    eligible_by_group: dict[str, int] = {}
    for c in candidates:
        eligible_by_group[c.group] = eligible_by_group.get(c.group, 0) + 1
    for group, quota in group_counts.items():
        if group in unknown:
            continue
        pool = eligible_by_group.get(group, 0)
        if quota > pool:
            if VIOLATION_RETEST_INSUFFICIENT_SAMPLES not in categories:
                categories.append(VIOLATION_RETEST_INSUFFICIENT_SAMPLES)
            messages.append(
                f"group '{group}' needs {quota} sample(s) but only {pool} "
                "eligible sample(s) have a valid reading at an unblocked, "
                "non-avoided well present in the frozen scan"
            )
    if categories:
        raise RetestPlanInvalid(categories, messages)

    quotas = dict(group_counts)
    # Only the groups named in the request belong to the plan: every quota is
    # met exactly, and samples of every other group are simply not drawn
    # (their booleans would otherwise be free to inflate the coverage).
    requested = set(quotas)
    model_candidates = [
        _Candidate(i, c.sample_id, c.group, c.row, c.col)
        for i, c in enumerate(c for c in candidates if c.group in requested)
    ]
    seed31 = int(seed) % (2**31)
    stages: list[tuple[str, int, str, bool, float]] = []

    # Stage 1: maximise covered rows + columns.
    m1 = _RetestModel(rows, cols, model_candidates, quotas)
    m1.maximize_coverage()
    m1.add_hint(_greedy_feasible(model_candidates, quotas, seed31))
    solver1, info1 = _solve_stage("1_coverage", m1, seed31)
    best_coverage = int(solver1.value(m1.coverage))
    stages.append(info1)

    # Stage 2: minimise same-batch adjacent pairs, pinning coverage.
    m2 = _RetestModel(rows, cols, model_candidates, quotas)
    m2.pin_coverage(best_coverage)
    m2.minimize_adjacency()
    m2.add_hint(set(m1.selected_indices(solver1)))
    solver2, info2 = _solve_stage("2_adjacency", m2, seed31)
    best_adjacency = int(solver2.objective_value)
    stages.append(info2)

    # Stage 3: deterministic seed tie-break — instantiate the hinted
    # (coverage*, adjacency*) optimum as the first solution.
    m3 = _RetestModel(rows, cols, model_candidates, quotas)
    m3.pin_coverage(best_coverage)
    m3.pin_adjacency(best_adjacency)
    m3.add_hint(set(m2.selected_indices(solver2)))
    solver3, info3 = _solve_stage("3_tiebreak", m3, seed31, first_solution=True)
    stages.append(info3)

    chosen = [model_candidates[i] for i in sorted(m3.selected_indices(solver3))]

    # Metrics are recounted independently in Python (authoritative).
    pos = {(c.row, c.col) for c in chosen}
    pairs: set[tuple[str, str]] = set()
    for c in chosen:
        for nxt in ((c.row + 1, c.col), (c.row, c.col + 1)):
            if nxt in pos:
                pairs.add(tuple(sorted((c.well, well_name(*nxt)))))

    covered_rows = sorted({c.row for c in chosen})
    covered_cols = sorted({c.col for c in chosen})
    return RetestSelection(
        rows=rows,
        columns=cols,
        selected=chosen,
        adjacent_pairs=sorted(pairs),
        eligible_by_group={g: eligible_by_group.get(g, 0) for g in quotas},
        eligible_total=len(candidates),
        coverage_rows=len(covered_rows),
        coverage_cols=len(covered_cols),
        coverage_total=len(covered_rows) + len(covered_cols),
        stages=stages,
    )


# ---------------------------------------------------------------- endpoint


def _plan_id(batch_id: str, revision: int, request_key: str) -> str:
    return "plan-" + _sha256(
        f"retest-plan\0{batch_id}\0{revision}\0{request_key}".encode("utf-8")
    )[:24]


def _plan_body(*, cert: dict, revision: int, digest: str, seed: int,
               group_counts: dict[str, int], avoid_wells: list[str],
               selection: RetestSelection, plan_id: str) -> dict:
    covered_rows = sorted({c.row for c in selection.selected})
    covered_cols = sorted({c.col for c in selection.selected})
    selected_by_group: dict[str, int] = {g: 0 for g in group_counts}
    for c in selection.selected:
        selected_by_group[c.group] += 1
    return {
        "plan_id": plan_id,
        "certificate_id": cert["certificate_id"],
        "batch_id": cert["batch_id"],
        "status": STATUS_ACTIVE,
        "revision": revision,
        "digest": digest,
        "seed": seed,
        "samples": [
            {"sample_id": c.sample_id, "well": c.well, "group": c.group}
            for c in selection.selected
        ],
        "wells": [c.well for c in selection.selected],
        "group_counts": selected_by_group,
        "requested_group_counts": dict(group_counts),
        "coverage": {
            "covered_rows": [well_name(r, 0)[0] for r in covered_rows],
            "covered_columns": [c + 1 for c in covered_cols],
            "rows_covered": selection.coverage_rows,
            "columns_covered": selection.coverage_cols,
            "total_rows": selection.rows,
            "total_columns": selection.columns,
            "total_covered": selection.coverage_total,
        },
        "same_batch_adjacent_pairs_count": len(selection.adjacent_pairs),
        "same_batch_adjacent_pairs": [list(p) for p in selection.adjacent_pairs],
        "avoided_wells": sorted(
            {well_name(*parse_well(w, selection.rows, selection.columns))
             for w in avoid_wells}
        ),
        "eligible_samples": selection.eligible_total,
        "eligible_by_group": selection.eligible_by_group,
        "solver": {
            "stages": [
                {
                    "stage": st[0],
                    "objective_value": st[1],
                    "status": st[2],
                    "proven_optimal": st[3],
                    "wall_time_seconds": round(st[4], 4),
                }
                for st in selection.stages
            ]
        },
    }


def create_retest_plan(req: RetestPlanRequest) -> tuple[int, dict]:
    """Draw and persist a re-test plan against the certificate's current rev."""
    store = get_store()
    content_hash = _content_hash(_ENDPOINT_RETEST, req)
    with store.transaction() as conn:
        # State is checked BEFORE the idempotency lookup on purpose: after a
        # re-verification advances the revision, a request (even a replay of
        # an earlier recorded one) naming the old revision must be refused
        # with 409 rather than returning a plan drawn from stale evidence.
        cert = _get_certificate(conn, req.batch_id)
        if cert is None:
            return 404, {
                "detail": "no certificate exists for the batch",
                "error": CERTIFICATE_NOT_FOUND,
                "batch_id": req.batch_id,
            }
        if cert["status"] == STATUS_REVOKED:
            return 409, {
                "detail": "the certificate is REVOKED; no re-test plan can be "
                "drawn from it and a revoked certificate can never be restored",
                "error": CERTIFICATE_REVOKED,
                "certificate_id": cert["certificate_id"],
                "batch_id": req.batch_id,
                "status": STATUS_REVOKED,
                "revoked_reason": cert["revoked_reason"],
            }
        if req.expected_revision != cert["revision"]:
            return 409, {
                "detail": "expected_revision does not match the certificate's "
                "current revision; plans are drawn only from the current "
                "revision and older revisions can never be re-opened",
                "error": CERTIFICATE_REVISION_MISMATCH,
                "certificate_id": cert["certificate_id"],
                "batch_id": req.batch_id,
                "current_revision": cert["revision"],
                "expected_revision": req.expected_revision,
            }

        replay = _idem_replay(conn, req.request_key, content_hash)
        if replay is not None:
            return replay

        # Freeze this transaction onto the exact current revision snapshot;
        # its digest is returned and pinned on the stored plan row.
        rev_row = conn.execute(
            "SELECT digest, snapshot FROM certificate_revisions "
            "WHERE batch_id = ? AND revision = ?",
            (req.batch_id, cert["revision"]),
        ).fetchone()
        if rev_row is None:  # pragma: no cover - invariant guard
            raise RuntimeError(
                f"revision {cert['revision']} snapshot missing for "
                f"batch {req.batch_id!r}"
            )
        snapshot = json.loads(rev_row["snapshot"])

        try:
            selection = build_retest_plan(
                snapshot, req.group_counts, req.avoid_wells, req.seed
            )
        except RetestPlanInvalid as exc:
            body = retest_invalid_body(exc)
            _idem_record(conn, req.request_key, _ENDPOINT_RETEST,
                         content_hash, 422, body)
            return 422, body

        plan_id = _plan_id(req.batch_id, cert["revision"], req.request_key)
        body = _plan_body(
            cert=cert,
            revision=cert["revision"],
            digest=rev_row["digest"],
            seed=req.seed,
            group_counts=req.group_counts,
            avoid_wells=req.avoid_wells,
            selection=selection,
            plan_id=plan_id,
        )
        now = _utcnow()
        conn.execute(
            "INSERT INTO retest_plans (plan_id, batch_id, certificate_id, "
            "revision, digest, seed, plan_body, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (plan_id, req.batch_id, cert["certificate_id"], cert["revision"],
             rev_row["digest"], req.seed,
             json.dumps(body, ensure_ascii=False), now),
        )
        _idem_record(conn, req.request_key, _ENDPOINT_RETEST, content_hash,
                     201, body)
        return 201, body
