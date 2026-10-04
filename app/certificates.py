"""Batch certificates: persist release verdicts and re-verify against them.

Two endpoints:

* ``POST /api/v1/certificates`` — runs the full batch release verification
  (reusing the reconciliation rules and the spatial QC gates unchanged).
  Only a ``RELEASED`` verdict is certified: the layout, scan, readings,
  thresholds and the blinded-view digest are frozen into a SQLite snapshot
  in a single transaction, and the certificate id, status, revision and
  content digest are returned. A ``NOT_RELEASED`` verdict (200) or a
  stage-1/structural failure (422) creates nothing.
* ``POST /api/v1/certificates/reverify`` — re-checks a certified batch with
  a fresh scan and fresh readings (and optionally the blinded operational
  view). Every other parameter is restored from the frozen snapshot. If the
  batch still passes, an immutable revision snapshot carrying the fresh
  scan/readings is appended, the content digest is recomputed over that
  snapshot and the certificate is pointed at the new revision — all in the
  same transaction; every past revision stays stored and auditable. Any
  misplacement, QC failure or view-digest change marks the certificate
  ``REVOKED`` (no new revision is appended) and the first failure reason is
  returned. A revoked certificate can never be restored, and a stale
  ``expected_revision`` can never overwrite a newer one (409).

Idempotency: both endpoints take ``batch_id`` + ``request_key``. Replaying a
request key with identical content returns the recorded response; the same
key with different content is rejected with 409. State-dependent rejections
(certificate missing/revoked/already existing, stale revision) are not
recorded under the key.

Confidentiality: certificates never store key shares or plaintext blind
mappings — of the optional blinded operational view only a SHA-256 digest
is kept.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from pydantic import BaseModel

from .plates import parse_well, well_name
from .release import (
    VERDICT_RELEASED,
    ReleaseInvalid,
    batch_release,
    release_invalid_body,
)
from .schemas import (
    CERTIFICATE_ALREADY_EXISTS,
    CERTIFICATE_NOT_FOUND,
    CERTIFICATE_REVISION_MISMATCH,
    CERTIFICATE_REVOKED,
    REQUEST_KEY_CONFLICT,
    REVOCATION_VIEW_DIGEST_MISMATCH,
    VIOLATION_CERTIFICATE_VIEW_INVALID,
    VIOLATION_WELL_REFERENCE_INVALID,
    BatchReleaseRequest,
    CertificateCreateRequest,
    CertificateReverifyRequest,
    PlateSpec,
)

# Certificate lifecycle states.
STATUS_ACTIVE = "ACTIVE"
STATUS_REVOKED = "REVOKED"
# Create-outcome marker used when the release gates did not pass.
STATUS_NOT_CERTIFIED = "NOT_CERTIFIED"

# Idempotency endpoint tags (part of the content hash, so a key cannot be
# reused across the two endpoints either).
_ENDPOINT_CREATE = "POST /api/v1/certificates"
_ENDPOINT_REVERIFY = "POST /api/v1/certificates/reverify"

DEFAULT_DB_PATH = "certificates.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS certificates (
    batch_id        TEXT PRIMARY KEY,
    certificate_id  TEXT NOT NULL UNIQUE,
    status          TEXT NOT NULL CHECK (status IN ('ACTIVE', 'REVOKED')),
    revision        INTEGER NOT NULL CHECK (revision >= 1),
    digest          TEXT NOT NULL,
    view_digest     TEXT,
    snapshot        TEXT NOT NULL,
    revoked_reason  TEXT,
    revoked_detail  TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
-- Immutable per-revision evidence snapshots. Rows are append-only: a
-- successful re-verification adds exactly one row carrying the fresh
-- scan/readings, so every past revision stays auditable. ``certificates``
-- always points at the latest revision.
CREATE TABLE IF NOT EXISTS certificate_revisions (
    batch_id        TEXT NOT NULL,
    revision        INTEGER NOT NULL CHECK (revision >= 1),
    digest          TEXT NOT NULL,
    view_digest     TEXT,
    snapshot        TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (batch_id, revision)
);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    request_key     TEXT PRIMARY KEY,
    endpoint        TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    response_status INTEGER NOT NULL,
    response_body   TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
-- Re-test sampling plans drawn from a certificate revision. Every row pins
-- the certificate revision (and that revision's content digest) whose frozen
-- layout/grouping/scan/readings the plan was drawn from; advancing the
-- certificate to a newer revision never mutates stored plans.
CREATE TABLE IF NOT EXISTS retest_plans (
    plan_id         TEXT PRIMARY KEY,
    batch_id        TEXT NOT NULL,
    certificate_id  TEXT NOT NULL,
    revision        INTEGER NOT NULL CHECK (revision >= 1),
    digest          TEXT NOT NULL,
    seed            INTEGER NOT NULL,
    plan_body       TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
"""


