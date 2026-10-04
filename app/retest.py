"""Certificate-anchored retest sampling plans.

``POST /api/v1/certificates/retest-plan`` draws a deterministic retest
sampling plan from the *current* revision of an ACTIVE batch certificate.
The caller submits ``batch_id``, ``expected_revision``, a per-group quota of
samples to draw, the wells to avoid and a ``seed``; the service restores the
layout, sample grouping, scan and readings frozen in that revision's
snapshot and selects only samples that have a valid (non-null) reading and
do not sit on an excluded well.

Selection is lexicographic:

1. every group quota is satisfied exactly (a hard constraint — a group whose
   eligible pool is smaller than its quota makes the request unsatisfiable);
2. the number of covered rows plus covered columns is maximized;
3. the number of orthogonally adjacent selected pairs is minimized (every
   selected sample belongs to the certificate's single batch, so any
   adjacent selected pair is a same-batch pair);
4. remaining ties are broken deterministically under the submitted seed —
   the stage-2 optimum is fed back as a complete hint and the first solution
   is taken, so identical input and seed always yield the identical plan.

Reading the certificate state, freezing the plan and saving it together
with the idempotency record happens in a single SQLite transaction. A
revoked certificate, a stale ``expected_revision``, insufficient eligible
samples or an unsolvable constraint saves nothing. Replaying the same
``request_key`` with identical content returns the recorded response; the
same key with different content is rejected with 409.
"""
from __future__ import annotations

import json

from ortools.sat.python import cp_model

from .certificates import (
    STATUS_ACTIVE,
    STATUS_REVOKED,
    _canonical_json,
    _content_hash,
    _get_certificate,
    _idem_record,
    _idem_replay,
    _sha256,
    _utcnow,
    get_store,
)
from .planner import _solve_stage
from .plates import parse_well, well_name
from .schemas import (
    CERTIFICATE_NOT_FOUND,
    CERTIFICATE_REVISION_MISMATCH,
    CERTIFICATE_REVOKED,
    VIOLATION_RETEST_GROUP_QUOTA_EXCEEDED,
    VIOLATION_WELL_REFERENCE_INVALID,
    CertificateRetestPlanRequest,
)

# Idempotency endpoint tag (part of the content hash, so a key cannot be
# reused across endpoints either).
_ENDPOINT_RETEST = "POST /api/v1/certificates/retest-plan"


# ------------------------------------------------------------- eligibility


def _position_map(mapping: dict, rows: int, cols: int) -> dict:
    """Well-name-keyed snapshot dict → position-keyed dict (best effort).

    Snapshot keys are stored exactly as submitted; names that fail to parse
    are skipped rather than hiding the entries that can be read.
    """
    out = {}
    for raw, value in mapping.items():
        try:
            out[parse_well(raw, rows, cols)] = value
        except ValueError:
            continue
    return out


def _eligible_samples(snapshot: dict, rows: int, cols: int,
                      excluded: set[tuple[int, int]]) -> list[dict]:
    """Samples selectable for retest from the frozen revision snapshot.

    A sample is eligible only when it appears in the frozen layout *and* the
    frozen grouping, its well is confirmed by the frozen scan, its reading is
    valid (present and non-null) and its well is not excluded.
    """
    group_of: dict[str, str] = {}
    for entry in snapshot["samples"]:
        group_of.setdefault(entry["id"], entry["group"])
    scan_at = _position_map(snapshot["scan"], rows, cols)
    reading_at = _position_map(snapshot["readings"], rows, cols)

    eligible = []
    for sid, raw_well in snapshot["target"].items():
        group = group_of.get(sid)
        if group is None:
            continue
        try:
            pos = parse_well(raw_well, rows, cols)
        except ValueError:
            continue
        if pos in excluded:
            continue
        if scan_at.get(pos) != sid:
            continue
        if reading_at.get(pos) is None:
            continue
        eligible.append(
            {"sample_id": sid, "group": group, "well": well_name(*pos), "pos": pos}
        )
    eligible.sort(key=lambda e: e["pos"])
    return eligible


# ---------------------------------------------------------- selection model


