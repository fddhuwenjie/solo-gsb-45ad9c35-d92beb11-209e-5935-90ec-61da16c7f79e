"""Two-party collaborative blinding of existing arrangement results.

The service turns a validated sample → well layout (as produced by
``POST /api/v1/arrangements``) into:

* an **operational view** that lists only ``well`` and a random
  ``blind_id`` — the original sample ids never appear there;
* a **mapping package** — the full blind_id ↔ sample_id ↔ well
  correspondence encrypted with authenticated encryption (AES-256-GCM),
  whose AAD binds the batch id, the plate and the *complete* operational
  view, so the package cannot be re-associated with another batch, plate or
  a truncated/modified view;
* **two independent key shares** — the random 256-bit content key is
  split with a one-time-pad (XOR) split: ``share_2 = key XOR share_1``.
  Each share on its own is uniform and independent of the key, so a single
  holder can neither decrypt the package nor recover any correspondence.

Unblinding is all-or-nothing: batch id, the original complete operational
view, the mapping package and *both, distinct* shares must be submitted
together. Authenticated decryption fails (422) on any tampering, share or
batch mismatch, and no partial mapping is ever produced. Plaintext shares,
keys and mappings are never logged or echoed in error messages.
"""
from __future__ import annotations

import base64
import json
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .planner import InfeasibleArrangement, _resolve_dimensions
from .plates import parse_well, well_name
from .schemas import (
    VIOLATION_BLIND_INTEGRITY_FAILED,
    VIOLATION_BLIND_PACKAGE_INVALID,
    VIOLATION_BLIND_SHARE_INVALID,
    VIOLATION_BLIND_SAMPLE_WELL_MISMATCH,
    VIOLATION_BLIND_VIEW_INVALID,
    VIOLATION_BLIND_VIEW_INTEGRITY_FAILED,
    VIOLATION_BLIND_WELLS_DUPLICATED,
    VIOLATION_PLATE_DIMENSION_INVALID,
    VIOLATION_WELL_REFERENCE_INVALID,
    BlindRequest,
    UnblindRequest,
)

# --- package format -------------------------------------------------------
# urlsafe-base64( VERSION_BYTE || nonce(12) || ciphertext || gcm_tag(16) )
_VERSION = 1
_VERSION_BYTE = bytes([_VERSION])
_NONCE_BYTES = 12
_KEY_BYTES = 32  # AES-256

_BLIND_PREFIX = "BL-"
_BLIND_RANDOM_BYTES = 6  # 48 bits, uniqueness re-checked inside the batch


class BlindInvalid(Exception):
    """A blinding/unblinding request is invalid (HTTP 422).

    On failure no blind codes, mapping package, shares or partial
    correspondence are produced. ``messages`` must never contain
    plaintext shares, keys or mappings.
    """

    def __init__(self, categories: list[str], messages: list[str]):
        self.categories = categories
        self.messages = messages
        super().__init__("; ".join(messages) if messages else "invalid blinding request")


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    """Decode urlsafe base64; raises on characters outside the alphabet."""
    return base64.urlsafe_b64decode(text.encode("ascii"))


