"""Lexicographic microplate layout optimizer built on OR-Tools CP-SAT.

Optimization order (字典序):

1. ``D`` — over every experiment group, the larger of that group's row-count
   spread (max-min across rows) and column-count spread (max-min across
   columns). Minimizing the global maximum D balances groups across both axes.
2. ``A`` — number of orthogonally adjacent well pairs occupied by samples of
   the same batch.

A third, deterministic tie-break step fixes the remaining degrees of freedom
so identical inputs always yield the identical canonical layout: the optimum
of stage 2 is fed back as a complete solution hint and the search stops at
that first (hint-derived) solution. The seed governs every stage's search.
"""
from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from .plates import MAX_CELLS, PLATE_PRESETS, parse_well, well_name
from .schemas import (
    VIOLATION_CAPACITY_EXCEEDED,
    VIOLATION_FIXED_CONFLICT,
    VIOLATION_FIXED_ON_BLOCKED,
    VIOLATION_GROUP_BATCH_INVALID,
    VIOLATION_PLATE_DIMENSION_INVALID,
    VIOLATION_SAMPLE_IDS_DUPLICATED,
    VIOLATION_SOLVER_INFEASIBLE,
    VIOLATION_WELL_REFERENCE_INVALID,
    ArrangeRequest,
)


class InfeasibleArrangement(Exception):
    """Raised when the request admits no valid layout.

    ``categories`` lists the constraint classes that make it infeasible.
    """

    def __init__(self, categories: list[str], messages: list[str]):
        self.categories = categories
        self.messages = messages
        super().__init__("; ".join(messages) if messages else "infeasible arrangement")


class SolverTimeout(Exception):
    """Raised when the solver cannot find any solution in the time budget."""

    def __init__(self, stage: str, elapsed: float):
        self.stage = stage
        self.elapsed = elapsed
        super().__init__(f"solver timed out during stage {stage} after {elapsed:.1f}s")


@dataclass
class StageInfo:
    stage: str
    objective_value: int
    status: str
    proven_optimal: bool
    wall_time_seconds: float


@dataclass
class PlannedLayout:
    rows: int
    columns: int
    available_wells: list[str]
    placement: dict[str, str]
    same_batch_adjacent_pairs: list[tuple[str, str]]
    groups: list[str]
    batches: list[str]
    group_row_counts: dict[str, dict[int, int]]
    group_col_counts: dict[str, dict[int, int]]
    stages: list[StageInfo] = field(default_factory=list)


def _resolve_dimensions(plate) -> tuple[int, int]:
    if plate.type == "custom":
        if not plate.rows or not plate.columns:
            raise InfeasibleArrangement(
                [VIOLATION_PLATE_DIMENSION_INVALID],
                ["custom plate requires both 'rows' and 'columns'"],
            )
        rows, cols = plate.rows, plate.columns
        if rows * cols > MAX_CELLS:
            raise InfeasibleArrangement(
                [VIOLATION_PLATE_DIMENSION_INVALID],
                [f"custom plate {rows}x{cols} exceeds {MAX_CELLS} wells"],
            )
        return rows, cols
    preset_rows, preset_cols = PLATE_PRESETS[plate.type]
    if plate.rows is not None and plate.rows != preset_rows:
        raise InfeasibleArrangement(
            [VIOLATION_PLATE_DIMENSION_INVALID],
            [f"plate type '{plate.type}' has {preset_rows} rows, got {plate.rows}"],
        )
    if plate.columns is not None and plate.columns != preset_cols:
        raise InfeasibleArrangement(
            [VIOLATION_PLATE_DIMENSION_INVALID],
            [f"plate type '{plate.type}' has {preset_cols} columns, got {plate.columns}"],
        )
    return preset_rows, preset_cols


