"""Batch release verification: scan agreement first, spatial QC gates second.

Two-stage gate for ``POST /api/v1/arrangements/batch-release``:

1. **Scan agreement.** The submitted full scan is checked against the
   planned layout with the exact same rules as the reconciliation endpoint
   (its preflight plus the open-chain/closed-loop decomposition): no missing
   planned sample, no unplanned sample, no blocked-well occupancy and no
   misplacement of either kind (an open chain means a planned well is empty;
   a closed loop is a cyclic displacement). Any disagreement makes the
   request unprocessable (422): the problem categories, the complete
   misplaced-well list (``expected``/``actual``, ``null`` marks an
   expected/observed empty well) and any closed loops are returned, and no
   release conclusion is produced.
2. **Spatial QC gates.** Only when the scan matches the plan well by well is
   the existing spatial QC pipeline reused (group-median centring, median
   polish, seed-controlled within-group permutation test). The batch is
   released only when the polish converged, the maximal absolute row/column
   effect is at most ``max_spatial_effect`` and the plus-one-corrected
   p-value reaches ``min_p_value``. With ``permutations=0`` no p-value
   exists, so the p-value gate can never pass. A failed gate returns 200
   with verdict ``NOT_RELEASED`` and the failing reason(s); no partial
   conclusion (no QC summary) is emitted in that case.
"""
from __future__ import annotations

from .plates import parse_well, well_name
from .planner import InfeasibleArrangement, _resolve_dimensions
from .reconciler import (
    ReconcileInvalid,
    _decompose,
    _preflight,
)
from .schemas import (
    RELEASE_REASON_P_VALUE_BELOW_THRESHOLD,
    RELEASE_REASON_P_VALUE_UNAVAILABLE,
    RELEASE_REASON_SPATIAL_EFFECT_EXCEEDED,
    BatchReleaseRequest,
    SpatialQCRequest,
    VIOLATION_CLOSED_LOOP_MISPLACEMENT,
    VIOLATION_SCAN_LAYOUT_MISMATCH,
    VIOLATION_SPATIAL_NOT_CONVERGED,
)
from .spatial_qc import SpatialQCInvalid, observed_polish, spatial_qc

# Verdict labels used in every 200 response.
VERDICT_RELEASED = "RELEASED"
VERDICT_NOT_RELEASED = "NOT_RELEASED"


class ReleaseInvalid(Exception):
    """Stage-1 (scan agreement) or request-structure failure (HTTP 422).

    No release verdict is produced; ``misplaced_wells`` lists every well
    whose scanned content differs from the plan (best effort when the
    request is structurally inconsistent).
    """

    def __init__(
        self,
        categories: list[str],
        messages: list[str],
        misplaced_wells: list[dict] | None = None,
        cycles: list[list[str]] | None = None,
    ):
        self.categories = categories
        self.messages = messages
        self.misplaced_wells = misplaced_wells or []
        self.cycles = cycles or []
        super().__init__("; ".join(messages) if messages else "invalid batch release request")


def release_invalid_body(exc: ReleaseInvalid) -> dict:
    """Canonical 422 envelope for a stage-1/structural release failure.

    Shared by the batch-release endpoint's exception handler and the batch
    certificate endpoint so both emit the identical body shape.
    """
    return {
        "detail": "The batch cannot be released; the scan does not match "
        "the plan (or the request is inconsistent), and no release "
        "conclusion was produced.",
        "infeasibility_categories": exc.categories,
        "messages": exc.messages,
        "misplaced_wells": exc.misplaced_wells,
        "cycles": exc.cycles,
    }


def _best_effort_misplaced(req: BatchReleaseRequest, rows: int, cols: int) -> list[dict]:
    """Misplaced-well list reconstructed without relying on a clean preflight.

    Used when the reconciliation preflight already rejected the request (bad
    well names, duplicates, missing/unplanned samples, blocked occupancy...):
    unparseable entries are skipped rather than hiding the wells that *can*
    be compared.
    """
    need: dict[tuple[int, int], str] = {}
    for sid, raw in req.target.items():
        try:
            need[parse_well(raw, rows, cols)] = sid
        except ValueError:
            continue
    scanned: dict[tuple[int, int], str] = {}
    for raw, sid in req.scan.items():
        try:
            scanned[parse_well(raw, rows, cols)] = sid
        except ValueError:
            continue
    misplaced = []
    for pos in sorted(set(need) | set(scanned)):
        expected, actual = need.get(pos), scanned.get(pos)
        if expected != actual:
            misplaced.append(
                {"well": well_name(*pos), "expected": expected, "actual": actual}
            )
    return misplaced


