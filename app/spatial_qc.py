"""Spatial QC: group-centred median polish and within-group permutation test.

Pipeline for ``POST /api/v1/arrangements/spatial-qc``:

1. Each numeric reading is centred by the *median* of its experiment group,
   so systematic between-group differences are removed before any spatial
   effect is estimated (otherwise an imbalanced layout would let group means
   leak into row/column effects).
2. The centred values undergo alternating median polish sweeps: a row sweep
   subtracts (and accumulates) the median of every populated row, then a
   column sweep does the same for every populated column. A "round" is one
   row sweep plus one column sweep. The round correction is the largest
   absolute median applied during either sweep; iteration stops when a round
   correction is no greater than ``tolerance``, or after ``max_iterations``.
3. The accumulated effects are canonicalised once at the end (row effects
   recentred to have median zero; the offset moves through the column
   effects into the overall effect) so they are unique regardless of the
   sweep order.

The reported statistic is the largest absolute row or column effect. For the
permutation test, readings are shuffled among the *valid wells of their own
group* (missing-well pattern and per-group validity counts stay fixed; the
group null is exchangeability within a group), the whole polish is repeated,
and the p-value is the plus-one-corrected empirical tail probability
``(1 + #{T_perm >= T_obs}) / (1 + n_perm)``.

Any group with fewer than two valid readings, or valid wells covering fewer
than two distinct rows or two distinct columns, or failure of the polish
(observed or in any permutation replicate) to converge, makes the request
unprocessable (422) and no partial conclusion is returned.
"""
from __future__ import annotations

import random
import statistics
from dataclasses import dataclass

from .planner import InfeasibleArrangement, _resolve_dimensions
from .plates import parse_well, well_name
from .schemas import (
    VIOLATION_PLATE_DIMENSION_INVALID,
    VIOLATION_SPATIAL_GRID_INSUFFICIENT,
    VIOLATION_SPATIAL_GROUP_INSUFFICIENT,
    VIOLATION_SPATIAL_LAYOUT_CONFLICT,
    VIOLATION_SPATIAL_NOT_CONVERGED,
    VIOLATION_SPATIAL_READING_DUPLICATED,
    VIOLATION_SPATIAL_READING_INVALID,
    VIOLATION_SPATIAL_SAMPLE_DUPLICATED,
    VIOLATION_SPATIAL_SAMPLE_UNKNOWN,
    VIOLATION_WELL_REFERENCE_INVALID,
    SpatialQCRequest,
)

# Output floats are rounded to this many decimal places; convergence checks
# always operate on the unrounded values.
_ROUND = 10


class SpatialQCInvalid(Exception):
    """The spatial QC request cannot be analysed (HTTP 422).

    No effects, corrected readings or p-value are produced in this case.
    """

    def __init__(self, categories: list[str], messages: list[str]):
        self.categories = categories
        self.messages = messages
        super().__init__("; ".join(messages) if messages else "invalid spatial QC request")


@dataclass
class PolishResult:
    overall: float
    row_effects: list[float]
    col_effects: list[float]
    # Residuals (centred value minus fitted spatial surface), valid wells only.
    residual: dict[tuple[int, int], float]
    trajectory: list[dict]
    iterations: int
    converged: bool


@dataclass
class _ValidData:
    rows: int
    cols: int
    # valid well -> (sample id, group, reading)
    wells: dict[tuple[int, int], tuple[str, str, float]]
    groups: list[str]
    group_medians: dict[str, float]
    # group -> valid wells occupied by that group, in row-major order
    group_wells: dict[str, list[tuple[int, int]]]


