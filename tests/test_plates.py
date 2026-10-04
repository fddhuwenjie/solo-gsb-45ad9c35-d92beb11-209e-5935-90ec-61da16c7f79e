"""Unit tests for well-name parsing and plate presets."""
import pytest

from app.plates import parse_well, well_name, PLATE_PRESETS


def test_well_name_roundtrip():
    for r in range(8):
        for c in range(12):
            assert parse_well(well_name(r, c), 8, 12) == (r, c)


def test_parse_well_case_insensitive():
    assert parse_well("c5", 8, 12) == (2, 4)
    assert parse_well(" A1 ", 8, 12) == (0, 0)


def test_parse_well_invalid():
    with pytest.raises(ValueError):
        parse_well("Z1", 8, 12)
    with pytest.raises(ValueError):
        parse_well("A13", 8, 12)
    with pytest.raises(ValueError):
        parse_well("1A", 8, 12)
    with pytest.raises(ValueError):
        parse_well("", 8, 12)


def test_presets():
    assert PLATE_PRESETS["96"] == (8, 12)
    assert PLATE_PRESETS["384"] == (16, 24)