def _canonical_json(obj) -> bytes:
    """Deterministic JSON encoding used identically for seal and open."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _plate_descriptor(req_plate, rows: int, cols: int) -> dict:
    return {"type": req_plate.type, "rows": rows, "columns": cols}


def _aad(batch_id: str, plate: dict, view: list[dict]) -> bytes:
    # The AAD binds the batch, the plate footprint and the COMPLETE
    # operational view (well + blind_id). GCM rejects any mismatch.
    return _canonical_json(
        {"batch_id": batch_id, "plate": plate, "view": view}
    )


# ---------------------------------------------------------------- blinding


def _validate_layout(req: BlindRequest, rows: int, cols: int) -> list[tuple[str, int, int]]:
    """Validate one-to-one sample ↔ legal-well correspondence.

    Returns entries ``(sample_id, row, col)`` in row-major well order.
    Raises BlindInvalid on any structural problem; nothing secret exists
    yet at this stage, so offending public identifiers may appear in
    messages.
    """
    categories: list[str] = []
    messages: list[str] = []

    seen_well: dict[tuple[int, int], str] = {}
    entries: list[tuple[str, int, int]] = []
    for sid, raw_well in req.layout.items():
        if not sid.strip():
            categories.append(VIOLATION_BLIND_SAMPLE_WELL_MISMATCH)
            messages.append("layout contains an empty sample id")
            continue
        try:
            pos = parse_well(raw_well, rows, cols)
        except ValueError as exc:
            if VIOLATION_WELL_REFERENCE_INVALID not in categories:
                categories.append(VIOLATION_WELL_REFERENCE_INVALID)
            messages.append(str(exc))
            continue
        if pos in seen_well:
            if VIOLATION_BLIND_WELLS_DUPLICATED not in categories:
                categories.append(VIOLATION_BLIND_WELLS_DUPLICATED)
            messages.append(
                f"well {well_name(*pos)} assigned to both "
                f"'{seen_well[pos]}' and '{sid}'"
            )
            continue
        seen_well[pos] = sid
        entries.append((sid, pos[0], pos[1]))

    # One sample → exactly one well and one well → at most one sample:
    # dict keys give one well per sample; the duplicate-well check above
    # guarantees at most one sample per well.
    if categories:
        raise BlindInvalid(categories, messages)

    return sorted(entries, key=lambda e: (e[1], e[2]))


def _generate_blind_ids(sample_ids: list[str]) -> dict[str, str]:
    """Assign each sample an unpredictable, batch-unique blind id.

    Generated with ``secrets`` (CSPRNG); the value carries no derivable
    relationship to the original sample id.
    """
    taken: set[str] = set(sample_ids)
    assignment: dict[str, str] = {}
    for sid in sample_ids:
        bid = f"{_BLIND_PREFIX}{secrets.token_hex(_BLIND_RANDOM_BYTES)}"
        while bid in taken:
            bid = f"{_BLIND_PREFIX}{secrets.token_hex(_BLIND_RANDOM_BYTES)}"
        taken.add(bid)
        assignment[sid] = bid
    return assignment


def _seal(plaintext: bytes, aad: bytes) -> tuple[str, bytes, bytes, bytes]:
    """Generate key, split into two XOR shares, and AES-GCM seal.

    Returns (package, key, share1, share2). The key only ever lives inside
    this call; callers discard it after combining shares is no longer
    needed (it is never returned).
    """
    key = secrets.token_bytes(_KEY_BYTES)
    nonce = secrets.token_bytes(_NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    package = _b64e(_VERSION_BYTE + nonce + ciphertext)

    share1 = secrets.token_bytes(_KEY_BYTES)
    share2 = bytes(a ^ b for a, b in zip(key, share1))
    return package, key, share1, share2


def blind(req: BlindRequest) -> dict:
    """Validate the layout and produce the view, package and two shares."""
    try:
        rows, cols = _resolve_dimensions(req.plate)
    except InfeasibleArrangement as exc:
        raise BlindInvalid(exc.categories, exc.messages) from exc

    entries = _validate_layout(req, rows, cols)
    sample_ids = [sid for sid, _r, _c in entries]
    blind_ids = _generate_blind_ids(sample_ids)

    # Operational view (row-major): well + blind_id only.
    view = [
        {"well": well_name(r, c), "blind_id": blind_ids[sid]}
        for sid, r, c in entries
    ]
    plate = _plate_descriptor(req.plate, rows, cols)

    # Sealed payload: the full correspondence, plus the bound context so an
    # opened package can be cross-checked independently of the AAD.
    mapping = [
        {"blind_id": blind_ids[sid], "sample_id": sid, "well": well_name(r, c)}
        for sid, r, c in entries
    ]
    plaintext = _canonical_json(
        {
            "version": _VERSION,
            "batch_id": req.batch_id,
            "plate": plate,
            "view": view,
            "mapping": mapping,
        }
    )

    package, _key, share1, share2 = _seal(plaintext, _aad(req.batch_id, plate, view))
    # _key is deliberately dropped here: neither key nor shares are logged.

    return {
        "batch_id": req.batch_id,
        "plate": plate,
        "view": view,
        "mapping_package": package,
        "key_shares": [
            {"holder": 1, "share": _b64e(share1)},
            {"holder": 2, "share": _b64e(share2)},
        ],
        "security": {
            "cipher": "AES-256-GCM",
            "key_splitting": "random 256-bit key split into two XOR one-time-pad "
            "shares; either single share is uniform and reveals nothing",
            "package_binds": ["batch_id", "plate", "complete_operational_view"],
            "unblinding_requires": [
                "batch_id",
                "complete_operational_view",
                "mapping_package",
                "two_distinct_key_shares",
            ],
        },
    }


# --------------------------------------------------------------- unblinding


def _parse_share(raw: str) -> bytes:
    """Decode one key share; raise BlindInvalid on any malformation."""
    try:
        share = _b64d(raw)
    except Exception as exc:  # binascii.Error / ValueError ...
        raise BlindInvalid(
            [VIOLATION_BLIND_SHARE_INVALID],
            ["a key share is not valid base64"],
        ) from exc
    if len(share) != _KEY_BYTES:
        raise BlindInvalid(
            [VIOLATION_BLIND_SHARE_INVALID],
            [f"a key share must decode to {_KEY_BYTES} bytes"],
        )
    return share


def _parse_package(raw: str) -> tuple[bytes, bytes]:
    """Split the package into (nonce, ciphertext_with_tag); validate framing."""
    try:
        blob = _b64d(raw)
    except Exception as exc:
        raise BlindInvalid(
            [VIOLATION_BLIND_PACKAGE_INVALID],
            ["mapping package is not valid base64"],
        ) from exc
    if len(blob) < 1 + _NONCE_BYTES + 16 or blob[:1] != _VERSION_BYTE:
        raise BlindInvalid(
            [VIOLATION_BLIND_PACKAGE_INVALID],
            ["mapping package framing or version is unsupported"],
        )
    nonce = blob[1:1 + _NONCE_BYTES]
    ciphertext = blob[1 + _NONCE_BYTES:]
    return nonce, ciphertext


def _validate_submitted_view(
    view_entries, rows: int, cols: int
) -> list[dict]:
    """Validate and canonicalize the submitted operational view.

    Order-insensitive; returns the view sorted row-major. Only public
    well/blind_id identifiers are involved here, never keys or mappings.
    """
    categories: list[str] = []
    messages: list[str] = []

    if not view_entries:
        raise BlindInvalid(
            [VIOLATION_BLIND_VIEW_INVALID],
            ["operational view must contain at least one entry"],
        )

    positions: list[tuple[int, int, str]] = []
    seen_wells: set[tuple[int, int]] = set()
    seen_blind: set[str] = set()
    for entry in view_entries:
        try:
            pos = parse_well(entry.well, rows, cols)
        except ValueError as exc:
            if VIOLATION_WELL_REFERENCE_INVALID not in categories:
                categories.append(VIOLATION_WELL_REFERENCE_INVALID)
            messages.append(str(exc))
            continue
        if pos in seen_wells:
            if VIOLATION_BLIND_VIEW_INVALID not in categories:
                categories.append(VIOLATION_BLIND_VIEW_INVALID)
            messages.append(f"well {well_name(*pos)} appears more than once in the view")
        if entry.blind_id in seen_blind:
            if VIOLATION_BLIND_VIEW_INVALID not in categories:
                categories.append(VIOLATION_BLIND_VIEW_INVALID)
            messages.append(
                f"blind_id '{entry.blind_id}' appears more than once in the view"
            )
        seen_wells.add(pos)
        seen_blind.add(entry.blind_id)
        positions.append((pos[0], pos[1], entry.blind_id))

    if categories:
        raise BlindInvalid(categories, messages)

    return [
        {"well": well_name(r, c), "blind_id": bid}
        for r, c, bid in sorted(positions, key=lambda x: (x[0], x[1]))
    ]


def _integrity_failure() -> BlindInvalid:
    # Single, generic message: decryption/authentication failures must not
    # reveal which of batch/plate/view/key differed, and no share, key or
    # mapping content is ever echoed.
    return BlindInvalid(
        [VIOLATION_BLIND_INTEGRITY_FAILED],
        ["mapping package authentication failed: batch, plate, operational "
         "view, package or key shares do not jointly match; no mapping revealed"],
    )


def unblind(req: UnblindRequest) -> dict:
    """All-or-nothing unblinding; any mismatch fails closed with 422."""
    try:
        rows, cols = _resolve_dimensions(req.plate)
    except InfeasibleArrangement as exc:
        # Re-tag plate problems under the blinding package category so the
        # error envelope stays uniform; the message carries no secrets.
        raise BlindInvalid(
            [VIOLATION_PLATE_DIMENSION_INVALID], exc.messages
        ) from exc

    view = _validate_submitted_view(req.view, rows, cols)
    nonce, ciphertext = _parse_package(req.mapping_package)

    raw_shares = [_parse_share(s) for s in req.key_shares]
    if raw_shares[0] == raw_shares[1]:
        # Two equal XOR shares reconstruct the all-zero key; they can never
        # have been issued as a pair. Treat as an invalid share submission.
        raise BlindInvalid(
            [VIOLATION_BLIND_SHARE_INVALID],
            ["the two key shares must be the two distinct shares issued together"],
        )
    key = bytes(a ^ b for a, b in zip(raw_shares[0], raw_shares[1]))

    plate = _plate_descriptor(req.plate, rows, cols)
    aad = _aad(req.batch_id, plate, view)
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise _integrity_failure() from exc

    # Authenticated: parsing the inner document only after GCM succeeded.
    try:
        payload = json.loads(plaintext.decode("utf-8"))
        payload_view = payload["view"]
        payload_mapping = payload["mapping"]
        payload_batch = payload["batch_id"]
        payload_plate = payload["plate"]
        payload_version = payload["version"]
    except (ValueError, KeyError, UnicodeDecodeError) as exc:
        raise _integrity_failure() from exc

    # Defense in depth: even though AAD/GCM already authenticates all of
    # this, re-check the bound context and internal correspondence
    # explicitly before revealing anything.
    if payload_version != _VERSION:
        raise _integrity_failure()
    if not isinstance(payload_view, list) or not isinstance(payload_mapping, list):
        raise _integrity_failure()
    if payload_batch != req.batch_id or payload_plate != plate or payload_view != view:
        raise BlindInvalid(
            [VIOLATION_BLIND_VIEW_INTEGRITY_FAILED],
            ["the package contents do not match the submitted batch, plate "
             "and complete operational view; no mapping revealed"],
        )

    view_blind_ids = {e["blind_id"] for e in view}
    revealed = []
    seen_blind: set[str] = set()
    try:
        for row in payload_mapping:
            if not isinstance(row, dict):
                raise _integrity_failure()
            bid, sid, well = row.get("blind_id"), row.get("sample_id"), row.get("well")
            if not isinstance(bid, str) or not isinstance(sid, str) or not isinstance(well, str):
                raise _integrity_failure()
            if bid not in view_blind_ids or bid in seen_blind:
                raise _integrity_failure()
            matching = [e for e in view if e["blind_id"] == bid]
            if len(matching) != 1 or matching[0]["well"] != well:
                raise _integrity_failure()
            seen_blind.add(bid)
            revealed.append({"blind_id": bid, "sample_id": sid, "well": well})
    except BlindInvalid:
        raise
    except TypeError as exc:  # authenticated-but-malformed inner structure
        raise _integrity_failure() from exc

    if seen_blind != view_blind_ids:
        raise _integrity_failure()

    revealed.sort(key=lambda e: parse_well(e["well"], rows, cols))
    return {
        "batch_id": req.batch_id,
        "plate": plate,
        "revealed": revealed,
    }