class CertificateViewInvalid(Exception):
    """The submitted blinded operational view is malformed (HTTP 422).

    Only public identifiers (well names, blind ids) appear in messages;
    the certificate is left unchanged.
    """

    def __init__(self, categories: list[str], messages: list[str]):
        self.categories = categories
        self.messages = messages
        super().__init__("; ".join(messages) if messages else "invalid blinded view")


class CertificateStore:
    """SQLite persistence for certificates, revision snapshots, idempotency.

    A single connection guarded by an RLock; every endpoint operation runs
    inside one ``BEGIN IMMEDIATE`` transaction so the certificate write, the
    appended revision snapshot and the idempotency record commit (or roll
    back) together.
    """

    def __init__(self, path: str):
        self._path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.RLock()
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
            # Databases written before per-revision snapshots existed: anchor
            # every certificate's current snapshot as an auditable revision
            # row (its current revision number). INSERT OR IGNORE keeps the
            # migration idempotent across restarts.
            self._conn.execute(
                "INSERT OR IGNORE INTO certificate_revisions "
                "(batch_id, revision, digest, view_digest, snapshot, created_at) "
                "SELECT batch_id, revision, digest, view_digest, snapshot, "
                "created_at FROM certificates"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self):
        """One atomic unit of work: idempotency check, state change, record."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()


_STORES: dict[str, CertificateStore] = {}
_STORES_LOCK = threading.Lock()


def get_store() -> CertificateStore:
    """Return the store for ``CERT_DB_PATH`` (created lazily, cached)."""
    path = os.getenv("CERT_DB_PATH", DEFAULT_DB_PATH)
    with _STORES_LOCK:
        store = _STORES.get(path)
        if store is None:
            store = CertificateStore(path)
            _STORES[path] = store
        return store


def reset_stores() -> None:
    """Close and forget every cached store (used by tests)."""
    with _STORES_LOCK:
        for store in _STORES.values():
            store.close()
        _STORES.clear()


# ------------------------------------------------------------------ helpers


def _canonical_json(obj) -> bytes:
    """Deterministic JSON encoding used for digests and stored snapshots."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _content_hash(endpoint: str, req: BaseModel) -> str:
    """Semantic hash of the full request (endpoint + parsed payload)."""
    return _sha256(
        _canonical_json({"endpoint": endpoint, "payload": req.model_dump(mode="json")})
    )


def _certificate_id(batch_id: str, request_key: str) -> str:
    return "cert-" + _sha256(
        f"certificate\0{batch_id}\0{request_key}".encode("utf-8")
    )[:24]


def _canonical_view_digest(view, rows: int, cols: int) -> str:
    """SHA-256 over the canonical (row-major, canonical well names) view.

    Raises CertificateViewInvalid on unparseable wells, duplicate wells or
    duplicate blind ids; nothing is stored in that case.
    """
    categories: list[str] = []
    messages: list[str] = []
    positions: list[tuple[int, int, str]] = []
    seen_wells: set[tuple[int, int]] = set()
    seen_blind: set[str] = set()
    for entry in view:
        try:
            pos = parse_well(entry.well, rows, cols)
        except ValueError as exc:
            if VIOLATION_WELL_REFERENCE_INVALID not in categories:
                categories.append(VIOLATION_WELL_REFERENCE_INVALID)
            messages.append(str(exc))
            continue
        if pos in seen_wells:
            if VIOLATION_CERTIFICATE_VIEW_INVALID not in categories:
                categories.append(VIOLATION_CERTIFICATE_VIEW_INVALID)
            messages.append(f"well {well_name(*pos)} appears more than once in the view")
        if entry.blind_id in seen_blind:
            if VIOLATION_CERTIFICATE_VIEW_INVALID not in categories:
                categories.append(VIOLATION_CERTIFICATE_VIEW_INVALID)
            messages.append(
                f"blind_id '{entry.blind_id}' appears more than once in the view"
            )
        seen_wells.add(pos)
        seen_blind.add(entry.blind_id)
        positions.append((pos[0], pos[1], entry.blind_id))

    if categories:
        raise CertificateViewInvalid(categories, messages)

    canonical = [
        {"well": well_name(r, c), "blind_id": bid}
        for r, c, bid in sorted(positions, key=lambda x: (x[0], x[1]))
    ]
    return _sha256(_canonical_json(canonical))