def _preflight(req: ArrangeRequest, rows: int, cols: int) -> tuple[set[tuple[int, int]], dict[str, tuple[int, int]]]:
    """Validate every structural constraint before building the CP model."""
    categories: list[str] = []
    messages: list[str] = []

    def add(category: str, msg: str) -> None:
        if category not in categories:
            categories.append(category)
        messages.append(msg)

    # --- sample identity / group / batch ----------------------------------
    sample_ids = [s.id for s in req.samples]
    if len(set(sample_ids)) != len(sample_ids):
        dupes = sorted({sid for sid in sample_ids if sample_ids.count(sid) > 1})
        add(VIOLATION_SAMPLE_IDS_DUPLICATED, f"duplicate sample id(s): {', '.join(dupes)}")

    for s in req.samples:
        if not s.group.strip() or not s.batch.strip():
            add(VIOLATION_GROUP_BATCH_INVALID, f"sample '{s.id}' has an empty group or batch")
            break

    # --- blocked wells -----------------------------------------------------
    blocked: set[tuple[int, int]] = set()
    for raw in req.blocked_wells:
        try:
            blocked.add(parse_well(raw, rows, cols))
        except ValueError as exc:
            add(VIOLATION_WELL_REFERENCE_INVALID, str(exc))

    # --- fixed assignments -------------------------------------------------
    fixed_by_sample: dict[str, tuple[int, int]] = {}
    fixed_well_seen: dict[tuple[int, int], str] = {}
    known_ids = set(sample_ids)
    for fa in req.fixed:
        if fa.sample_id not in known_ids:
            add(
                VIOLATION_WELL_REFERENCE_INVALID,
                f"fixed assignment references unknown sample '{fa.sample_id}'",
            )
            continue
        try:
            pos = parse_well(fa.well, rows, cols)
        except ValueError as exc:
            add(VIOLATION_WELL_REFERENCE_INVALID, str(exc))
            continue
        if pos in blocked:
            add(
                VIOLATION_FIXED_ON_BLOCKED,
                f"sample '{fa.sample_id}' fixed at {fa.well}, which is blocked",
            )
        if fa.sample_id in fixed_by_sample and fixed_by_sample[fa.sample_id] != pos:
            add(VIOLATION_FIXED_CONFLICT, f"sample '{fa.sample_id}' fixed to more than one well")
        if pos in fixed_well_seen and fixed_well_seen[pos] != fa.sample_id:
            add(
                VIOLATION_FIXED_CONFLICT,
                f"well {fa.well} fixed to multiple samples "
                f"('{fixed_well_seen[pos]}' and '{fa.sample_id}')",
            )
        fixed_by_sample[fa.sample_id] = pos
        fixed_well_seen.setdefault(pos, fa.sample_id)

    # --- capacity ----------------------------------------------------------
    n_samples = len(req.samples)
    n_available = rows * cols - len(blocked)
    if n_samples > n_available:
        add(
            VIOLATION_CAPACITY_EXCEEDED,
            f"{n_samples} samples but only {n_available} available wells "
            f"({rows * cols} total, {len(blocked)} blocked)",
        )

    if categories:
        raise InfeasibleArrangement(categories, messages)

    return blocked, fixed_by_sample