def _check_scan(req: BatchReleaseRequest, rows: int, cols: int):
    """Stage 1: enforce the existing reconciliation rules, verdict-free."""
    try:
        blocked, target_pos, scan_pos = _preflight(req, rows, cols)
    except ReconcileInvalid as exc:
        raise ReleaseInvalid(
            exc.categories,
            exc.messages,
            misplaced_wells=_best_effort_misplaced(req, rows, cols),
        ) from exc

    need, _chains, cycles = _decompose(target_pos, scan_pos)

    misplaced = []
    for pos in sorted(set(need) | set(scan_pos)):
        expected, actual = need.get(pos), scan_pos.get(pos)
        if expected != actual:
            misplaced.append(
                {"well": well_name(*pos), "expected": expected, "actual": actual}
            )

    if not misplaced:
        # Exact agreement: nothing to release-block.
        return blocked

    categories: list[str] = []
    messages: list[str] = []
    if cycles:
        categories.append(VIOLATION_CLOSED_LOOP_MISPLACEMENT)
        messages.append(
            f"scan contains {len(cycles)} closed-loop misplacement cycle(s): "
            + "; ".join(" -> ".join(well_name(*w) for w in cyc) for cyc in cycles)
        )
    categories.append(VIOLATION_SCAN_LAYOUT_MISMATCH)
    messages.append(
        f"scan disagrees with the planned layout at {len(misplaced)} well(s); "
        "reconcile the plate before requesting release"
    )
    raise ReleaseInvalid(
        categories,
        messages,
        misplaced_wells=misplaced,
        cycles=[[well_name(*w) for w in cyc] for cyc in cycles],
    )


def _to_spatial_request(req: BatchReleaseRequest) -> SpatialQCRequest:
    """Project the release request onto the existing spatial QC schema."""
    return SpatialQCRequest(
        plate=req.plate,
        layout=dict(req.target),
        samples=list(req.samples),
        readings=dict(req.readings),
        seed=req.seed,
        permutations=req.permutations,
        tolerance=req.tolerance,
        max_iterations=req.max_iterations,
    )


def _scan_summary(req: BatchReleaseRequest, rows: int, cols: int,
                  blocked: set[tuple[int, int]]) -> dict:
    """Facts about the agreed scan; emitted only on a RELEASED verdict."""
    scanned = {}
    for raw, sid in req.scan.items():
        pos = parse_well(raw, rows, cols)  # preflight guaranteed legality
        scanned[pos] = sid
    occupied = len(scanned)
    return {
        "scanned_wells": occupied,
        "planned_samples": len(req.target),
        "misplaced_wells": [],
        "blocked_wells": [well_name(*w) for w in sorted(blocked)],
        "blocked_wells_occupied": 0,
        "matches_plan": True,
    }