class _SelectionModel:
    """CP-SAT model for one lexicographic retest-selection stage."""

    def __init__(self, eligible: list[dict], quotas: dict[str, int]):
        self.model = cp_model.CpModel()
        self.eligible = sorted(eligible, key=lambda e: e["sample_id"])
        # sel[i]: eligible sample i is drawn into the retest plan.
        self.x = {
            e["sample_id"]: self.model.new_bool_var(f"sel_{i}")
            for i, e in enumerate(self.eligible)
        }
        # Hard constraint: every group quota is met exactly.
        for group, quota in quotas.items():
            members = [
                self.x[e["sample_id"]] for e in self.eligible if e["group"] == group
            ]
            self.model.add(sum(members) == quota)

        # Row/column coverage indicators (1 iff some selected sample sits in
        # the row/column).
        self.row_vars: dict[int, object] = {}
        for r in sorted({e["pos"][0] for e in self.eligible}):
            members = [
                self.x[e["sample_id"]] for e in self.eligible if e["pos"][0] == r
            ]
            v = self.model.new_bool_var(f"row_{r}")
            for m in members:
                self.model.add(v >= m)
            self.model.add(v <= sum(members))
            self.row_vars[r] = v
        self.col_vars: dict[int, object] = {}
        for c in sorted({e["pos"][1] for e in self.eligible}):
            members = [
                self.x[e["sample_id"]] for e in self.eligible if e["pos"][1] == c
            ]
            v = self.model.new_bool_var(f"col_{c}")
            for m in members:
                self.model.add(v >= m)
            self.model.add(v <= sum(members))
            self.col_vars[c] = v

        # y[e] >= x[u] + x[v] - 1 for every orthogonal pair of eligible wells.
        by_pos = {e["pos"]: e["sample_id"] for e in self.eligible}
        self.edges: list[tuple[tuple[int, int], tuple[int, int]]] = []
        self.y: dict[tuple, object] = {}
        for pos in sorted(by_pos):
            r, c = pos
            for other in ((r + 1, c), (r, c + 1)):
                if other in by_pos:
                    y = self.model.new_bool_var(
                        f"adj_{r}_{c}_{other[0]}_{other[1]}"
                    )
                    self.model.add(
                        y >= self.x[by_pos[pos]] + self.x[by_pos[other]] - 1
                    )
                    self.edges.append((pos, other))
                    self.y[(pos, other)] = y

    # ------------------------------------------------------------- objectives
    def _coverage_expr(self):
        return sum(self.row_vars.values()) + sum(self.col_vars.values())

    def _adjacency_expr(self):
        return sum(self.y.values()) if self.y else 0

    def add_coverage_objective(self):
        self.model.maximize(self._coverage_expr())

    def pin_coverage(self, value: int) -> None:
        self.model.add(self._coverage_expr() >= value)

    def add_adjacency_objective(self):
        self.model.minimize(self._adjacency_expr())

    def pin_adjacency(self, value: int) -> None:
        if self.y:
            self.model.add(self._adjacency_expr() <= value)

    # ----------------------------------------------------------------- hints
    def selection_from_solver(self, solver) -> list[str]:
        return [
            e["sample_id"]
            for e in self.eligible
            if solver.value(self.x[e["sample_id"]]) == 1
        ]

    def add_hint_from_selection(self, selected: list[str]) -> None:
        """Provide values for *every* variable so the hint is a full solution."""
        sel = set(selected)
        by_pos = {e["pos"]: e["sample_id"] for e in self.eligible}
        hints: list[tuple[object, int]] = []
        for e in self.eligible:
            hints.append((self.x[e["sample_id"]], 1 if e["sample_id"] in sel else 0))
        covered_rows = {e["pos"][0] for e in self.eligible if e["sample_id"] in sel}
        covered_cols = {e["pos"][1] for e in self.eligible if e["sample_id"] in sel}
        for r, v in self.row_vars.items():
            hints.append((v, 1 if r in covered_rows else 0))
        for c, v in self.col_vars.items():
            hints.append((v, 1 if c in covered_cols else 0))
        for (u, w), y in self.y.items():
            hints.append((y, 1 if by_pos[u] in sel and by_pos[w] in sel else 0))
        for var, val in hints:
            self.model.add_hint(var, val)