class _StageModel:
    """CP-SAT model for one lexicographic stage (rebuilt per stage)."""

    def __init__(self, rows: int, cols: int, blocked: set[tuple[int, int]], req: ArrangeRequest,
                 need_adjacency: bool = True):
        self.rows = rows
        self.cols = cols
        self.model = cp_model.CpModel()
        self.samples = sorted(req.samples, key=lambda s: s.id)
        self.n = len(self.samples)
        self.id_to_sample = {s.id: s for s in self.samples}
        self.groups = sorted({s.group for s in self.samples})
        self.batches = sorted({s.batch for s in self.samples})
        self.group_idx = {g: i for i, g in enumerate(self.groups)}
        self.batch_idx = {b: i for i, b in enumerate(self.batches)}

        self.wells = [(r, c) for r in range(rows) for c in range(cols) if (r, c) not in blocked]
        self.well_index = {w: i for i, w in enumerate(self.wells)}

        # x[i][j]: sample i occupies well j
        self.x = [
            [self.model.new_bool_var(f"x_{i}_{j}") for j in range(len(self.wells))]
            for i in range(self.n)
        ]
        # occ[w]: 1 iff well w is occupied
        self.occ = {w: self.model.new_bool_var(f"occ_{w[0]}_{w[1]}") for w in self.wells}
        # b[w]: batch index at well w, or -1 when empty
        self.b = {}

        self._build_assignment_constraints(req)
        # Adjacency structure is only needed by stages 2 and 3; group counts
        # are needed by every stage (objective or pin). Building lazily keeps
        # the stage-1 model small.
        self._build_group_counts()
        self.edges: list = []
        self.y: dict = {}
        self._same: dict = {}
        if need_adjacency:
            self._build_adjacency()

        # Optional balance-extreme vars (created with the objective/pins).
        self._balance_vars: dict[str, tuple] = {}

    # ------------------------------------------------------------- structure
    def _build_assignment_constraints(self, req) -> None:
        for i in range(self.n):
            self.model.add_exactly_one(self.x[i])
        for j, w in enumerate(self.wells):
            col = [self.x[i][j] for i in range(self.n)]
            self.model.add(sum(col) <= 1)
            self.model.add(self.occ[w] == sum(col))
        for fa in req.fixed:
            i = next(k for k, s in enumerate(self.samples) if s.id == fa.sample_id)
            j = self.well_index[parse_well(fa.well, self.rows, self.cols)]
            self.model.add(self.x[i][j] == 1)

    def _build_group_counts(self) -> None:
        self.gr: dict[str, list] = {}
        self.gc: dict[str, list] = {}
        for g in self.groups:
            member_rows: dict[int, list] = {r: [] for r in range(self.rows)}
            member_cols: dict[int, list] = {c: [] for c in range(self.cols)}
            for i, s in enumerate(self.samples):
                if s.group != g:
                    continue
                for j, (r, c) in enumerate(self.wells):
                    member_rows[r].append(self.x[i][j])
                    member_cols[c].append(self.x[i][j])
            rv, cv = [], []
            for r in range(self.rows):
                v = self.model.new_int_var(0, self.n, f"gr_{g}_{r}")
                self.model.add(v == sum(member_rows[r]))
                rv.append(v)
            for c in range(self.cols):
                v = self.model.new_int_var(0, self.n, f"gc_{g}_{c}")
                self.model.add(v == sum(member_cols[c]))
                cv.append(v)
            self.gr[g] = rv
            self.gc[g] = cv

    def _build_adjacency(self) -> None:
        # Orthogonal undirected edges between available wells.
        well_set = set(self.wells)
        self.edges = []
        for r, c in self.wells:
            if (r + 1, c) in well_set:
                self.edges.append(((r, c), (r + 1, c)))
            if (r, c + 1) in well_set:
                self.edges.append(((r, c), (r, c + 1)))

        n_b = len(self.batches)
        for w in self.wells:
            var = self.model.new_int_var(-1, max(n_b - 1, 0), f"batch_{w[0]}_{w[1]}")
            self.model.add(var == -1).only_enforce_if(self.occ[w].Not())
            for i, s in enumerate(self.samples):
                j = self.well_index[w]
                self.model.add(var == self.batch_idx[s.batch]).only_enforce_if(self.x[i][j])
            self.b[w] = var

        # y[e] <=> occ[u] AND occ[v] AND b[u] == b[v]
        self.y = {}
        self._same = {}
        for ei, (u, v) in enumerate(self.edges):
            y = self.model.new_bool_var(f"adj_{ei}")
            same = self.model.new_bool_var(f"adjsame_{ei}")
            self.model.add(self.b[u] == self.b[v]).only_enforce_if(same)
            self.model.add(self.b[u] != self.b[v]).only_enforce_if(same.Not())
            self.model.add(y <= self.occ[u])
            self.model.add(y <= self.occ[v])
            self.model.add(y <= same)
            self.model.add(y >= self.occ[u] + self.occ[v] + same - 2)
            self.y[ei] = y
            self._same[ei] = same

    # ------------------------------------------------------------- objectives
    def _ensure_balance_vars(self):
        if self._balance_vars:
            return
        for g in self.groups:
            rmax = self.model.new_int_var(0, self.n, f"rmax_{g}")
            rmin = self.model.new_int_var(0, self.n, f"rmin_{g}")
            cmax = self.model.new_int_var(0, self.n, f"cmax_{g}")
            cmin = self.model.new_int_var(0, self.n, f"cmin_{g}")
            self.model.add_max_equality(rmax, self.gr[g])
            self.model.add_min_equality(rmin, self.gr[g])
            self.model.add_max_equality(cmax, self.gc[g])
            self.model.add_min_equality(cmin, self.gc[g])
            rspread = self.model.new_int_var(0, self.n, f"rspread_{g}")
            cspread = self.model.new_int_var(0, self.n, f"cspread_{g}")
            self.model.add(rspread == rmax - rmin)
            self.model.add(cspread == cmax - cmin)
            self._balance_vars[g] = (rmax, rmin, cmax, cmin, rspread, cspread)

    def add_balance_objective(self):
        self._ensure_balance_vars()
        extremes = []
        for g in self.groups:
            rmax, rmin, cmax, cmin, rspread, cspread = self._balance_vars[g]
            extremes.extend([rspread, cspread])
        d = self.model.new_int_var(0, self.n, "D")
        self.model.add_max_equality(d, extremes)
        self.model.minimize(d)
        self._d_var = d
        return d

    def pin_d(self, d: int) -> None:
        self._ensure_balance_vars()
        for g in self.groups:
            rmax, rmin, cmax, cmin, rspread, cspread = self._balance_vars[g]
            self.model.add(rspread <= d)
            self.model.add(cspread <= d)

    def add_adjacency_objective(self):
        expr = sum(self.y.values())
        self.model.minimize(expr)
        return expr

    def pin_adjacency(self, a: int) -> None:
        self.model.add(sum(self.y.values()) <= a)

    # ----------------------------------------------------------------- hints
    def assignment_from_solver(self, solver) -> dict[str, tuple[int, int]]:
        out = {}
        for i, s in enumerate(self.samples):
            for j, w in enumerate(self.wells):
                if solver.value(self.x[i][j]) == 1:
                    out[s.id] = w
                    break
        return out

    def add_hint_from_assignment(self, assignment: dict[str, tuple[int, int]]) -> None:
        """Provide values for *every* variable so the hint is a full solution."""
        index_of_sample = {s.id: i for i, s in enumerate(self.samples)}
        hint_vars, hint_vals = [], []

        for i in range(self.n):
            j = self.well_index[assignment[self.samples[i].id]]
            for k in range(len(self.wells)):
                hint_vars.append(self.x[i][k])
                hint_vals.append(1 if k == j else 0)

        occupied = set(assignment.values())
        sample_at = {pos: sid for sid, pos in assignment.items()}
        for w in self.wells:
            hint_vars.append(self.occ[w])
            hint_vals.append(1 if w in occupied else 0)
        if self.b:
            for w in self.wells:
                hint_vars.append(self.b[w])
                hint_vals.append(self.batch_idx[self.id_to_sample[sample_at[w]].batch]
                                 if w in sample_at else -1)

        gr_vals = {g: [0] * self.rows for g in self.groups}
        gc_vals = {g: [0] * self.cols for g in self.groups}
        for sid, (r, c) in assignment.items():
            g = self.id_to_sample[sid].group
            gr_vals[g][r] += 1
            gc_vals[g][c] += 1
        for g in self.groups:
            for r in range(self.rows):
                hint_vars.append(self.gr[g][r])
                hint_vals.append(gr_vals[g][r])
            for c in range(self.cols):
                hint_vars.append(self.gc[g][c])
                hint_vals.append(gc_vals[g][c])

        batch_at = {w: self.id_to_sample[sid].batch for w, sid in
                    ((pos, sample_at[pos]) for pos in occupied)}
        for ei, (u, v) in enumerate(self.edges):
            same_batch = u in batch_at and v in batch_at and batch_at[u] == batch_at[v]
            both_empty = u not in occupied and v not in occupied
            hint_vars.append(self._same[ei])
            hint_vals.append(1 if same_batch or both_empty else 0)
            hint_vars.append(self.y[ei])
            hint_vals.append(1 if same_batch else 0)

        if self._balance_vars:
            for g in self.groups:
                rmax, rmin, cmax, cmin, rspread, cspread = self._balance_vars[g]
                rv, cv = gr_vals[g], gc_vals[g]
                for var, val in (
                    (rmax, max(rv)), (rmin, min(rv)),
                    (cmax, max(cv)), (cmin, min(cv)),
                    (rspread, max(rv) - min(rv)),
                    (cspread, max(cv) - min(cv)),
                ):
                    hint_vars.append(var)
                    hint_vals.append(val)
            d_hint = max(
                [max(v) - min(v) for v in gr_vals.values()]
                + [max(v) - min(v) for v in gc_vals.values()],
                default=0,
            )
            # The D var is only present in the stage-1 model.
            if hasattr(self, "_d_var"):
                hint_vars.append(self._d_var)
                hint_vals.append(d_hint)

        for var, val in zip(hint_vars, hint_vals):
            self.model.add_hint(var, val)