def batch_release(req: BatchReleaseRequest) -> dict:
    """Run the two release stages and assemble the verdict response."""
    # Plate geometry is resolved here so stage-1 failures can still report
    # misplaced wells; an invalid plate is a plain structural 422.
    try:
        rows, cols = _resolve_dimensions(req.plate)
    except InfeasibleArrangement as exc:
        raise ReleaseInvalid(exc.categories, exc.messages) from exc

    blocked = _check_scan(req, rows, cols)

    # Stage 2: reuse the spatial QC pipeline unchanged.
    qc_req = _to_spatial_request(req)
    try:
        qc = spatial_qc(qc_req)
    except SpatialQCInvalid as exc:
        if exc.categories == [VIOLATION_SPATIAL_NOT_CONVERGED]:
            # Convergence is a release gate rather than a malformed request.
            # The failure may come from the observed polish or from a
            # permutation replicate; polish the observed data separately so
            # the gate reports its convergence fact and iteration count
            # accurately (effects stay withheld: no partial conclusion).
            try:
                observed = observed_polish(qc_req)
                converged, iterations = observed.converged, observed.iterations
            except SpatialQCInvalid:
                converged, iterations = False, req.max_iterations
            return _not_released(
                req, rows, cols, exc.messages,
                converged=converged, iterations=iterations,
            )
        raise ReleaseInvalid(exc.categories, exc.messages) from exc

    effects = qc["effects"]
    pt = qc["permutation_test"]
    observed_stat = effects["max_abs_row_col_effect"]
    p_value = pt["p_value"]

    thresholds = {
        "max_spatial_effect": req.max_spatial_effect,
        "min_p_value": req.min_p_value,
        "permutations": req.permutations,
        "tolerance": req.tolerance,
        "max_iterations": req.max_iterations,
    }
    gates = {
        "converged": True,
        "iterations": qc["convergence"]["iterations"],
        "max_abs_row_col_effect": observed_stat,
        "max_spatial_effect": req.max_spatial_effect,
        "effect_within_limit": observed_stat <= req.max_spatial_effect,
        "observed_p_value": p_value,
        "min_p_value": req.min_p_value,
        "p_value_available": p_value is not None,
        "p_value_meets_threshold": (
            p_value is not None and p_value >= req.min_p_value
        ),
    }

    reasons: list[str] = []
    messages: list[str] = []
    if not gates["effect_within_limit"]:
        reasons.append(RELEASE_REASON_SPATIAL_EFFECT_EXCEEDED)
        messages.append(
            f"max absolute row/column effect {observed_stat:g} exceeds the "
            f"limit {req.max_spatial_effect:g}"
        )
    if p_value is None:
        reasons.append(RELEASE_REASON_P_VALUE_UNAVAILABLE)
        messages.append(
            "no permutation p-value is available (permutations=0); "
            f"the p-value gate (minimum {req.min_p_value:g}) cannot be passed"
        )
    elif p_value < req.min_p_value:
        reasons.append(RELEASE_REASON_P_VALUE_BELOW_THRESHOLD)
        messages.append(
            f"permutation p-value {p_value:g} is below the minimum "
            f"{req.min_p_value:g}"
        )

    if reasons:
        return {
            "verdict": VERDICT_NOT_RELEASED,
            "seed": req.seed,
            "plate": {"type": req.plate.type, "rows": rows, "columns": cols},
            "reasons": reasons,
            "messages": messages,
            "thresholds": thresholds,
            "gates": gates,
        }

    return {
        "verdict": VERDICT_RELEASED,
        "seed": req.seed,
        "plate": {"type": req.plate.type, "rows": rows, "columns": cols},
        "thresholds": thresholds,
        "gates": gates,
        "scan_summary": _scan_summary(req, rows, cols, blocked),
        "spatial_qc_summary": qc,
    }


def _not_released(req: BatchReleaseRequest, rows: int, cols: int,
                  messages: list[str], *, converged: bool,
                  iterations: int) -> dict:
    """NOT_RELEASED body for a failed convergence gate (no QC partial data)."""
    return {
        "verdict": VERDICT_NOT_RELEASED,
        "seed": req.seed,
        "plate": {"type": req.plate.type, "rows": rows, "columns": cols},
        "reasons": [VIOLATION_SPATIAL_NOT_CONVERGED],
        "messages": messages,
        "thresholds": {
            "max_spatial_effect": req.max_spatial_effect,
            "min_p_value": req.min_p_value,
            "permutations": req.permutations,
            "tolerance": req.tolerance,
            "max_iterations": req.max_iterations,
        },
        "gates": {
            "converged": converged,
            "iterations": iterations,
            "max_abs_row_col_effect": None,
            "max_spatial_effect": req.max_spatial_effect,
            "effect_within_limit": False,
            "observed_p_value": None,
            "min_p_value": req.min_p_value,
            "p_value_available": False,
            "p_value_meets_threshold": False,
        },
    }
