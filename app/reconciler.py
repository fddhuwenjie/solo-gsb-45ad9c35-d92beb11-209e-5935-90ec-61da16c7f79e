"""Deterministic minimal-move reconciliation of a scanned plate.

Given a target layout (sample → well, as produced by the arrangement
endpoint) and a full scan of the physical plate (well → sample, empty wells
omitted), this module first cross-checks the two mappings and then emits a
minimal, deterministic sequence of single-sample moves that brings the plate
to the target state.

Move model:

* every step moves exactly one sample into a well that is empty at that
  moment (never a swap of two occupied wells);
* misplacements decompose into *open chains* — a sample sitting on a
  non-target well whose relocation cascade ends at an empty target well —
  and *closed loops* (cycles) where every well of the loop is occupied;
* chains are resolved first (空孔链先行), each sample moving directly into
  its final target well, so a chain of k samples costs exactly k moves;
* a cycle of k samples needs one extra move: one sample is parked on the
  request-supplied staging well, the remaining k-1 samples rotate into
  place, and the parked sample finally moves from the staging well into its
  target, leaving the staging well empty — k+1 moves in total.

The total move count ``misplaced_samples + number_of_cycles`` is provably
minimal: every misplaced sample must move at least once, and a cycle has no
empty well, so at least one of its samples must move twice (out to the
staging well and back in).
"""
from __future__ import annotations

from .planner import InfeasibleArrangement, _resolve_dimensions
from .plates import parse_well, well_name
from .schemas import (
    VIOLATION_BLOCKED_WELL_OCCUPIED,
    VIOLATION_SAMPLES_MISSING,
    VIOLATION_SCAN_SAMPLES_DUPLICATED,
    VIOLATION_SCAN_WELLS_DUPLICATED,
    VIOLATION_TARGET_ON_BLOCKED,
    VIOLATION_TARGET_WELLS_DUPLICATED,
    VIOLATION_UNPLANNED_SAMPLES,
    VIOLATION_WELL_REFERENCE_INVALID,
    ReconcileRequest,
)


class ReconcileInvalid(Exception):
    """The target/scan/blocked inputs are mutually inconsistent (HTTP 422).

    No move instructions are generated in this case.
    """

    def __init__(self, categories: list[str], messages: list[str]):
        self.categories = categories
        self.messages = messages
        super().__init__("; ".join(messages) if messages else "invalid reconciliation request")


class StagingConflict(Exception):
    """Closed loops exist but no usable staging well was supplied (HTTP 409).

    ``cycles`` lists the offending loops (well names in dependency order);
    no partial instruction list is produced.
    """

    def __init__(self, cycles: list[list[str]], problems: list[str], staging_well):
        self.cycles = cycles
        self.problems = problems
        self.staging_well = staging_well
        super().__init__("closed-loop misplacements require a usable staging well")


