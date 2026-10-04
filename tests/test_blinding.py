"""Tests for two-party collaborative blinding/unblinding."""
from __future__ import annotations

import base64
import logging
import re

import pytest
from fastapi.testclient import TestClient

from app.blinding import _KEY_BYTES, _VERSION_BYTE
from app.main import app

client = TestClient(app)

BLIND_URL = "/api/v1/blinding/blind"
UNBLIND_URL = "/api/v1/blinding/unblind"

LAYOUT = {"S01": "A1", "S02": "B2", "S03": "C3", "S04": "D4", "S05": "A2"}
BATCH = "BATCH-2026-09-30"

BLIND_ID_RE = re.compile(r"^BL-[0-9a-f]{12}$")


def _blind_payload(layout=None, batch=BATCH, plate=None):
    payload = {"batch_id": batch, "layout": layout if layout is not None else LAYOUT}
    if plate is not None:
        payload["plate"] = plate
    return payload


def _do_blind(layout=None, batch=BATCH, plate=None):
    r = client.post(BLIND_URL, json=_blind_payload(layout, batch, plate))
    assert r.status_code == 200, r.text
    return r.json()


def _unblind_payload(body, *, view=None, shares=None, batch=None, plate=None,
                     package=None):
    return {
        "batch_id": batch if batch is not None else body["batch_id"],
        "plate": plate if plate is not None else {"type": body["plate"]["type"]},
        "view": view if view is not None else body["view"],
        "mapping_package": package if package is not None else body["mapping_package"],
        "key_shares": shares if shares is not None else [
            s["share"] for s in body["key_shares"]
        ],
    }


def _do_unblind(body, **overrides):
    return client.post(UNBLIND_URL, json=_unblind_payload(body, **overrides))


# --------------------------------------------------------------- blind: 200


def test_blind_ok_view_package_shares_shape():
    body = _do_blind()
    assert body["batch_id"] == BATCH
    assert body["plate"] == {"type": "96", "rows": 8, "columns": 12}

    # Operational view: well + blind_id only, never the sample id.
    view = body["view"]
    assert len(view) == len(LAYOUT)
    assert all(set(row) == {"well", "blind_id"} for row in view)
    for row in view:
        assert row["blind_id"] not in LAYOUT
        assert BLIND_ID_RE.match(row["blind_id"])
    assert len({r["blind_id"] for r in view}) == len(view)
    serialized = str(view)
    for sid in LAYOUT:
        assert sid not in serialized

    # Row-major ordering.
    wells = [r["well"] for r in view]
    assert wells == sorted(wells, key=lambda w: (ord(w[0]) - 65, int(w[1:])))

    # Package framing.
    blob = base64.urlsafe_b64decode(body["mapping_package"])
    assert blob[:1] == _VERSION_BYTE
    assert len(blob) >= 1 + 12 + 16

    # Two distinct, well-formed shares.
    assert [s["holder"] for s in body["key_shares"]] == [1, 2]
    shares = [base64.urlsafe_b64decode(s["share"]) for s in body["key_shares"]]
    assert all(len(s) == _KEY_BYTES for s in shares)
    assert shares[0] != shares[1]


def test_blind_ids_are_unpredictable_and_batch_unique():
    first = _do_blind()
    second = _do_blind()
    a = {r["blind_id"] for r in first["view"]}
    b = {r["blind_id"] for r in second["view"]}
    # Random blind ids: two blinding runs should not coincide.
    assert a.isdisjoint(b)
    assert a.isdisjoint(LAYOUT.keys())


def test_package_ciphertext_does_not_expose_sample_ids():
    body = _do_blind()
    blob = base64.urlsafe_b64decode(body["mapping_package"])
    for sid in LAYOUT:
        assert sid.encode() not in blob


def test_blind_custom_plate():
    body = _do_blind(plate={"type": "custom", "rows": 3, "columns": 3},
                     layout={"X1": "A1", "X2": "C3"})
    assert body["plate"] == {"type": "custom", "rows": 3, "columns": 3}
    assert len(body["view"]) == 2


# ------------------------------------------------------------- blind: 422


