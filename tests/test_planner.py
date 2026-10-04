"""Planner tests: constraints, lexicographic metrics, determinism, 422s."""
import pytest

from app.metrics import compute_spreads, count_adjacent_pairs, verify_layout
from app.planner import InfeasibleArrangement, plan_layout
from app.schemas import ArrangeRequest

SMALL = {
    "plate": {"type": "96"},
    "samples": [{"id": f"s{i}", "group": f"G{i % 3}", "batch": f"B{i % 4}"} for i in range(20)],
    "fixed": [{"sample_id": "s0", "well": "C5"}, {"sample_id": "s7", "well": "H12"}],
    "blocked_wells": ["A1", "B2"],
    "seed": 42,
}


def _metrics(req, layout):
    spreads = compute_spreads(layout, layout.rows, layout.columns)
    d = max([s.row_spread for s in spreads] + [s.col_spread for s in spreads])
    pairs, _ = count_adjacent_pairs(req, layout.placement, layout.rows, layout.columns)
    return d, len(pairs)


def test_basic_layout_valid():
    req = ArrangeRequest(**SMALL)
    layout = plan_layout(req)
    assert len(layout.placement) == 20
    v = verify_layout(req, layout, layout.rows, layout.columns)
    assert v["all_passed"], v


def test_fixed_and_blocked_respected():
    req = ArrangeRequest(**SMALL)
    layout = plan_layout(req)
    assert layout.placement["s0"] == "C5"
    assert layout.placement["s7"] == "H12"
    assert "A1" not in layout.placement.values()
    assert "B2" not in layout.placement.values()


def test_determinism_same_seed():
    req = ArrangeRequest(**SMALL)
    l1 = plan_layout(req)
    l2 = plan_layout(ArrangeRequest(**SMALL))
    assert l1.placement == l2.placement


def test_seed_changes_layout():
    req = ArrangeRequest(**SMALL)
    l1 = plan_layout(req)
    other = dict(SMALL, seed=43)
    l2 = plan_layout(ArrangeRequest(**other))
    # Not a strict guarantee for every seed, but 42 vs 43 must differ here.
    assert l1.placement != l2.placement


def test_lexicographic_balance_quality():
    # 20 samples, 3 groups on 8x12: spreads must be tight (D == 1).
    req = ArrangeRequest(**SMALL)
    layout = plan_layout(req)
    d, _ = _metrics(req, layout)
    assert d <= 1


def test_full_plate_single_batch_adjacency():
    # A full plate of one batch: every orthogonal edge is a same-batch pair.
    req = ArrangeRequest(
        plate={"type": "24"},
        samples=[{"id": f"x{i}", "group": "G", "batch": "B"} for i in range(24)],
        seed=1,
    )
    layout = plan_layout(req)
    _, a = _metrics(req, layout)
    assert a == 4 * 5 + 3 * 6  # rows*(cols-1) + (rows-1)*cols


def test_custom_plate():
    req = ArrangeRequest(
        plate={"type": "custom", "rows": 3, "columns": 5},
        samples=[{"id": f"s{i}", "group": f"G{i % 2}", "batch": "B"} for i in range(10)],
        seed=5,
    )
    layout = plan_layout(req)
    assert (layout.rows, layout.columns) == (3, 5)
    assert verify_layout(req, layout, 3, 5)["all_passed"]


@pytest.mark.parametrize(
    "payload,category",
    [
        (
            {"plate": {"type": "24"},
             "samples": [{"id": f"x{i}", "group": "G", "batch": "B"} for i in range(25)],
             "seed": 1},
            "CAPACITY_EXCEEDED",
        ),
        (
            {"plate": {"type": "24"},
             "samples": [{"id": "x", "group": "G", "batch": "B"}],
             "fixed": [{"sample_id": "x", "well": "A1"}],
             "blocked_wells": ["A1"], "seed": 1},
            "FIXED_ON_BLOCKED",
        ),
        (
            {"plate": {"type": "24"},
             "samples": [{"id": "x", "group": "G", "batch": "B"},
                         {"id": "y", "group": "G", "batch": "B"}],
             "fixed": [{"sample_id": "x", "well": "A1"},
                       {"sample_id": "y", "well": "A1"}], "seed": 1},
            "FIXED_CONFLICT",
        ),
        (
            {"plate": {"type": "24"},
             "samples": [{"id": "x", "group": "G", "batch": "B"},
                         {"id": "x", "group": "G", "batch": "B"}], "seed": 1},
            "SAMPLE_IDS_DUPLICATED",
        ),
        (
            {"plate": {"type": "24"},
             "samples": [{"id": "x", "group": "G", "batch": "B"}],
             "fixed": [{"sample_id": "ghost", "well": "A1"}], "seed": 1},
            "WELL_REFERENCE_INVALID",
        ),
        (
            {"plate": {"type": "24"},
             "samples": [{"id": "x", "group": "G", "batch": "B"}],
             "blocked_wells": ["Z99"], "seed": 1},
            "WELL_REFERENCE_INVALID",
        ),
        (
            {"plate": {"type": "custom"},
             "samples": [{"id": "x", "group": "G", "batch": "B"}], "seed": 1},
            "PLATE_DIMENSION_INVALID",
        ),
    ],
)
def test_infeasible_categories(payload, category):
    with pytest.raises(InfeasibleArrangement) as exc:
        plan_layout(ArrangeRequest(**payload))
    assert category in exc.value.categories
