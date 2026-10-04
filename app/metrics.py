"""Metric computation and independent constraint verification of layouts."""
from __future__ import annotations

from dataclasses import dataclass

from .plates import parse_well, well_name
from .schemas import ArrangeRequest


@dataclass
class GroupSpread:
    group: str
    row_counts: list[int]
    col_counts: list[int]
    row_max: int
    row_min: int
    row_spread: int
    col_max: int
    col_min: int
    col_spread: int


def compute_spreads(layout, rows: int, cols: int) -> list[GroupSpread]:
    out: list[GroupSpread] = []
    for g in layout.groups:
        rc = layout.group_row_counts[g]
        cc = layout.group_col_counts[g]
        rvals = [rc[r] for r in range(rows)]
        cvals = [cc[c] for c in range(cols)]
        out.append(
            GroupSpread(
                group=g,
                row_counts=rvals,
                col_counts=cvals,
                row_max=max(rvals),
                row_min=min(rvals),
                row_spread=max(rvals) - min(rvals),
                col_max=max(cvals),
                col_min=min(cvals),
                col_spread=max(cvals) - min(cvals),
            )
        )
    return out


def count_adjacent_pairs(req: ArrangeRequest, placement: dict[str, str], rows: int, cols: int):
    """Pure-Python recount of orthogonal adjacent same-batch pairs.

    Returns (pairs, total_orthogonal_edges_on_plate).
    """
    well_to_sample: dict[tuple[int, int], str] = {}
    for sid, wn in placement.items():
        well_to_sample[parse_well(wn, rows, cols)] = sid
    batch_of = {s.id: s.batch for s in req.samples}

    pairs: list[tuple[str, str]] = []
    for (r, c), sid in well_to_sample.items():
        down = (r + 1, c)
        right = (r, c + 1)
        for other in (down, right):
            if other in well_to_sample and batch_of[sid] == batch_of[well_to_sample[other]]:
                pairs.append(tuple(sorted((well_name(r, c), well_name(*other)))))
    pairs = sorted(set(pairs))
    total_edges = rows * (cols - 1) + (rows - 1) * cols
    return pairs, total_edges


def verify_layout(req: ArrangeRequest, layout, rows: int, cols: int) -> dict:
    """Independently re-verify every hard constraint on the returned layout.

    Every check is independent; failures carry human-readable details.
    """
    checks: dict[str, dict] = {}
    blocked = {parse_well(w, rows, cols) for w in req.blocked_wells}

    # 1. Every sample placed, exactly once, on a valid well.
    assigned_wells: list[tuple[int, int]] = []
    problems: list[str] = []
    sample_ids = {s.id for s in req.samples}
    if set(layout.placement.keys()) != sample_ids:
        missing = sample_ids - set(layout.placement.keys())
        extra = set(layout.placement.keys()) - sample_ids
        if missing:
            problems.append(f"samples without a well: {sorted(missing)}")
        if extra:
            problems.append(f"unknown samples placed: {sorted(extra)}")
    for sid, wn in layout.placement.items():
        try:
            assigned_wells.append(parse_well(wn, rows, cols))
        except ValueError as exc:
            problems.append(str(exc))
    if len(assigned_wells) != len(set(assigned_wells)):
        problems.append("one or more wells occupied by more than one sample")
    checks["one_well_per_sample"] = {"passed": not problems, "problems": problems}

    # 2. No sample on a blocked well.
    blocked_hits = sorted({well_name(*w) for w in assigned_wells if w in blocked})
    checks["blocked_wells_respected"] = {
        "passed": not blocked_hits,
        "violating_wells": blocked_hits,
    }

    # 3. Fixed assignments unchanged.
    fixed_problems: list[str] = []
    for fa in req.fixed:
        actual = layout.placement.get(fa.sample_id)
        if actual != fa.well.upper():
            fixed_problems.append(
                f"sample '{fa.sample_id}' expected at {fa.well}, found at {actual}"
            )
    checks["fixed_assignments_respected"] = {"passed": not fixed_problems, "problems": fixed_problems}

    # 4. All placed wells inside the plate (parse_well already guarantees this,
    #    but kept as an explicit independent assertion).
    in_range = all(0 <= r < rows and 0 <= c < cols for r, c in assigned_wells)
    checks["wells_in_range"] = {"passed": in_range}

    # 5. Adjacency metric agrees with the reported list.
    pairs, total_edges = count_adjacent_pairs(req, layout.placement, rows, cols)
    metric_matches = pairs == layout.same_batch_adjacent_pairs
    checks["adjacency_metric_consistent"] = {
        "passed": metric_matches,
        "recounted_pairs": pairs,
        "total_orthogonal_edges": total_edges,
    }

    all_passed = all(v["passed"] for v in checks.values())
    return {"all_passed": all_passed, "checks": checks}