def _snapshot(req: CertificateCreateRequest, rows: int, cols: int,
              view_digest: str | None) -> dict:
    """The frozen parameter set restored at every future re-verification."""
    rel = req.release
    return {
        "batch_id": req.batch_id,
        "plate": {"type": rel.plate.type, "rows": rows, "columns": cols},
        "target": dict(rel.target),
        "scan": dict(rel.scan),
        "readings": dict(rel.readings),
        "samples": [s.model_dump(mode="json") for s in rel.samples],
        "blocked_wells": list(rel.blocked_wells),
        "seed": rel.seed,
        "thresholds": {
            "max_spatial_effect": rel.max_spatial_effect,
            "min_p_value": rel.min_p_value,
            "permutations": rel.permutations,
            "tolerance": rel.tolerance,
            "max_iterations": rel.max_iterations,
        },
        "view_digest": view_digest,
    }


def _reverification_snapshot(base: dict, req: CertificateReverifyRequest,
                             view_digest: str | None) -> dict:
    """Evidence frozen at the next revision of a re-verified certificate.

    The plan parameters (plate, layout, samples, seed, thresholds) and the
    frozen view digest carry over unchanged from the current snapshot; only
    the scan and the readings come from the re-verification request.
    """
    return {
        "batch_id": base["batch_id"],
        "plate": base["plate"],
        "target": base["target"],
        "scan": dict(req.scan),
        "readings": dict(req.readings),
        "samples": base["samples"],
        "blocked_wells": base["blocked_wells"],
        "seed": base["seed"],
        "thresholds": base["thresholds"],
        "view_digest": view_digest,
    }


def _get_certificate(conn: sqlite3.Connection, batch_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM certificates WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def _insert_revision(conn: sqlite3.Connection, batch_id: str, revision: int,
                     digest: str, view_digest: str | None,
                     snapshot_json: str, created_at: str) -> None:
    """Append one immutable evidence snapshot for a certificate revision."""
    conn.execute(
        "INSERT INTO certificate_revisions "
        "(batch_id, revision, digest, view_digest, snapshot, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (batch_id, revision, digest, view_digest, snapshot_json, created_at),
    )


def _idem_record(conn: sqlite3.Connection, request_key: str, endpoint: str,
                 content_hash: str, status: int, body: dict) -> None:
    conn.execute(
        "INSERT INTO idempotency_keys "
        "(request_key, endpoint, content_hash, response_status, response_body, "
        "created_at) VALUES (?,?,?,?,?,?)",
        (request_key, endpoint, content_hash, status,
         json.dumps(body, ensure_ascii=False), _utcnow()),
    )


def _idem_replay(conn: sqlite3.Connection, request_key: str,
                 content_hash: str) -> tuple[int, dict] | None:
    """Return the recorded response, a 409 key-conflict, or None if unseen."""
    row = conn.execute(
        "SELECT content_hash, response_status, response_body "
        "FROM idempotency_keys WHERE request_key = ?",
        (request_key,),
    ).fetchone()
    if row is None:
        return None
    if row["content_hash"] != content_hash:
        # The key conflict itself is not recorded: the original record stands.
        return 409, {
            "detail": "request_key was already used with a different payload; "
            "choose a fresh request_key for a new operation",
            "error": REQUEST_KEY_CONFLICT,
            "request_key": request_key,
        }
    return row["response_status"], json.loads(row["response_body"])


def _view_invalid_body(exc: CertificateViewInvalid, consequence: str) -> dict:
    return {
        "detail": f"The blinded operational view is invalid; {consequence}.",
        "infeasibility_categories": exc.categories,
        "messages": exc.messages,
    }


# -------------------------------------------------------------------- create


def create_certificate(req: CertificateCreateRequest) -> tuple[int, dict]:
    """Verify the release request and, only when RELEASED, freeze the batch."""
    store = get_store()
    content_hash = _content_hash(_ENDPOINT_CREATE, req)
    with store.transaction() as conn:
        replay = _idem_replay(conn, req.request_key, content_hash)
        if replay is not None:
            return replay

        existing = _get_certificate(conn, req.batch_id)
        if existing is not None:
            # State-dependent rejection (not recorded under the request key):
            # one certificate per batch, ever — a revoked batch stays revoked.
            return 409, {
                "detail": "the batch already has a certificate; certificates are "
                "never replaced and a revoked certificate is never restored",
                "error": CERTIFICATE_ALREADY_EXISTS,
                "certificate_id": existing["certificate_id"],
                "batch_id": req.batch_id,
                "status": existing["status"],
                "revision": existing["revision"],
            }

        # Reuse the two-stage release verification unchanged.
        try:
            result = batch_release(req.release)
        except ReleaseInvalid as exc:
            body = release_invalid_body(exc)
            _idem_record(conn, req.request_key, _ENDPOINT_CREATE, content_hash, 422, body)
            return 422, body

        if result["verdict"] != VERDICT_RELEASED:
            body = {
                "certificate_id": None,
                "batch_id": req.batch_id,
                "status": STATUS_NOT_CERTIFIED,
                "verdict": result["verdict"],
                "reasons": result["reasons"],
                "messages": result["messages"],
                "thresholds": result["thresholds"],
                "gates": result["gates"],
            }
            _idem_record(conn, req.request_key, _ENDPOINT_CREATE, content_hash, 200, body)
            return 200, body

        rows, cols = result["plate"]["rows"], result["plate"]["columns"]
        try:
            view_digest = (
                _canonical_view_digest(req.view, rows, cols)
                if req.view is not None
                else None
            )
        except CertificateViewInvalid as exc:
            body = _view_invalid_body(exc, "no certificate was created")
            _idem_record(conn, req.request_key, _ENDPOINT_CREATE, content_hash, 422, body)
            return 422, body

        # RELEASED: freeze layout, scan, readings, thresholds and the view
        # digest in the same transaction as the idempotency record. The
        # snapshot is stored twice: as revision 1 of the append-only
        # evidence trail and as the certificate's current snapshot.
        snapshot = _snapshot(req, rows, cols, view_digest)
        snapshot_json = _canonical_json(snapshot)
        digest = _sha256(snapshot_json)
        cert_id = _certificate_id(req.batch_id, req.request_key)
        now = _utcnow()
        conn.execute(
            "INSERT INTO certificates (batch_id, certificate_id, status, revision, "
            "digest, view_digest, snapshot, revoked_reason, revoked_detail, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (req.batch_id, cert_id, STATUS_ACTIVE, 1, digest, view_digest,
             snapshot_json.decode("utf-8"), None, None, now, now),
        )
        _insert_revision(conn, req.batch_id, 1, digest, view_digest,
                         snapshot_json.decode("utf-8"), now)
        body = {
            "certificate_id": cert_id,
            "batch_id": req.batch_id,
            "status": STATUS_ACTIVE,
            "revision": 1,
            "digest": digest,
            "view_digest": view_digest,
        }
        _idem_record(conn, req.request_key, _ENDPOINT_CREATE, content_hash, 201, body)
        return 201, body