def _preflight(req: ReconcileRequest, rows: int, cols: int):
    """Cross-check target, scan and blocked wells before any planning."""
    categories: list[str] = []
    messages: list[str] = []

    def add(category: str, msg: str) -> None:
        if category not in categories:
            categories.append(category)
        messages.append(msg)

    # --- blocked wells -----------------------------------------------------
    blocked: set[tuple[int, int]] = set()
    for raw in req.blocked_wells:
        try:
            blocked.add(parse_well(raw, rows, cols))
        except ValueError as exc:
            add(VIOLATION_WELL_REFERENCE_INVALID, str(exc))

    # --- target mapping (sample -> well) -----------------------------------
    target_pos: dict[str, tuple[int, int]] = {}
    target_well_owner: dict[tuple[int, int], str] = {}
    for sid, raw in req.target.items():
        try:
            pos = parse_well(raw, rows, cols)
        except ValueError as exc:
            add(VIOLATION_WELL_REFERENCE_INVALID, str(exc))
            continue
        if pos in target_well_owner:
            add(
                VIOLATION_TARGET_WELLS_DUPLICATED,
                f"target well {well_name(*pos)} assigned to both "
                f"'{target_well_owner[pos]}' and '{sid}'",
            )
        else:
            target_well_owner[pos] = sid
        target_pos[sid] = pos
        if pos in blocked:
            add(
                VIOLATION_TARGET_ON_BLOCKED,
                f"sample '{sid}' targets blocked well {well_name(*pos)}",
            )

    # --- scanned mapping (well -> sample) ----------------------------------
    scan_pos: dict[tuple[int, int], str] = {}
    scan_samples: list[str] = []
    for raw, sid in req.scan.items():
        try:
            pos = parse_well(raw, rows, cols)
        except ValueError as exc:
            add(VIOLATION_WELL_REFERENCE_INVALID, str(exc))
            continue
        # Distinct raw keys may normalize to the same well ("a1" vs "A1").
        if pos in scan_pos:
            add(
                VIOLATION_SCAN_WELLS_DUPLICATED,
                f"well {well_name(*pos)} appears more than once in the scan "
                f"(samples '{scan_pos[pos]}' and '{sid}')",
            )
        else:
            scan_pos[pos] = sid
        scan_samples.append(sid)
        if pos in blocked:
            add(
                VIOLATION_BLOCKED_WELL_OCCUPIED,
                f"scan places sample '{sid}' on blocked well {well_name(*pos)}",
            )

    dupes = sorted({s for s in scan_samples if scan_samples.count(s) > 1})
    if dupes:
        add(
            VIOLATION_SCAN_SAMPLES_DUPLICATED,
            f"sample(s) scanned at more than one well: {', '.join(dupes)}",
        )

    # --- sample set agreement ----------------------------------------------
    target_ids = set(req.target)
    scanned_ids = set(scan_samples)
    missing = sorted(target_ids - scanned_ids)
    if missing:
        add(
            VIOLATION_SAMPLES_MISSING,
            f"planned sample(s) not found in scan: {', '.join(missing)}",
        )
    unplanned = sorted(scanned_ids - target_ids)
    if unplanned:
        add(
            VIOLATION_UNPLANNED_SAMPLES,
            f"scanned sample(s) not in the plan: {', '.join(unplanned)}",
        )

    if categories:
        raise ReconcileInvalid(categories, messages)

    return blocked, target_pos, scan_pos


def _decompose(target_pos: dict[str, tuple[int, int]],
               scan_pos: dict[tuple[int, int], str]):
    """Split misplacements into open chains and closed loops.

    Builds the dependency graph whose edges point from a wrongly occupied
    well to the target well of its current occupant. Every well has
    in-degree and out-degree at most one, so the graph is a disjoint union
    of paths (starting at an occupied non-target well, ending at an empty
    target well) and cycles (every well occupied).
    """
    need = {pos: sid for sid, pos in target_pos.items()}
    succ: dict[tuple[int, int], tuple[int, int]] = {}
    for pos, sid in scan_pos.items():
        if need.get(pos) != sid:
            succ[pos] = target_pos[sid]

    indeg: dict[tuple[int, int], int] = {}
    for dst in succ.values():
        indeg[dst] = indeg.get(dst, 0) + 1

    visited: set[tuple[int, int]] = set()
    chains: list[list[tuple[int, int]]] = []
    for start in sorted(w for w in succ if indeg.get(w, 0) == 0):
        path = []
        node = start
        while node in succ:
            path.append(node)
            visited.add(node)
            node = succ[node]
        path.append(node)  # terminal empty target well
        chains.append(path)

    cycles: list[list[tuple[int, int]]] = []
    seen: set[tuple[int, int]] = set()
    for start in sorted(w for w in succ if w not in visited):
        if start in seen:
            continue
        cyc = []
        node = start
        while node not in seen:
            seen.add(node)
            cyc.append(node)
            node = succ[node]
        # Rotate so the smallest well (row-major) leads: deterministic output.
        i = cyc.index(min(cyc))
        cycles.append(cyc[i:] + cyc[:i])

    return need, chains, cycles


def _check_staging(req: ReconcileRequest, rows: int, cols: int,
                   blocked: set[tuple[int, int]],
                   need: dict[tuple[int, int], str],
                   scan_pos: dict[tuple[int, int], str],
                   cycles: list[list[tuple[int, int]]]):
    """Validate the staging well; 409 when loops need it but it is unusable."""
    if not cycles:
        return None
    problems: list[str] = []
    staging_pos = None
    if req.staging_well is None:
        problems.append("no staging well provided")
    else:
        try:
            staging_pos = parse_well(req.staging_well, rows, cols)
        except ValueError as exc:
            problems.append(str(exc))
        else:
            if staging_pos in blocked:
                problems.append(f"staging well {well_name(*staging_pos)} is blocked")
            if staging_pos in need:
                problems.append(f"staging well {well_name(*staging_pos)} is a target well")
            if staging_pos in scan_pos:
                problems.append(
                    f"staging well {well_name(*staging_pos)} is occupied by "
                    f"sample '{scan_pos[staging_pos]}'"
                )
    if problems:
        raise StagingConflict(
            cycles=[[well_name(*w) for w in cyc] for cyc in cycles],
            problems=problems,
            staging_well=req.staging_well,
        )
    return staging_pos