def _select(eligible: list[dict], quotas: dict[str, int], seed: int):
    """Three-stage lexicographic selection; returns (selected ids, stages)."""
    selectable = [e for e in eligible if e["group"] in quotas]
    seed31 = int(seed) % (2**31)
    stages = []

    # Stage 1: maximize covered rows + columns under the exact group quotas.
    m1 = _SelectionModel(selectable, quotas)
    m1.add_coverage_objective()
    solver1, info1 = _solve_stage("1_coverage", m1, seed31)
    best_coverage = int(solver1.objective_value)
    stages.append(info1)

    # Stage 2: minimize same-batch orthogonal adjacency, pinning coverage.
    m2 = _SelectionModel(selectable, quotas)
    m2.pin_coverage(best_coverage)
    m2.add_adjacency_objective()
    m2.add_hint_from_selection(m1.selection_from_solver(solver1))
    solver2, info2 = _solve_stage("2_adjacency", m2, seed31)
    best_adjacency = int(solver2.objective_value)
    stages.append(info2)

    # Stage 3: deterministic tie-break — the (coverage*, adjacency*)-optimal
    # stage-2 selection is fed back as a complete hint and returned as the
    # first solution.
    m3 = _SelectionModel(selectable, quotas)
    m3.pin_coverage(best_coverage)
    m3.pin_adjacency(best_adjacency)
    m3.add_hint_from_selection(m2.selection_from_solver(solver2))
    solver3, info3 = _solve_stage("3_tiebreak", m3, seed31, first_solution=True)
    stages.append(info3)

    return m3.selection_from_solver(solver3), stages


# ----------------------------------------------------------------- metrics


def _recount(selected_ids: list[str], eligible: list[dict],
             quotas: dict[str, int]):
    """Authoritative pure-Python recount of the plan metrics."""
    by_id = {e["sample_id"]: e for e in eligible}
    selected = sorted((by_id[sid] for sid in selected_ids), key=lambda e: e["pos"])

    rows_covered = sorted({e["pos"][0] for e in selected})
    cols_covered = sorted({e["pos"][1] for e in selected})

    positions = {e["pos"] for e in selected}
    pairs: list[list[str]] = []
    for e in selected:
        r, c = e["pos"]
        for other in ((r + 1, c), (r, c + 1)):
            if other in positions:
                pairs.append([e["well"], well_name(*other)])
    pairs.sort()

    eligible_counts: dict[str, int] = {}
    for e in eligible:
        eligible_counts[e["group"]] = eligible_counts.get(e["group"], 0) + 1
    groups = [
        {
            "group": group,
            "requested": quotas[group],
            "selected": sum(1 for e in selected if e["group"] == group),
            "eligible": eligible_counts.get(group, 0),
        }
        for group in sorted(quotas)
    ]
    return selected, groups, rows_covered, cols_covered, pairs


# ------------------------------------------------------------------ endpoint


def _plan_id(batch_id: str, request_key: str) -> str:
    return "retest-" + _sha256(
        f"retest-plan\0{batch_id}\0{request_key}".encode("utf-8")
    )[:24]