# ------------------------------------------------------------------ reverify


def _revoke(conn: sqlite3.Connection, cert: dict, req: CertificateReverifyRequest,
            content_hash: str, *, reason: str, messages: list[str],
            misplaced_wells: list[dict], cycles: list[list[str]]) -> tuple[int, dict]:
    """Mark the certificate REVOKED (first failure reason) and record it."""
    detail = {
        "messages": messages,
        "misplaced_wells": misplaced_wells,
        "cycles": cycles,
    }
    conn.execute(
        "UPDATE certificates SET status = ?, revoked_reason = ?, "
        "revoked_detail = ?, updated_at = ? "
        "WHERE batch_id = ? AND revision = ? AND status = ?",
        (STATUS_REVOKED, reason, json.dumps(detail, ensure_ascii=False),
         _utcnow(), req.batch_id, cert["revision"], STATUS_ACTIVE),
    )
    body = {
        "certificate_id": cert["certificate_id"],
        "batch_id": req.batch_id,
        "status": STATUS_REVOKED,
        "revision": cert["revision"],
        "digest": cert["digest"],
        "revocation": {
            "reason": reason,
            "messages": messages,
            "misplaced_wells": misplaced_wells,
            "cycles": cycles,
        },
    }
    _idem_record(conn, req.request_key, _ENDPOINT_REVERIFY, content_hash, 200, body)
    return 200, body