@pytest.mark.parametrize(
    "plate,layout,category",
    [
        ({"type": "96"}, {"S1": "Z9", "S2": "A1"}, "WELL_REFERENCE_INVALID"),
        ({"type": "96"}, {"S1": "A13"}, "WELL_REFERENCE_INVALID"),
        ({"type": "96"}, {"S1": "not-a-well"}, "WELL_REFERENCE_INVALID"),
        ({"type": "96"}, {"S1": "A1", "S2": "a1"}, "BLIND_WELLS_DUPLICATED"),
        ({"type": "96"}, {"S1": "A1", "S2": "A1"}, "BLIND_WELLS_DUPLICATED"),
        ({"type": "24"}, {"S1": "D6", "S2": "A7"}, "WELL_REFERENCE_INVALID"),
    ],
)
def test_blind_invalid_layout_422_and_no_artifacts(plate, layout, category):
    r = client.post(BLIND_URL, json=_blind_payload(layout, plate=plate))
    assert r.status_code == 422, r.text
    body = r.json()
    assert category in body["infeasibility_categories"]
    # Nothing secret is ever produced on failure.
    assert "view" not in body
    assert "mapping_package" not in body
    assert "key_shares" not in body


def test_blind_empty_layout_422():
    r = client.post(BLIND_URL, json={"batch_id": BATCH, "layout": {}})
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == ["REQUEST_VALIDATION_ERROR"]


def test_blind_missing_field_is_422():
    r = client.post(BLIND_URL, json={"layout": LAYOUT})
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == ["REQUEST_VALIDATION_ERROR"]


def test_blind_extra_field_rejected():
    payload = _blind_payload()
    payload["unexpected"] = 1
    assert client.post(BLIND_URL, json=payload).status_code == 422


# ------------------------------------------------------------- unblind: 200