def _preflight(req: SpatialQCRequest) -> _ValidData:
    """Cross-check layout, samples, readings and plate geometry."""
    try:
        rows, cols = _resolve_dimensions(req.plate)
    except InfeasibleArrangement as exc:
        raise SpatialQCInvalid(exc.categories, exc.messages) from exc

    categories: list[str] = []
    messages: list[str] = []

    def add(category: str, msg: str) -> None:
        if category not in categories:
            categories.append(category)
        messages.append(msg)

    # --- samples -----------------------------------------------------------
    group_of: dict[str, str] = {}
    for s in req.samples:
        if s.id in group_of:
            add(
                VIOLATION_SPATIAL_SAMPLE_DUPLICATED,
                f"sample '{s.id}' listed more than once",
            )
        else:
            group_of[s.id] = s.group

    # --- layout: sample -> well -------------------------------------------
    layout_pos: dict[str, tuple[int, int]] = {}
    well_owner: dict[tuple[int, int], str] = {}
    for sid, raw in req.layout.items():
        if sid not in group_of:
            add(
                VIOLATION_SPATIAL_SAMPLE_UNKNOWN,
                f"layout references sample '{sid}' which is absent from samples",
            )
        try:
            pos = parse_well(raw, rows, cols)
        except ValueError as exc:
            add(VIOLATION_WELL_REFERENCE_INVALID, str(exc))
            continue
        if pos in well_owner:
            add(
                VIOLATION_SPATIAL_LAYOUT_CONFLICT,
                f"well {well_name(*pos)} assigned to both "
                f"'{well_owner[pos]}' and '{sid}'",
            )
        else:
            well_owner[pos] = sid
        layout_pos[sid] = pos

    unplaced = sorted(set(group_of) - set(req.layout))
    for sid in unplaced:
        add(
            VIOLATION_SPATIAL_LAYOUT_CONFLICT,
            f"sample '{sid}' is listed in samples but has no well in layout",
        )

    # --- readings: well -> value (null allowed) ---------------------------
    reading_at: dict[tuple[int, int], float | None] = {}
    for raw, value in req.readings.items():
        try:
            pos = parse_well(raw, rows, cols)
        except ValueError as exc:
            add(VIOLATION_WELL_REFERENCE_INVALID, str(exc))
            continue
        if pos in reading_at:
            add(
                VIOLATION_SPATIAL_READING_DUPLICATED,
                f"well {well_name(*pos)} carries more than one reading "
                "(names are case-insensitive)",
            )
            continue
        if pos not in well_owner:
            add(
                VIOLATION_SPATIAL_READING_INVALID,
                f"reading well {well_name(*pos)} is not occupied in the layout",
            )
        reading_at[pos] = value

    if categories:
        raise SpatialQCInvalid(categories, messages)

    # --- build the valid (non-missing) well table -------------------------
    wells: dict[tuple[int, int], tuple[str, str, float]] = {}
    group_values: dict[str, list[float]] = {}
    group_wells: dict[str, list[tuple[int, int]]] = {}
    for sid in sorted(layout_pos):
        pos = layout_pos[sid]
        value = reading_at.get(pos)
        if value is None:
            continue  # omitted wells and explicit null are missing readings
        group = group_of[sid]
        wells[pos] = (sid, group, float(value))
        group_values.setdefault(group, []).append(float(value))
        group_wells.setdefault(group, []).append(pos)

    # Row-major ordering keeps permutation draws deterministic.
    for g in group_wells:
        group_wells[g].sort()

    # --- validity gates: every group >= 2 valid readings ------------------
    for group in sorted(group_of.values()):
        n = len(group_values.get(group, ()))
        if n < 2:
            add(
                VIOLATION_SPATIAL_GROUP_INSUFFICIENT,
                f"group '{group}' has {n} valid reading(s); at least 2 are required",
            )

    valid_rows = {r for r, _ in wells}
    valid_cols = {c for _, c in wells}
    if len(valid_rows) < 2 or len(valid_cols) < 2:
        add(
            VIOLATION_SPATIAL_GRID_INSUFFICIENT,
            f"valid wells cover {len(valid_rows)} row(s) and {len(valid_cols)} "
            "column(s); at least 2 distinct rows and 2 columns are required",
        )

    if categories:
        raise SpatialQCInvalid(categories, messages)

    group_medians = {g: statistics.median(v) for g, v in group_values.items()}
    return _ValidData(
        rows=rows,
        cols=cols,
        wells=wells,
        groups=sorted(group_values),
        group_medians=group_medians,
        group_wells=group_wells,
    )