def _heuristic_assignment(req: ArrangeRequest, rows: int, cols: int,
                          blocked: set[tuple[int, int]],
                          fixed_by_sample: dict[str, tuple[int, int]],
                          seed: int) -> dict[str, tuple[int, int]]:
    """Construct a feasible placement as a CP-SAT warm-start hint.

    Fixed samples stay pinned; remaining wells are scanned diagonally (which
    spreads consecutive positions across rows and columns, ties broken by the
    seed) and dealt to free samples in a round-robin over (group, batch).
    """
    rng = random.Random(seed)
    assignment: dict[str, tuple[int, int]] = dict(fixed_by_sample)
    used = set(fixed_by_sample.values())

    free_wells = [
        (r, c) for r in range(rows) for c in range(cols)
        if (r, c) not in blocked and (r, c) not in used
    ]
    free_wells.sort(key=lambda w: (w[0] + w[1], rng.random(), w[0]))

    free_samples = [s for s in req.samples if s.id not in assignment]
    groups = sorted({s.group for s in free_samples})
    batches = sorted({s.batch for s in free_samples})
    gpos = {g: i for i, g in enumerate(groups)}
    bpos = {b: i for i, b in enumerate(batches)}
    span = max(len(batches), 1)
    free_samples.sort(key=lambda s: (
        (gpos[s.group] + bpos[s.batch]) % span, gpos[s.group], bpos[s.batch], s.id))
    for s, w in zip(free_samples, free_wells):
        assignment[s.id] = w
    return assignment