def reverify_certificate(req: CertificateReverifyRequest) -> tuple[int, dict]:
    """Re-verify a certified batch; increment the revision or revoke it."""
    store = get_store()
    content_hash = _content_hash(_ENDPOINT_REVERIFY, req)
    with store.transaction() as conn:
        replay = _idem_replay(conn, req.request_key, content_hash)
        if replay is not None:
            return replay

        cert = _get_certificate(conn, req.batch_id)
        if cert is None:
            # State-dependent rejections are not recorded under the key.
            return 404, {
                "detail": "no certificate exists for the batch",
                "error": CERTIFICATE_NOT_FOUND,
                "batch_id": req.batch_id,
            }
        if cert["status"] == STATUS_REVOKED:
            return 409, {
                "detail": "the certificate is REVOKED and can never be restored",
                "error": CERTIFICATE_REVOKED,
                "certificate_id": cert["certificate_id"],
                "batch_id": req.batch_id,
                "status": STATUS_REVOKED,
                "revoked_reason": cert["revoked_reason"],
            }
        if req.expected_revision != cert["revision"]:
            return 409, {
                "detail": "expected_revision does not match the certificate's "
                "current revision; older revisions can never be overwritten",
                "error": CERTIFICATE_REVISION_MISMATCH,
                "certificate_id": cert["certificate_id"],
                "batch_id": req.batch_id,
                "current_revision": cert["revision"],
                "expected_revision": req.expected_revision,
            }

        snapshot = json.loads(cert["snapshot"])
        rows = snapshot["plate"]["rows"]
        cols = snapshot["plate"]["columns"]

        view_digest = None
        if req.view is not None:
            try:
                view_digest = _canonical_view_digest(req.view, rows, cols)
            except CertificateViewInvalid as exc:
                body = _view_invalid_body(exc, "the certificate was left unchanged")
                _idem_record(
                    conn, req.request_key, _ENDPOINT_REVERIFY, content_hash, 422, body
                )
                return 422, body

        # Restore every other parameter from the frozen snapshot; only the
        # scan, the readings and the optional view come from the request.
        release_req = BatchReleaseRequest(
            plate=PlateSpec(**snapshot["plate"]),
            target=snapshot["target"],
            scan=req.scan,
            blocked_wells=snapshot["blocked_wells"],
            samples=snapshot["samples"],
            readings=req.readings,
            seed=snapshot["seed"],
            max_spatial_effect=snapshot["thresholds"]["max_spatial_effect"],
            min_p_value=snapshot["thresholds"]["min_p_value"],
            permutations=snapshot["thresholds"]["permutations"],
            tolerance=snapshot["thresholds"]["tolerance"],
            max_iterations=snapshot["thresholds"]["max_iterations"],
        )
        try:
            result = batch_release(release_req)
        except ReleaseInvalid as exc:
            # Any disagreement with the frozen plan revokes the certificate;
            # the first reported category is the revocation reason.
            return _revoke(
                conn, cert, req, content_hash,
                reason=exc.categories[0],
                messages=exc.messages,
                misplaced_wells=exc.misplaced_wells,
                cycles=exc.cycles,
            )
        if result["verdict"] != VERDICT_RELEASED:
            return _revoke(
                conn, cert, req, content_hash,
                reason=result["reasons"][0],
                messages=result["messages"],
                misplaced_wells=[],
                cycles=[],
            )

        if (
            view_digest is not None
            and cert["view_digest"] is not None
            and view_digest != cert["view_digest"]
        ):
            return _revoke(
                conn, cert, req, content_hash,
                reason=REVOCATION_VIEW_DIGEST_MISMATCH,
                messages=[
                    "the presented blinded operational view does not match the "
                    "digest frozen in the certificate"
                ],
                misplaced_wells=[],
                cycles=[],
            )

        # Still passing: freeze the fresh evidence (new scan/readings over
        # the unchanged frozen plan) as the next immutable revision, recompute
        # the digest over that snapshot and point the certificate at it —
        # revision counter, current snapshot, appended revision row and the
        # idempotency record all commit (or roll back) in this transaction.
        new_revision = cert["revision"] + 1
        new_snapshot_json = _canonical_json(
            _reverification_snapshot(snapshot, req, cert["view_digest"])
        )
        new_digest = _sha256(new_snapshot_json)
        now = _utcnow()
        _insert_revision(conn, req.batch_id, new_revision, new_digest,
                         cert["view_digest"], new_snapshot_json.decode("utf-8"),
                         now)
        cur = conn.execute(
            "UPDATE certificates SET revision = ?, digest = ?, snapshot = ?, "
            "updated_at = ? "
            "WHERE batch_id = ? AND revision = ? AND status = ?",
            (new_revision, new_digest, new_snapshot_json.decode("utf-8"), now,
             req.batch_id, cert["revision"], STATUS_ACTIVE),
        )
        if cur.rowcount != 1:
            # Unreachable: the row is locked by this transaction. Guarded so a
            # silent no-op can never masquerade as an increment.
            raise RuntimeError("certificate revision changed concurrently")
        body = {
            "certificate_id": cert["certificate_id"],
            "batch_id": req.batch_id,
            "status": STATUS_ACTIVE,
            "revision": new_revision,
            "digest": new_digest,
            "view_digest": cert["view_digest"],
        }
        _idem_record(conn, req.request_key, _ENDPOINT_REVERIFY, content_hash, 200, body)
        return 200, body