def _median_polish(
    values: dict[tuple[int, int], float],
    rows: int,
    cols: int,
    tolerance: float,
    max_iterations: int,
) -> PolishResult:
    """Alternating row/column median subtraction with accumulated effects.

    ``values`` holds the already group-centred values at valid wells.
    """
    z = {pos: v for pos, v in values.items()}
    row_eff = [0.0] * rows
    col_eff = [0.0] * cols
    overall = 0.0
    trajectory: list[dict] = []

    active_rows = sorted({r for r, _ in z})
    active_cols = sorted({c for _, c in z})
    by_row: dict[int, list[tuple[int, int]]] = {r: [] for r in active_rows}
    by_col: dict[int, list[tuple[int, int]]] = {c: [] for c in active_cols}
    for pos in z:
        by_row[pos[0]].append(pos)
        by_col[pos[1]].append(pos)

    converged = False
    iteration = 0
    for iteration in range(1, max_iterations + 1):
        # --- row sweep -----------------------------------------------------
        rmed: dict[int, float] = {}
        for r in active_rows:
            med = statistics.median(z[pos] for pos in by_row[r])
            if med != 0.0:
                rmed[r] = med
                row_eff[r] += med
        for (r, c) in list(z):
            if r in rmed:
                z[(r, c)] -= rmed[r]
        row_correction = max((abs(m) for m in rmed.values()), default=0.0)

        # --- column sweep --------------------------------------------------
        cmed: dict[int, float] = {}
        for c in active_cols:
            med = statistics.median(z[pos] for pos in by_col[c])
            if med != 0.0:
                cmed[c] = med
                col_eff[c] += med
        for (r, c) in list(z):
            if c in cmed:
                z[(r, c)] -= cmed[c]
        col_correction = max((abs(m) for m in cmed.values()), default=0.0)

        max_correction = max(row_correction, col_correction)
        trajectory.append(
            {
                "iteration": iteration,
                "row_correction": row_correction,
                "col_correction": col_correction,
                "max_correction": max_correction,
            }
        )
        if max_correction <= tolerance:
            converged = True
            break

    # Canonicalise: median of row effects -> 0; the offset is moved through
    # the column effects into the overall effect. Residuals are untouched.
    shift = statistics.median(row_eff[r] for r in active_rows)
    if shift != 0.0:
        for r in active_rows:
            row_eff[r] -= shift
        for c in active_cols:
            col_eff[c] += shift
    col_shift = statistics.median(col_eff[c] for c in active_cols)
    if col_shift != 0.0:
        for c in active_cols:
            col_eff[c] -= col_shift
        overall += col_shift

    return PolishResult(
        overall=overall,
        row_effects=row_eff,
        col_effects=col_eff,
        residual=z,
        trajectory=trajectory,
        iterations=iteration,
        converged=converged,
    )


def _max_effect(result: PolishResult) -> float:
    """Test statistic: largest absolute row/column effect."""
    return max(
        [abs(v) for v in result.row_effects]
        + [abs(v) for v in result.col_effects]
    )


def _rounded(value: float) -> float:
    out = round(value, _ROUND)
    # Normalise signed zero so responses and tests stay stable.
    return out if out != 0.0 else 0.0


def _centred_values(data: _ValidData) -> dict[tuple[int, int], float]:
    return {
        pos: reading - data.group_medians[group]
        for pos, (_, group, reading) in data.wells.items()
    }


def observed_polish(req: SpatialQCRequest) -> PolishResult:
    """Run the observed (non-permuted) median polish only.

    Runs the same preflight and polish as :func:`spatial_qc` without any
    permutation replicates; structural failures propagate as
    :class:`SpatialQCInvalid` exactly as in the full pipeline. The returned
    :class:`PolishResult` records whether the observed data itself converged
    (a full run may still fail later on a permutation replicate).
    """
    data = _preflight(req)
    centred = _centred_values(data)
    return _median_polish(
        centred, data.rows, data.cols, req.tolerance, req.max_iterations
    )