def _make_solver(seed: int, det_limit: float, wall_limit: float, *,
                 stop_at_first: bool = False, fast_presolve: bool = False):
    solver = cp_model.CpSolver()
    # Deterministic configuration: single worker, fixed seed.
    solver.parameters.num_search_workers = int(os.getenv("CP_NUM_WORKERS", "1"))
    solver.parameters.random_seed = int(seed) % (2**31)
    solver.parameters.max_deterministic_time = det_limit
    solver.parameters.max_time_in_seconds = wall_limit
    solver.parameters.log_search_progress = False
    if stop_at_first:
        solver.parameters.stop_after_first_solution = True
    if fast_presolve:
        # Skip heavy presolve/symmetry work: with a complete solution hint
        # this instantiates the hint almost immediately, giving a guaranteed
        # feasible fallback within the budget on every feasible instance.
        solver.parameters.cp_model_presolve = False
        solver.parameters.symmetry_level = 0
        solver.parameters.cp_model_probing_level = 0
    else:
        # Symmetry breaking on highly symmetric plate models can prevent the
        # hint from being instantiated within a small budget; disable it and
        # probing to keep the search both fast and reproducible.
        solver.parameters.symmetry_level = 0
        solver.parameters.cp_model_probing_level = 0
    return solver


def _solve_stage(stage: str, model: _StageModel, seed: int,
                 first_solution: bool = False):
    det_limit = float(os.getenv("CP_DETERMINISTIC_TIME_SECONDS", "4"))
    wall_limit = float(os.getenv("CP_WALL_TIME_SECONDS", "12"))
    fast_det = float(os.getenv("CP_FAST_DETERMINISTIC_TIME_SECONDS", "1"))
    fast_wall = float(os.getenv("CP_FAST_WALL_TIME_SECONDS", "8"))

    if first_solution:
        # Deterministic tie-break: instantiate the hinted (D*, A*)-optimal
        # layout without any optimization or presolve overhead.
        solver = _make_solver(seed, fast_det, fast_wall,
                              stop_at_first=True, fast_presolve=True)
        t0 = time.perf_counter()
        status = solver.solve(model.model)
        elapsed = time.perf_counter() - t0
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return solver, StageInfo(stage, -1, solver.status_name(status),
                                     status == cp_model.OPTIMAL, elapsed)
        if status == cp_model.INFEASIBLE:
            raise InfeasibleArrangement(
                [VIOLATION_SOLVER_INFEASIBLE],
                [f"CP-SAT proved infeasibility at stage {stage}"],
            )
        raise SolverTimeout(stage, elapsed)

    # Phase A: cheap feasibility solve that instantiates the complete hint,
    # guaranteeing a feasible fallback even if the budget is too tight.
    fast = _make_solver(seed, fast_det, fast_wall,
                        stop_at_first=True, fast_presolve=True)
    t0 = time.perf_counter()
    fast_status = fast.solve(model.model)
    fast_elapsed = time.perf_counter() - t0
    if fast_status == cp_model.INFEASIBLE:
        raise InfeasibleArrangement(
            [VIOLATION_SOLVER_INFEASIBLE],
            [f"CP-SAT proved infeasibility at stage {stage}"],
        )
    fallback = fast if fast_status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else None

    # Phase B: full single-worker search within the deterministic budget.
    solver = _make_solver(seed, det_limit, wall_limit)
    t0 = time.perf_counter()
    status = solver.solve(model.model)
    elapsed = time.perf_counter() - t0
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return solver, StageInfo(stage, int(solver.objective_value),
                                 solver.status_name(status),
                                 status == cp_model.OPTIMAL, elapsed)
    if status == cp_model.INFEASIBLE:
        raise InfeasibleArrangement(
            [VIOLATION_SOLVER_INFEASIBLE],
            [f"CP-SAT proved infeasibility at stage {stage}"],
        )
    if fallback is not None:
        # Budget exhausted without an optimizing solution; return the
        # deterministic feasible fallback instead of failing the request.
        return fallback, StageInfo(
            stage, int(fallback.objective_value), "FEASIBLE_FALLBACK", False,
            fast_elapsed + elapsed)
    raise SolverTimeout(stage, elapsed)