def test_unblind_roundtrip_returns_full_correspondence():
    body = _do_blind()
    r = _do_unblind(body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["batch_id"] == BATCH
    revealed = out["revealed"]
    assert len(revealed) == len(LAYOUT)
    assert {row["sample_id"] for row in revealed} == set(LAYOUT)
    by_sample = {row["sample_id"]: row for row in revealed}
    for sid, well in LAYOUT.items():
        row = by_sample[sid]
        assert row["well"] == well.upper()
        assert row["blind_id"] == next(v["blind_id"] for v in body["view"] if v["well"] == well.upper())
        assert set(row) == {"blind_id", "sample_id", "well"}


def test_unblind_is_order_insensitive_for_view_and_shares():
    body = _do_blind()
    view = list(reversed(body["view"]))
    shares = [s["share"] for s in reversed(body["key_shares"])]
    r = _do_unblind(body, view=view, shares=shares)
    assert r.status_code == 200, r.text
    assert len(r.json()["revealed"]) == len(LAYOUT)


def test_unblind_accepts_lowercase_wells_in_view():
    body = _do_blind()
    view = [{"well": v["well"].lower(), "blind_id": v["blind_id"]} for v in body["view"]]
    r = _do_unblind(body, view=view)
    assert r.status_code == 200, r.text


# ------------------------------------------------------------- unblind: 422


def _assert_422_no_mapping(r):
    assert r.status_code == 422, r.text
    body = r.json()
    assert "revealed" not in body
    for sid in LAYOUT:
        assert sid not in r.text
    return body


def test_unblind_wrong_batch_422():
    body = _do_blind()
    r = _do_unblind(body, batch="OTHER-BATCH")
    out = _assert_422_no_mapping(r)
    assert "BLIND_INTEGRITY_FAILED" in out["infeasibility_categories"]


def test_unblind_wrong_plate_422():
    body = _do_blind()
    r = _do_unblind(body, plate={"type": "24"})
    out = _assert_422_no_mapping(r)
    assert "BLIND_INTEGRITY_FAILED" in out["infeasibility_categories"]


def test_unblind_tampered_blind_id_422():
    body = _do_blind()
    view = [dict(v) for v in body["view"]]
    view[0]["blind_id"] = "BL-deadbeefdead"
    r = _do_unblind(body, view=view)
    out = _assert_422_no_mapping(r)
    assert "BLIND_INTEGRITY_FAILED" in out["infeasibility_categories"]


def test_unblind_tampered_well_422():
    body = _do_blind()
    view = [dict(v) for v in body["view"]]
    view[0]["well"] = "H12"
    r = _do_unblind(body, view=view)
    _assert_422_no_mapping(r)


def test_unblind_truncated_view_422():
    body = _do_blind()
    r = _do_unblind(body, view=body["view"][:-1])
    out = _assert_422_no_mapping(r)
    assert "BLIND_INTEGRITY_FAILED" in out["infeasibility_categories"]


def test_unblind_view_with_extra_entry_422():
    body = _do_blind()
    view = body["view"] + [{"well": "H11", "blind_id": "BL-aaaaaaaaaaaa"}]
    r = _do_unblind(body, view=view)
    _assert_422_no_mapping(r)


def test_unblind_tampered_package_422():
    body = _do_blind()
    blob = bytearray(base64.urlsafe_b64decode(body["mapping_package"]))
    blob[-1] ^= 0x01  # flip a GCM tag bit
    package = base64.urlsafe_b64encode(bytes(blob)).decode()
    r = _do_unblind(body, package=package)
    out = _assert_422_no_mapping(r)
    assert "BLIND_INTEGRITY_FAILED" in out["infeasibility_categories"]


def test_unblind_garbage_package_422():
    body = _do_blind()
    r = _do_unblind(body, package="not-base64-or-a-package!!!")
    out = _assert_422_no_mapping(r)
    assert "BLIND_PACKAGE_INVALID" in out["infeasibility_categories"]


def test_unblind_equal_shares_422():
    body = _do_blind()
    s = body["key_shares"][0]["share"]
    r = _do_unblind(body, shares=[s, s])
    out = _assert_422_no_mapping(r)
    assert "BLIND_SHARE_INVALID" in out["infeasibility_categories"]


def test_unblind_single_share_plus_garbage_cannot_open():
    body = _do_blind()
    # Holder 1's share on its own, paired with an unrelated random share,
    # reconstructs the wrong key: GCM must reject it.
    other = base64.urlsafe_b64encode(b"\x00" * _KEY_BYTES).decode()
    r = _do_unblind(body, shares=[body["key_shares"][0]["share"], other])
    out = _assert_422_no_mapping(r)
    assert "BLIND_INTEGRITY_FAILED" in out["infeasibility_categories"]


def test_unblind_missing_share_422():
    body = _do_blind()
    payload = _unblind_payload(body, shares=[body["key_shares"][0]["share"]])
    r = client.post(UNBLIND_URL, json=payload)
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == ["REQUEST_VALIDATION_ERROR"]


def test_unblind_share_from_another_batch_422():
    body_a = _do_blind(batch="BATCH-A", layout={"S1": "A1", "S2": "A2"})
    body_b = _do_blind(batch="BATCH-B", layout={"S1": "A1", "S2": "A2"})
    mixed = [body_a["key_shares"][0]["share"], body_b["key_shares"][1]["share"]]
    r = _do_unblind(body_a, shares=mixed)
    _assert_422_no_mapping(r)


def test_unblind_cross_package_view_422():
    body_a = _do_blind(batch="BATCH-A", layout={"S1": "A1", "S2": "A2"})
    body_b = _do_blind(batch="BATCH-B", layout={"S1": "A1", "S2": "A2"})
    # Package from A, everything else from B: no mapping may surface.
    r = client.post(UNBLIND_URL, json=_unblind_payload(
        body_b, package=body_a["mapping_package"]))
    _assert_422_no_mapping(r)


def test_unblind_duplicate_blind_id_in_view_422():
    body = _do_blind()
    view = [dict(v) for v in body["view"]]
    view[1]["blind_id"] = view[0]["blind_id"]
    r = _do_unblind(body, view=view)
    assert r.status_code == 422
    assert "BLIND_VIEW_INVALID" in r.json()["infeasibility_categories"]


def test_error_responses_never_echo_shares_or_package():
    body = _do_blind()
    r = _do_unblind(body, batch="OTHER-BATCH")
    assert r.status_code == 422
    for entry in body["key_shares"]:
        assert entry["share"] not in r.text
    assert body["mapping_package"] not in r.text


def test_no_secrets_in_logs(caplog):
    payload = _blind_payload()
    with caplog.at_level(logging.DEBUG, logger="app"):
        r = client.post(BLIND_URL, json=payload)
    assert r.status_code == 200
    body = r.json()
    logged = " ".join(rec.getMessage() for rec in caplog.records)
    for sid in LAYOUT:
        assert sid not in logged
    assert body["mapping_package"] not in logged
    for entry in body["key_shares"]:
        assert entry["share"] not in logged