def create_retest_plan(req: CertificateRetestPlanRequest) -> tuple[int, dict]:
    """Draw, freeze and persist a retest sampling plan (single transaction)."""
    store = get_store()
    content_hash = _content_hash(_ENDPOINT_RETEST, req)
    with store.transaction() as conn:
        replay = _idem_replay(conn, req.request_key, content_hash)
        if replay is not None:
            return replay

        cert = _get_certificate(conn, req.batch_id)
        if cert is None:
            # State-dependent rejections are not recorded under the key.
            return 404, {
                "detail": "no certificate exists for the batch",
                "error": CERTIFICATE_NOT_FOUND,
                "batch_id": req.batch_id,
            }
        if cert["status"] == STATUS_REVOKED:
            return 409, {
                "detail": "the certificate is REVOKED and can never be restored",
                "error": CERTIFICATE_REVOKED,
                "certificate_id": cert["certificate_id"],
                "batch_id": req.batch_id,
                "status": STATUS_REVOKED,
                "revoked_reason": cert["revoked_reason"],
            }
        if req.expected_revision != cert["revision"]:
            return 409, {
                "detail": "expected_revision does not match the certificate's "
                "current revision; plans can only be drawn from the current "
                "revision",
                "error": CERTIFICATE_REVISION_MISMATCH,
                "certificate_id": cert["certificate_id"],
                "batch_id": req.batch_id,
                "current_revision": cert["revision"],
                "expected_revision": req.expected_revision,
            }

        snapshot = json.loads(cert["snapshot"])
        rows = snapshot["plate"]["rows"]
        cols = snapshot["plate"]["columns"]

        # Wells to avoid: every name must parse inside the plate.
        excluded: set[tuple[int, int]] = set()
        well_errors: list[str] = []
        for raw in req.excluded_wells:
            try:
                excluded.add(parse_well(raw, rows, cols))
            except ValueError as exc:
                well_errors.append(str(exc))
        if well_errors:
            body = {
                "detail": "The retest sampling request is invalid; "
                "no plan was saved.",
                "infeasibility_categories": [VIOLATION_WELL_REFERENCE_INVALID],
                "messages": well_errors,
            }
            _idem_record(conn, req.request_key, _ENDPOINT_RETEST, content_hash, 422, body)
            return 422, body

        eligible = _eligible_samples(snapshot, rows, cols, excluded)
        eligible_counts: dict[str, int] = {}
        for e in eligible:
            eligible_counts[e["group"]] = eligible_counts.get(e["group"], 0) + 1
        quota_problems = [
            f"group '{group}': quota {quota} exceeds the "
            f"{eligible_counts.get(group, 0)} eligible sample(s) with a valid "
            "reading"
            for group, quota in sorted(req.group_counts.items())
            if quota > eligible_counts.get(group, 0)
        ]
        if quota_problems:
            body = {
                "detail": "The retest sampling quotas cannot be satisfied by "
                "the eligible samples of the certificate's current revision; "
                "no plan was saved.",
                "infeasibility_categories": [VIOLATION_RETEST_GROUP_QUOTA_EXCEEDED],
                "messages": quota_problems,
            }
            _idem_record(conn, req.request_key, _ENDPOINT_RETEST, content_hash, 422, body)
            return 422, body

        quotas = dict(sorted(req.group_counts.items()))
        selected_ids, stages = _select(eligible, quotas, req.seed)
        selected, groups, rows_cov, cols_cov, pairs = _recount(
            selected_ids, eligible, quotas
        )

        # Freeze the plan anchored to the certificate's current revision and
        # digest; the plan row and the idempotency record commit together.
        plan = {
            "plan_id": _plan_id(req.batch_id, req.request_key),
            "batch_id": req.batch_id,
            "certificate_id": cert["certificate_id"],
            "status": STATUS_ACTIVE,
            "revision": cert["revision"],
            "digest": cert["digest"],
            "seed": req.seed,
            "group_counts": quotas,
            "excluded_wells": [well_name(*p) for p in sorted(excluded)],
            "selected": [
                {"sample_id": e["sample_id"], "well": e["well"], "group": e["group"]}
                for e in selected
            ],
            "groups": groups,
            "coverage": {
                "covered_rows": len(rows_cov),
                "covered_columns": len(cols_cov),
                "covered_total": len(rows_cov) + len(cols_cov),
                "rows": [chr(ord("A") + r) for r in rows_cov],
                "columns": [c + 1 for c in cols_cov],
            },
            "adjacency": {
                "same_batch_adjacent_pairs_count": len(pairs),
                "same_batch_adjacent_pairs": pairs,
            },
        }
        conn.execute(
            "INSERT INTO retest_plans (plan_id, batch_id, certificate_id, "
            "revision, digest, request_key, seed, plan, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (plan["plan_id"], req.batch_id, cert["certificate_id"],
             cert["revision"], cert["digest"], req.request_key, req.seed,
             _canonical_json(plan).decode("utf-8"), _utcnow()),
        )

        optimization_stages = [s for s in stages if s.stage != "3_tiebreak"]
        body = dict(plan)
        body["solver"] = {
            "status": (
                "OPTIMAL"
                if all(s.proven_optimal for s in optimization_stages)
                else "FEASIBLE"
            ),
            "stages": [
                {
                    "stage": s.stage,
                    "objective_value": (
                        s.objective_value if s.objective_value >= 0 else None
                    ),
                    "status": s.status,
                    "proven_optimal": s.proven_optimal,
                }
                for s in stages
            ],
        }
        _idem_record(conn, req.request_key, _ENDPOINT_RETEST, content_hash, 201, body)
        return 201, body