def plan_layout(req: ArrangeRequest) -> PlannedLayout:
    """Run the full lexicographic optimization and return the layout."""
    rows, cols = _resolve_dimensions(req.plate)
    blocked, fixed_by_sample = _preflight(req, rows, cols)

    seed31 = int(req.seed) % (2**31)
    stages: list[StageInfo] = []

    hint0 = _heuristic_assignment(req, rows, cols, blocked, fixed_by_sample, seed31)

    # Stage 1: minimize group balance spread D (no adjacency vars needed).
    m1 = _StageModel(rows, cols, blocked, req, need_adjacency=False)
    m1.add_balance_objective()
    m1.add_hint_from_assignment(hint0)
    solver1, info1 = _solve_stage("1_balance", m1, seed31)
    best_d = int(solver1.objective_value)
    stages.append(info1)

    # Stage 2: minimize same-batch orthogonal adjacency, pinning D.
    m2 = _StageModel(rows, cols, blocked, req)
    m2.pin_d(best_d)
    m2.add_adjacency_objective()
    m2.add_hint_from_assignment(m1.assignment_from_solver(solver1))
    solver2, info2 = _solve_stage("2_adjacency", m2, seed31)
    best_a = int(solver2.objective_value)
    stages.append(info2)

    # Stage 3: deterministic tie-break — the (D*, A*)-optimal stage-2 layout
    # is fed back as a complete hint and returned as the first solution.
    m3 = _StageModel(rows, cols, blocked, req)
    m3.pin_d(best_d)
    m3.pin_adjacency(best_a)
    m3.add_hint_from_assignment(m2.assignment_from_solver(solver2))
    solver3, info3 = _solve_stage("3_tiebreak", m3, seed31, first_solution=True)
    stages.append(info3)

    return _extract(m3, solver3, req, rows, cols, blocked, stages)


def _extract(model: _StageModel, solver, req, rows, cols, blocked, stages) -> PlannedLayout:
    placement: dict[str, str] = {}
    for i, s in enumerate(model.samples):
        for j, w in enumerate(model.wells):
            if solver.value(model.x[i][j]) == 1:
                placement[s.id] = well_name(*w)
                break

    # Metrics are recounted independently in Python (authoritative).
    batch_of = {s.id: s.batch for s in req.samples}
    pos = {sid: parse_well(wn, rows, cols) for sid, wn in placement.items()}
    sample_at = {p: sid for sid, p in pos.items()}
    same_pairs: set[tuple[str, str]] = set()
    for sid, (r, c) in pos.items():
        for other_pos in ((r + 1, c), (r, c + 1)):
            other = sample_at.get(other_pos)
            if other is not None and batch_of[sid] == batch_of[other]:
                same_pairs.add(tuple(sorted((well_name(r, c), well_name(*other_pos)))))

    group_row_counts = {g: {r: 0 for r in range(rows)} for g in model.groups}
    group_col_counts = {g: {c: 0 for c in range(cols)} for g in model.groups}
    sample_by_id = {s.id: s for s in req.samples}
    for sid, (r, c) in pos.items():
        g = sample_by_id[sid].group
        group_row_counts[g][r] += 1
        group_col_counts[g][c] += 1

    available = [
        well_name(r, c) for r in range(rows) for c in range(cols) if (r, c) not in blocked
    ]
    return PlannedLayout(
        rows=rows,
        columns=cols,
        available_wells=available,
        placement=placement,
        same_batch_adjacent_pairs=sorted(same_pairs),
        groups=model.groups,
        batches=model.batches,
        group_row_counts={g: dict(sorted(rc.items())) for g, rc in group_row_counts.items()},
        group_col_counts={g: dict(sorted(cc.items())) for g, cc in group_col_counts.items()},
        stages=stages,
    )