def _plan_moves(scan_pos: dict[tuple[int, int], str],
                chains: list[list[tuple[int, int]]],
                cycles: list[list[tuple[int, int]]],
                staging_pos) -> list[tuple[str, tuple[int, int], tuple[int, int]]]:
    """Emit the minimal (sample, from, to) move sequence.

    Chains first: walking each chain backwards moves every sample straight
    into its (already emptied) final target well. Then each cycle is broken
    via the staging well: park the first sample, rotate the rest into place,
    park back into the freed target well.
    """
    moves: list[tuple[str, tuple[int, int], tuple[int, int]]] = []

    for path in chains:
        # path = [w0, ..., wk]; wk is the empty terminal target well.
        for i in range(len(path) - 2, -1, -1):
            moves.append((scan_pos[path[i]], path[i], path[i + 1]))

    for cyc in cycles:
        k = len(cyc)
        parked = scan_pos[cyc[0]]
        moves.append((parked, cyc[0], staging_pos))
        for i in range(k - 1, 0, -1):
            moves.append((scan_pos[cyc[i]], cyc[i], cyc[(i + 1) % k]))
        moves.append((parked, staging_pos, cyc[1 % k]))

    return moves


def _replay(scan_pos: dict[tuple[int, int], str],
            moves: list[tuple[str, tuple[int, int], tuple[int, int]]]):
    """Replay moves on the scanned state; verify the empty-destination rule."""
    state = dict(scan_pos)
    every_destination_empty = True
    for sid, frm, to in moves:
        if state.get(frm) != sid or to in state:
            every_destination_empty = False
        state.pop(frm, None)
        state[to] = sid
    return state, every_destination_empty


def reconcile(req: ReconcileRequest) -> dict:
    """Validate the request and produce the minimal correction plan."""
    try:
        rows, cols = _resolve_dimensions(req.plate)
    except InfeasibleArrangement as exc:
        raise ReconcileInvalid(exc.categories, exc.messages) from exc

    blocked, target_pos, scan_pos = _preflight(req, rows, cols)
    need, chains, cycles = _decompose(target_pos, scan_pos)
    staging_pos = _check_staging(req, rows, cols, blocked, need, scan_pos, cycles)
    moves = _plan_moves(scan_pos, chains, cycles, staging_pos)

    # Misplaced-well list: wells whose observed content differs from the
    # required final content (empty target wells and wrongly occupied wells).
    misplaced = []
    for pos in sorted(set(need) | set(scan_pos)):
        expected, actual = need.get(pos), scan_pos.get(pos)
        if expected != actual:
            misplaced.append(
                {"well": well_name(*pos), "expected": expected, "actual": actual}
            )

    final_state, destinations_clear = _replay(scan_pos, moves)
    final_mapping = {sid: well_name(*pos) for pos, sid in final_state.items()}
    target_mapping = {sid: well_name(*pos) for sid, pos in target_pos.items()}

    return {
        "plate": {
            "type": req.plate.type,
            "rows": rows,
            "columns": cols,
            "blocked_wells": [well_name(*w) for w in sorted(blocked)],
        },
        "misplaced_wells": misplaced,
        "move_count": len(moves),
        "cycles": [[well_name(*w) for w in cyc] for cyc in cycles],
        "staging_well": well_name(*staging_pos) if staging_pos is not None else None,
        "instructions": [
            {
                "step": i + 1,
                "sample_id": sid,
                "from": well_name(*frm),
                "to": well_name(*to),
            }
            for i, (sid, frm, to) in enumerate(moves)
        ],
        "final_mapping": {sid: final_mapping[sid] for sid in sorted(final_mapping)},
        "verification": {
            "final_matches_target": final_mapping == target_mapping,
            "every_destination_empty_at_move": destinations_clear,
            "staging_well_restored_empty": staging_pos is None or staging_pos not in final_state,
        },
    }