def spatial_qc(req: SpatialQCRequest) -> dict:
    """Validate, polish, permute and assemble the spatial QC response."""
    data = _preflight(req)
    rows, cols = data.rows, data.cols

    centred = _centred_values(data)
    observed = _median_polish(
        centred, rows, cols, req.tolerance, req.max_iterations
    )
    if not observed.converged:
        raise SpatialQCInvalid(
            [VIOLATION_SPATIAL_NOT_CONVERGED],
            [
                f"median polish did not converge within {req.max_iterations} "
                f"iterations (last max correction "
                f"{observed.trajectory[-1]['max_correction']:.6g} > tolerance "
                f"{req.tolerance:.6g}); no partial conclusion is returned"
            ],
        )

    observed_stat = _max_effect(observed)

    # --- within-group permutation test ------------------------------------
    exceed = 0
    if req.permutations > 0:
        rng = random.Random(req.seed)
        for _ in range(req.permutations):
            perm_values: dict[tuple[int, int], float] = {}
            for group, positions in data.group_wells.items():
                values = [
                    data.wells[pos][2] - data.group_medians[group]
                    for pos in positions
                ]
                rng.shuffle(values)
                for pos, value in zip(positions, values):
                    perm_values[pos] = value
            perm = _median_polish(
                perm_values, rows, cols, req.tolerance, req.max_iterations
            )
            if not perm.converged:
                raise SpatialQCInvalid(
                    [VIOLATION_SPATIAL_NOT_CONVERGED],
                    [
                        "median polish did not converge for a permutation "
                        f"replicate within {req.max_iterations} iterations; "
                        "no partial conclusion is returned"
                    ],
                )
            stat = _max_effect(perm)
            if stat >= observed_stat:
                exceed += 1
        p_value = (1.0 + exceed) / (1.0 + req.permutations)
    else:
        p_value = None

    # --- assemble response -------------------------------------------------
    group_summary = []
    for group in data.groups:
        positions = data.group_wells[group]
        group_rows = {r for r, _ in positions}
        group_cols = {c for _, c in positions}
        group_summary.append(
            {
                "group": group,
                "valid_readings": len(positions),
                "valid_rows": len(group_rows),
                "valid_columns": len(group_cols),
                "median": _rounded(data.group_medians[group]),
            }
        )

    readings_out = []
    for r in range(rows):
        for c in range(cols):
            pos = (r, c)
            if pos not in data.wells:
                continue
            sid, group, reading = data.wells[pos]
            # Corrected reading: drop the fitted spatial surface (overall +
            # row + column) but keep the group baseline, i.e. group median +
            # median-polish residual.
            corrected = data.group_medians[group] + observed.residual[pos]
            readings_out.append(
                {
                    "well": well_name(r, c),
                    "sample_id": sid,
                    "group": group,
                    "reading": _rounded(reading),
                    "centred_reading": _rounded(
                        reading - data.group_medians[group]
                    ),
                    "corrected_reading": _rounded(corrected),
                }
            )

    return {
        "seed": req.seed,
        "plate": {"type": req.plate.type, "rows": rows, "columns": cols},
        "parameters": {
            "permutations": req.permutations,
            "tolerance": req.tolerance,
            "max_iterations": req.max_iterations,
        },
        "groups": group_summary,
        "effects": {
            "overall": _rounded(observed.overall),
            "row_effects": [_rounded(v) for v in observed.row_effects],
            "col_effects": [_rounded(v) for v in observed.col_effects],
            "max_abs_row_col_effect": _rounded(observed_stat),
        },
        "readings": readings_out,
        "convergence": {
            "converged": True,
            "iterations": observed.iterations,
            "tolerance": req.tolerance,
            "trajectory": [
                {
                    "iteration": step["iteration"],
                    "row_correction": _rounded(step["row_correction"]),
                    "col_correction": _rounded(step["col_correction"]),
                    "max_correction": _rounded(step["max_correction"]),
                }
                for step in observed.trajectory
            ],
        },
        "permutation_test": {
            "permutations": req.permutations,
            "observed_statistic": _rounded(observed_stat),
            "exceedances": exceed,
            "p_value": _rounded(p_value) if p_value is not None else None,
            "method": "within-group well permutation; p=(1+#{T_perm>=T_obs})/(1+n)",
        },
    }
