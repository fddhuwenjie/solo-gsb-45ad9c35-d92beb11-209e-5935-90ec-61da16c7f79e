"""Deterministic dilution-gradient planning for an existing plate layout.

Given the target ``layout`` (sample → well, as produced by the arrangement
endpoint), the stock concentration and available volume of every source
sample, and the desired concentration / final volume of every target well,
this module produces a fully determined sequence of single-channel
pipetting steps.

Pipetting model (one well at a time, wells processed in row-major order):

* step 1 for each well — transfer diluent (omitted entirely when 0 µL);
* step 2 for each well — transfer source stock (always > 0 µL);
* both volumes are integer multiples of ``pipette.resolution_ul`` and never
  smaller than ``pipette.min_aspirate_ul`` (zero-volume transfers are simply
  omitted rather than counted);
* the two volumes add up exactly to the well's ``final_volume_ul``, which
  must itself lie on the resolution grid and fit inside
  ``well_capacity_ul``;
* the resulting concentration ``C_stock · V_stock / V_final`` must be within
  the request's relative ``concentration_tolerance`` of the target.

One physical source sample can feed several target wells; their stock draws
share a single inventory pool and the sum of draws may never exceed the
source's ``available_volume_ul``. Among all grid solutions that satisfy the
concentration tolerance of a well, the one closest to the ideal (exact)
stock draw is chosen; ties break toward the *smaller* stock draw, which is
the inventory-minimal choice and keeps the whole plan deterministic.

If any well cannot be satisfied (unknown source, over-capacity final
volume, concentration above stock, no grid solution, tolerance exceeded
after rounding, sub-minimum transfer) or any shared source runs out of
stock, the whole request fails with HTTP 422 listing every reason; no
partial plan is ever emitted.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .planner import InfeasibleArrangement, _resolve_dimensions
from .plates import parse_well, well_name
from .schemas import (
    VIOLATION_DILUTION_CONCENTRATION_UNACHIEVABLE,
    VIOLATION_DILUTION_ROUNDING_NO_GRID_SOLUTION,
    VIOLATION_DILUTION_ROUNDING_TOLERANCE_EXCEEDED,
    VIOLATION_DILUTION_SOURCE_STOCK_INSUFFICIENT,
    VIOLATION_DILUTION_SOURCE_UNKNOWN,
    VIOLATION_DILUTION_SOURCES_DUPLICATED,
    VIOLATION_DILUTION_TARGET_CAPACITY_EXCEEDED,
    VIOLATION_DILUTION_TARGET_WELLS_DUPLICATED,
    VIOLATION_WELL_REFERENCE_INVALID,
    DilutionPlanRequest,
)

# Floating-point slack: volumes/concentrations are compared with a small
# relative tolerance so decimal inputs (e.g. 0.3 resolution) round cleanly.
_EPS = 1e-9


class DilutionPlanInvalid(Exception):
    """No dilution plan can satisfy every target well (HTTP 422).

    Every offending source/well is listed; no partial plan is produced.
    """

    def __init__(self, categories: list[str], messages: list[str]):
        self.categories = categories
        self.messages = messages
        super().__init__("; ".join(messages) if messages else "invalid dilution plan request")


@dataclass(frozen=True)
class _WellChoice:
    """The resolved transfer pair for one target well."""

    stock_ul: float
    diluent_ul: float
    actual_concentration: float
    relative_error: float


def _clean_grid(value: float) -> float:
    """Round a value that is meant to be a whole number of µL-grid ticks."""
    return round(value, 9)


def _fmt_ul(v: float) -> float:
    """Stable presentation of a volume on the resolution grid."""
    return round(v, 6)


def _fmt_conc(v: float) -> float:
    """Stable presentation of a concentration/relative-error figure."""
    return round(v, 9)


def _grid_multiple(value: float, resolution: float) -> bool:
    """True when ``value`` is an integer multiple of ``resolution``."""
    n = value / resolution
    return abs(n - round(n)) <= _EPS * max(1.0, abs(n))


def _validate_layout(req: DilutionPlanRequest, rows: int, cols: int):
    """Resolve layout sample → well position; reject malformed/dup wells."""
    categories: list[str] = []
    messages: list[str] = []

    def add(category: str, msg: str) -> None:
        if category not in categories:
            categories.append(category)
        messages.append(msg)

    placement: dict[str, tuple[int, int]] = {}
    owners: dict[tuple[int, int], str] = {}
    for sid, raw in req.layout.items():
        try:
            pos = parse_well(raw, rows, cols)
        except ValueError as exc:
            add(VIOLATION_WELL_REFERENCE_INVALID, str(exc))
            continue
        if pos in owners:
            add(
                VIOLATION_DILUTION_TARGET_WELLS_DUPLICATED,
                f"layout well {well_name(*pos)} is assigned to both "
                f"'{owners[pos]}' and '{sid}'",
            )
            continue
        owners[pos] = sid
        placement[sid] = pos

    return categories, messages, placement


def _solve_well(stock_conc: float, final_v: float, target_conc: float,
                resolution: float, min_aspirate: float,
                tolerance: float) -> _WellChoice:
    """Pick the grid-aligned stock/diluent split for one well.

    Raises ``DilutionPlanInvalid`` (single-category, single-message) when no
    feasible split exists.
    """
    # Ideal, pre-rounding stock draw; concentration scales linearly with it.
    ideal = final_v * target_conc / stock_conc
    total_ticks = round(final_v / resolution)

    def make(tick: int) -> _WellChoice:
        v_stock = _clean_grid(tick * resolution)
        v_diluent = _clean_grid(final_v - v_stock)
        conc = stock_conc * v_stock / final_v
        rel_err = abs(conc - target_conc) / target_conc
        return _WellChoice(v_stock, v_diluent, conc, rel_err)

    # Enumerate every admissible stock tick. Bounds come from:
    #   1 ≤ stock_ticks ≤ total_ticks                 (stock draw strictly > 0)
    #   stock ≥ min_aspirate                          (when a floor is set)
    #   diluent = final - stock ≥ min_aspirate        (unless diluent is 0)
    # Pure stock (tick == total_ticks, zero diluent) is always admissible
    # regardless of the minimum aspirate of the omitted diluent transfer.
    min_ticks = 1
    if min_aspirate > 0:
        min_ticks = max(min_ticks, math.ceil(min_aspirate / resolution - _EPS))
    max_ticks_with_diluent = total_ticks - min_ticks

    allowed = set(range(min_ticks, max_ticks_with_diluent + 1))
    # Pure stock (zero diluent) is legal only when the stock draw itself
    # clears the minimum aspirate; the omitted diluent transfer has no floor.
    if total_ticks >= min_ticks:
        allowed.add(total_ticks)
    allowed = {t for t in allowed if 1 <= t <= total_ticks}

    if not allowed:
        raise DilutionPlanInvalid(
            [VIOLATION_DILUTION_ROUNDING_NO_GRID_SOLUTION],
            [
                f"no pipetting grid solution for final volume {_fmt_ul(final_v)} µL: "
                f"every non-zero transfer must be at least {_fmt_ul(min_aspirate)} µL "
                f"(resolution {_fmt_ul(resolution)} µL), so the well cannot hold both a "
                f"stock and a diluent transfer; undiluted stock also cannot reach "
                f"concentration {target_conc:g}"
            ],
        )

    feasible: list[tuple[int, _WellChoice]] = [
        (t, make(t)) for t in sorted(allowed)
    ]
    within = [(t, c) for t, c in feasible if c.relative_error <= tolerance + _EPS]
    if not within:
        _best_t, best = min(feasible, key=lambda tc: tc[1].relative_error)
        raise DilutionPlanInvalid(
            [VIOLATION_DILUTION_ROUNDING_TOLERANCE_EXCEEDED],
            [
                f"rounding to the {_fmt_ul(resolution)} µL grid cannot reach target "
                f"concentration {target_conc:g} within relative tolerance {tolerance:g}: "
                f"best achievable {_fmt_conc(best.actual_concentration):g} "
                f"(relative error {_fmt_conc(best.relative_error):g}, "
                f"stock {_fmt_ul(best.stock_ul)} µL + diluent "
                f"{_fmt_ul(best.diluent_ul)} µL)"
            ],
        )

    ideal_tick = ideal / resolution
    # Closest grid point to the ideal draw; exact midpoint ties take the
    # smaller draw (inventory-minimal, deterministic).
    best_t, best = min(
        within,
        key=lambda tc: (
            round(abs(tc[0] - ideal_tick), 9),
            tc[0],
        ),
    )
    return best


def dilution_plan(req: DilutionPlanRequest) -> dict:
    """Validate inputs, solve every well and emit the stepwise plan."""
    try:
        rows, cols = _resolve_dimensions(req.plate)
    except InfeasibleArrangement as exc:
        raise DilutionPlanInvalid(exc.categories, exc.messages) from exc

    categories: list[str] = []
    messages: list[str] = []

    def add(category: str, msg: str) -> None:
        if category not in categories:
            categories.append(category)
        messages.append(msg)

    # --- layout ------------------------------------------------------------
    layout_cats, layout_msgs, placement = _validate_layout(req, rows, cols)
    categories.extend(layout_cats)
    messages.extend(layout_msgs)
    well_owner = {pos: sid for sid, pos in placement.items()}

    # --- sources -----------------------------------------------------------
    sources: dict[str, tuple[float, float]] = {}
    for src in req.sources:
        if src.sample_id in sources:
            add(
                VIOLATION_DILUTION_SOURCES_DUPLICATED,
                f"source sample '{src.sample_id}' is listed more than once",
            )
            continue
        sources[src.sample_id] = (src.concentration, src.available_volume_ul)

    # --- targets: parse and order deterministically (row-major) -------------
    resolution = req.pipette.resolution_ul
    min_aspirate = req.pipette.min_aspirate_ul
    tolerance = req.concentration_tolerance

    parsed_targets: list[tuple[tuple[int, int], str, float, float]] = []
    seen_targets: set[tuple[int, int]] = set()
    for tgt in req.targets:
        try:
            pos = parse_well(tgt.well, rows, cols)
        except ValueError as exc:
            add(VIOLATION_WELL_REFERENCE_INVALID, str(exc))
            continue
        if pos in seen_targets:
            add(
                VIOLATION_DILUTION_TARGET_WELLS_DUPLICATED,
                f"target well {well_name(*pos)} appears more than once",
            )
            continue
        seen_targets.add(pos)
        occupant = well_owner.get(pos)
        if occupant is None:
            add(
                VIOLATION_WELL_REFERENCE_INVALID,
                f"target well {well_name(*pos)} is not occupied by the submitted layout",
            )
            continue
        # The feeding source defaults to the well's layout occupant but may
        # be overridden so one stock can feed a whole dilution gradient.
        owner = tgt.sample_id if tgt.sample_id is not None else occupant
        parsed_targets.append((pos, owner, tgt.target_concentration, tgt.final_volume_ul))

    parsed_targets.sort(key=lambda item: item[0])

    # --- per-well structural checks ----------------------------------------
    # Even when a well is individually unsolvable we keep scanning, so the
    # 422 response lists every failing well in one response.
    well_problems: list[tuple[str, str]] = []  # (category, message)
    choices: dict[tuple[int, int], tuple[str, _WellChoice]] = {}

    for pos, owner, target_conc, final_v in parsed_targets:
        label = f"well {well_name(*pos)} (sample '{owner}')"

        if owner not in sources:
            well_problems.append((
                VIOLATION_DILUTION_SOURCE_UNKNOWN,
                f"{label}: source sample '{owner}' has no concentration/volume entry "
                f"in sources",
            ))
            continue
        stock_conc, _avail = sources[owner]

        if final_v > req.well_capacity_ul + _EPS * max(1.0, req.well_capacity_ul):
            well_problems.append((
                VIOLATION_DILUTION_TARGET_CAPACITY_EXCEEDED,
                f"{label}: requested final volume {_fmt_ul(final_v)} µL exceeds well "
                f"capacity {_fmt_ul(req.well_capacity_ul)} µL",
            ))
            continue

        if not _grid_multiple(final_v, resolution):
            well_problems.append((
                VIOLATION_DILUTION_ROUNDING_NO_GRID_SOLUTION,
                f"{label}: final volume {_fmt_ul(final_v)} µL is not an integer "
                f"multiple of the pipette resolution {_fmt_ul(resolution)} µL",
            ))
            continue

        if target_conc > stock_conc * (1.0 + _EPS):
            well_problems.append((
                VIOLATION_DILUTION_CONCENTRATION_UNACHIEVABLE,
                f"{label}: target concentration {target_conc:g} exceeds the source "
                f"stock concentration {stock_conc:g}; dilution can only lower it",
            ))
            continue

        try:
            choice = _solve_well(
                stock_conc, final_v, target_conc, resolution, min_aspirate, tolerance
            )
        except DilutionPlanInvalid as exc:
            well_problems.append((exc.categories[0], f"{label}: {exc.messages[0]}"))
            continue
        choices[pos] = (owner, choice)

    for cat, msg in well_problems:
        add(cat, msg)

    # --- shared-inventory check -------------------------------------------
    # Runs after every well has been solved individually so stock totals are
    # meaningful; insufficient sources are still reported together with any
    # per-well problems above.
    draws: dict[str, float] = {}
    for pos, (owner, choice) in choices.items():
        draws[owner] = draws.get(owner, 0.0) + choice.stock_ul

    for sid in sorted(sources):
        total = draws.get(sid, 0.0)
        avail = sources[sid][1]
        if total > avail + _EPS * max(1.0, avail):
            add(
                VIOLATION_DILUTION_SOURCE_STOCK_INSUFFICIENT,
                f"source sample '{sid}': the {len([p for p, (o, _c) in choices.items() if o == sid])} "
                f"target well(s) fed from it require {_fmt_ul(total)} µL of stock but only "
                f"{_fmt_ul(avail)} µL is available (short by {_fmt_ul(total - avail)} µL)",
            )

    if categories:
        # Deterministic category order: first occurrence along a fixed
        # category ordering, messages in the order they were gathered.
        order = [
            VIOLATION_WELL_REFERENCE_INVALID,
            VIOLATION_DILUTION_TARGET_WELLS_DUPLICATED,
            VIOLATION_DILUTION_SOURCES_DUPLICATED,
            VIOLATION_DILUTION_SOURCE_UNKNOWN,
            VIOLATION_DILUTION_TARGET_CAPACITY_EXCEEDED,
            VIOLATION_DILUTION_CONCENTRATION_UNACHIEVABLE,
            VIOLATION_DILUTION_ROUNDING_NO_GRID_SOLUTION,
            VIOLATION_DILUTION_ROUNDING_TOLERANCE_EXCEEDED,
            VIOLATION_DILUTION_SOURCE_STOCK_INSUFFICIENT,
        ]
        ordered = [c for c in order if c in categories]
        raise DilutionPlanInvalid(ordered, messages)

    # --- emit deterministic instructions -----------------------------------
    instructions: list[dict] = []
    wells_out: list[dict] = []
    step = 0
    for pos, owner, target_conc, final_v in parsed_targets:
        _, choice = choices[pos]
        steps_here: list[int] = []
        if choice.diluent_ul > 0:
            step += 1
            steps_here.append(step)
            instructions.append({
                "step": step,
                "action": "DISPENSE_DILUENT",
                "well": well_name(*pos),
                "source_id": req.diluent.id,
                "volume_ul": _fmt_ul(choice.diluent_ul),
            })
        step += 1
        steps_here.append(step)
        instructions.append({
            "step": step,
            "action": "TRANSFER_STOCK",
            "well": well_name(*pos),
            "sample_id": owner,
            "source_id": owner,
            "volume_ul": _fmt_ul(choice.stock_ul),
        })
        wells_out.append({
            "well": well_name(*pos),
            "sample_id": owner,
            "layout_sample_id": well_owner[pos],
            "target_concentration": _fmt_conc(target_conc),
            "actual_concentration": _fmt_conc(choice.actual_concentration),
            "relative_concentration_error": _fmt_conc(choice.relative_error),
            "final_volume_ul": _fmt_ul(final_v),
            "stock_volume_ul": _fmt_ul(choice.stock_ul),
            "diluent_volume_ul": _fmt_ul(choice.diluent_ul),
            "within_tolerance": choice.relative_error <= tolerance + _EPS,
            "steps": steps_here,
        })

    sources_out = []
    for sid in sorted(sources):
        conc, avail = sources[sid]
        used = draws.get(sid, 0.0)
        fed_wells = sorted(
            (well_name(*pos) for pos, (owner, _c) in choices.items() if owner == sid),
            key=lambda name: parse_well(name, rows, cols),
        )
        sources_out.append({
            "sample_id": sid,
            "concentration": _fmt_conc(conc),
            "available_volume_ul": _fmt_ul(avail),
            "used_volume_ul": _fmt_ul(used),
            "remaining_volume_ul": _fmt_ul(avail - used),
            "feeds_wells": fed_wells,
        })

    return {
        "plate": {
            "type": req.plate.type,
            "rows": rows,
            "columns": cols,
        },
        "diluent": {"id": req.diluent.id},
        "pipette": {
            "resolution_ul": _fmt_ul(resolution),
            "min_aspirate_ul": _fmt_ul(min_aspirate),
        },
        "concentration_tolerance": _fmt_conc(tolerance),
        "well_capacity_ul": _fmt_ul(req.well_capacity_ul),
        "wells": wells_out,
        "sources": sources_out,
        "instructions": instructions,
        "verification": {
            "all_wells_within_tolerance": all(w["within_tolerance"] for w in wells_out),
            "all_volumes_on_resolution_grid": all(
                _grid_multiple(w["stock_volume_ul"], resolution)
                and (w["diluent_volume_ul"] == 0
                     or _grid_multiple(w["diluent_volume_ul"], resolution))
                for w in wells_out
            ),
            "all_final_volumes_within_capacity": all(
                w["final_volume_ul"]
                <= req.well_capacity_ul + _EPS * max(1.0, req.well_capacity_ul)
                for w in wells_out
            ),
            "all_sources_within_inventory": all(
                s["used_volume_ul"]
                <= s["available_volume_ul"] + _EPS * max(1.0, s["available_volume_ul"])
                for s in sources_out
            ),
        },
    }
