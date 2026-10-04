"""Pydantic request/response models for the microplate arrangement API."""
from __future__ import annotations

import math
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Categories reported in 422 responses when no layout can exist.
# They identify the class of constraint that makes the instance infeasible.
VIOLATION_SAMPLE_IDS_DUPLICATED = "SAMPLE_IDS_DUPLICATED"
VIOLATION_GROUP_BATCH_INVALID = "GROUP_BATCH_INVALID"
VIOLATION_PLATE_DIMENSION_INVALID = "PLATE_DIMENSION_INVALID"
VIOLATION_WELL_REFERENCE_INVALID = "WELL_REFERENCE_INVALID"
VIOLATION_FIXED_CONFLICT = "FIXED_CONFLICT"
VIOLATION_FIXED_ON_BLOCKED = "FIXED_ON_BLOCKED"
VIOLATION_CAPACITY_EXCEEDED = "CAPACITY_EXCEEDED"
VIOLATION_SOLVER_INFEASIBLE = "SOLVER_INFEASIBLE"

# Categories reported in 422 responses of the reconcile endpoint when the
# target layout and the scanned plate state are inconsistent.
VIOLATION_TARGET_WELLS_DUPLICATED = "TARGET_WELLS_DUPLICATED"
VIOLATION_TARGET_ON_BLOCKED = "TARGET_ON_BLOCKED"
VIOLATION_SCAN_WELLS_DUPLICATED = "SCAN_WELLS_DUPLICATED"
VIOLATION_SCAN_SAMPLES_DUPLICATED = "SCAN_SAMPLES_DUPLICATED"
VIOLATION_SAMPLES_MISSING = "SAMPLES_MISSING"
VIOLATION_UNPLANNED_SAMPLES = "UNPLANNED_SAMPLES"
VIOLATION_BLOCKED_WELL_OCCUPIED = "BLOCKED_WELL_OCCUPIED"

# Categories reported in 422 responses of the spatial QC endpoint.
VIOLATION_SPATIAL_SAMPLE_DUPLICATED = "SPATIAL_SAMPLE_DUPLICATED"
VIOLATION_SPATIAL_SAMPLE_UNKNOWN = "SPATIAL_SAMPLE_UNKNOWN"
VIOLATION_SPATIAL_LAYOUT_CONFLICT = "SPATIAL_LAYOUT_CONFLICT"
VIOLATION_SPATIAL_READING_INVALID = "SPATIAL_READING_INVALID"
VIOLATION_SPATIAL_READING_DUPLICATED = "SPATIAL_READING_DUPLICATED"
VIOLATION_SPATIAL_GROUP_INSUFFICIENT = "SPATIAL_GROUP_INSUFFICIENT"
VIOLATION_SPATIAL_GRID_INSUFFICIENT = "SPATIAL_GRID_INSUFFICIENT"
VIOLATION_SPATIAL_NOT_CONVERGED = "SPATIAL_NOT_CONVERGED"

# Categories reported in 422 responses of the batch release endpoint when the
# scanned plate does not agree with the planned layout (stage 1 of release).
VIOLATION_CLOSED_LOOP_MISPLACEMENT = "CLOSED_LOOP_MISPLACEMENT"
VIOLATION_SCAN_LAYOUT_MISMATCH = "SCAN_LAYOUT_MISMATCH"

# Reasons reported (in ``reasons``) when the scan matched but the spatial QC
# release gates are not all satisfied; the batch is then NOT_RELEASED.
RELEASE_REASON_SPATIAL_EFFECT_EXCEEDED = "SPATIAL_EFFECT_EXCEEDED"
RELEASE_REASON_P_VALUE_BELOW_THRESHOLD = "P_VALUE_BELOW_THRESHOLD"
RELEASE_REASON_P_VALUE_UNAVAILABLE = "P_VALUE_UNAVAILABLE"

# Categories reported in 422 responses of the blinding endpoints.
VIOLATION_BLIND_WELLS_DUPLICATED = "BLIND_WELLS_DUPLICATED"
VIOLATION_BLIND_SAMPLE_WELL_MISMATCH = "BLIND_SAMPLE_WELL_MISMATCH"
VIOLATION_BLIND_VIEW_INVALID = "BLIND_VIEW_INVALID"
VIOLATION_BLIND_VIEW_INTEGRITY_FAILED = "BLIND_VIEW_INTEGRITY_FAILED"
VIOLATION_BLIND_SHARE_INVALID = "BLIND_SHARE_INVALID"
VIOLATION_BLIND_PACKAGE_INVALID = "BLIND_PACKAGE_INVALID"
VIOLATION_BLIND_INTEGRITY_FAILED = "BLIND_INTEGRITY_FAILED"

# Categories and error codes reported by the batch certificate endpoints.
# ``CERTIFICATE_VIEW_INVALID`` appears in 422 ``infeasibility_categories``;
# the ``CERT_*``/``REQUEST_KEY_CONFLICT`` codes appear as ``error`` in 404/409
# bodies; ``REVOCATION_VIEW_DIGEST_MISMATCH`` is a revocation reason stored on
# the certificate and returned by re-verification.
VIOLATION_CERTIFICATE_VIEW_INVALID = "CERTIFICATE_VIEW_INVALID"
CERTIFICATE_NOT_FOUND = "CERTIFICATE_NOT_FOUND"
CERTIFICATE_ALREADY_EXISTS = "CERTIFICATE_ALREADY_EXISTS"
CERTIFICATE_REVOKED = "CERTIFICATE_REVOKED"
CERTIFICATE_REVISION_MISMATCH = "CERTIFICATE_REVISION_MISMATCH"
REQUEST_KEY_CONFLICT = "REQUEST_KEY_CONFLICT"
REVOCATION_VIEW_DIGEST_MISMATCH = "VIEW_DIGEST_MISMATCH"

# Categories reported in 422 responses of the dilution-plan endpoint when no
# pipetting plan can satisfy every target well; no partial plan is emitted.
VIOLATION_DILUTION_TARGET_WELLS_DUPLICATED = "DILUTION_TARGET_WELLS_DUPLICATED"
VIOLATION_DILUTION_SOURCES_DUPLICATED = "DILUTION_SOURCES_DUPLICATED"
VIOLATION_DILUTION_SOURCE_UNKNOWN = "DILUTION_SOURCE_UNKNOWN"
VIOLATION_DILUTION_SOURCE_STOCK_INSUFFICIENT = "DILUTION_SOURCE_STOCK_INSUFFICIENT"
VIOLATION_DILUTION_TARGET_CAPACITY_EXCEEDED = "DILUTION_TARGET_CAPACITY_EXCEEDED"
VIOLATION_DILUTION_CONCENTRATION_UNACHIEVABLE = "DILUTION_CONCENTRATION_UNACHIEVABLE"
VIOLATION_DILUTION_ROUNDING_NO_GRID_SOLUTION = "DILUTION_ROUNDING_NO_GRID_SOLUTION"
VIOLATION_DILUTION_ROUNDING_TOLERANCE_EXCEEDED = "DILUTION_ROUNDING_TOLERANCE_EXCEEDED"


class PlateSpec(BaseModel):
    """Plate geometry.

    ``type`` selects a standard footprint ("24"/"48"/"96"/"384") or
    "custom", in which case ``rows`` and ``columns`` must be supplied.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["24", "48", "96", "384", "custom"] = "96"
    rows: Optional[int] = Field(default=None, ge=1, le=26)
    columns: Optional[int] = Field(default=None, ge=1, le=50)


class Sample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    group: str = Field(min_length=1, description="实验组标识")
    batch: str = Field(min_length=1, description="批次标识")


class FixedAssignment(BaseModel):
    """A sample pinned to a specific well; the solver may never move it."""

    model_config = ConfigDict(extra="forbid")

    sample_id: str = Field(min_length=1)
    well: str = Field(min_length=1, description="孔位,如 'C5'")


class ArrangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plate: PlateSpec = Field(default_factory=PlateSpec)
    samples: list[Sample]
    fixed: list[FixedAssignment] = Field(default_factory=list)
    blocked_wells: list[str] = Field(default_factory=list)
    seed: int = Field(default=0, ge=0)

    @field_validator("samples")
    @classmethod
    def _nonempty_samples(cls, v: list[Sample]) -> list[Sample]:
        if not v:
            raise ValueError("samples must contain at least one sample")
        return v


class ReconcileRequest(BaseModel):
    """Reconcile a physically scanned plate against a planned layout.

    ``target`` is the sample → well mapping exactly as returned in the
    ``layout`` field of ``POST /api/v1/arrangements``. ``scan`` is the
    observed well → sample mapping from a full plate scan; empty wells are
    simply omitted. ``staging_well`` optionally names a free, non-target,
    non-blocked well that may be borrowed to break closed-loop
    misplacements; it is restored to empty by the final instruction.
    """

    model_config = ConfigDict(extra="forbid")

    plate: PlateSpec = Field(default_factory=PlateSpec)
    target: dict[str, str] = Field(
        min_length=1, description="目标映射:样本 id → 目标孔位(原编排接口的 layout)"
    )
    scan: dict[str, str] = Field(
        default_factory=dict, description="扫描映射:孔位 → 样本 id(空孔不列出)"
    )
    blocked_wells: list[str] = Field(default_factory=list)
    staging_well: Optional[str] = Field(
        default=None, description="空闲且非目标的暂存孔,用于解开闭环错位"
    )


class SpatialSample(BaseModel):
    """A sample carrying an experiment group label for spatial QC."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    group: str = Field(min_length=1, description="实验组标识;中位数中心化与置换均在组内进行")


class SpatialQCRequest(BaseModel):
    """Spatial QC request.

    ``layout`` is the sample → well mapping exactly as returned in the
    ``layout`` field of ``POST /api/v1/arrangements``. ``readings`` maps each
    well to its numeric assay value; wells may be omitted or mapped to null
    to mark missing readings. Group medians are removed first, then row and
    column medians are stripped and accumulated by alternating median polish
    sweeps. An optional seed-controlled within-group permutation test yields
    an empirical, plus-one-corrected p-value for the maximal row/column
    effect.
    """

    model_config = ConfigDict(extra="forbid")

    plate: PlateSpec = Field(default_factory=PlateSpec)
    layout: dict[str, str] = Field(
        min_length=1, description="布局映射:样本 id → 孔位(编排接口响应中的 layout)"
    )
    samples: list[SpatialSample] = Field(min_length=1)
    readings: dict[str, Optional[float]] = Field(
        default_factory=dict,
        description="孔位 → 数值读数;省略孔位或显式 null 表示读数缺失",
    )
    seed: int = Field(default=0, ge=0, description="随机种子(≥0),控制组内置换")
    permutations: int = Field(
        default=1000, ge=0, le=1_000_000, description="组内置换次数(≥0,0 表示跳过置换)"
    )
    tolerance: float = Field(
        default=1e-6, gt=0, description="收敛阈值:本轮最大行列校正量 ≤ tolerance 即收敛"
    )
    max_iterations: int = Field(
        default=1000, ge=1, description="行列交替剥离的最大轮数"
    )

    @field_validator("tolerance")
    @classmethod
    def _tolerance_finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("tolerance must be a finite number")
        return v

    @field_validator("readings")
    @classmethod
    def _readings_finite(cls, v: dict[str, Optional[float]]) -> dict[str, Optional[float]]:
        for well, value in v.items():
            if value is not None and not math.isfinite(value):
                raise ValueError(f"reading at well '{well}' must be a finite number or null")
        return v


class BatchReleaseRequest(BaseModel):
    """Batch release verification request.

    Stage 1 reuses the reconciliation rules exactly: the complete ``scan``
    must agree well-by-well with the planned ``target`` layout — no missing
    planned sample, no unplanned sample, no closed-loop (or open-chain)
    misplacement and no blocked-well occupancy. Any disagreement yields 422
    with the problem categories and the misplaced-well list; no release
    conclusion is produced.

    Stage 2 runs only after the scan matches, reusing the spatial QC
    pipeline. The batch is released only when the polish converged, the
    maximal row/column effect is at most ``max_spatial_effect`` and the
    permutation p-value reaches ``min_p_value``. Any failed gate returns a
    NOT_RELEASED verdict with the failing reason(s); no partial conclusion is
    emitted.
    """

    model_config = ConfigDict(extra="forbid")

    plate: PlateSpec = Field(default_factory=PlateSpec)
    target: dict[str, str] = Field(
        min_length=1, description="计划映射:样本 id → 计划孔位(原编排接口的 layout)"
    )
    scan: dict[str, str] = Field(
        default_factory=dict, description="完整扫描:孔位 → 样本 id(空孔不列出)"
    )
    blocked_wells: list[str] = Field(default_factory=list)
    samples: list[SpatialSample] = Field(
        min_length=1, description="样本分组,每项 {id, group};应与计划样本一一对应"
    )
    readings: dict[str, Optional[float]] = Field(
        default_factory=dict,
        description="孔位 → 数值读数;省略孔位或显式 null 表示读数缺失",
    )
    seed: int = Field(default=0, ge=0, description="随机种子(≥0),控制组内置换")
    max_spatial_effect: float = Field(
        gt=0, description="空间效应上限:最大行列效应绝对值必须 ≤ 该值"
    )
    min_p_value: float = Field(
        ge=0.0,
        le=1.0,
        description="最低置换 p 值门槛(0–1);permutations=0 时无 p 值,该门无法通过",
    )
    permutations: int = Field(
        default=1000, ge=0, le=1_000_000, description="组内置换次数(≥0,0 表示跳过置换)"
    )
    tolerance: float = Field(
        default=1e-6, gt=0, description="收敛阈值:本轮最大行列校正量 ≤ tolerance 即收敛"
    )
    max_iterations: int = Field(
        default=1000, ge=1, description="行列交替剥离的最大轮数"
    )

    @field_validator("max_spatial_effect")
    @classmethod
    def _max_effect_finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("max_spatial_effect must be a finite number")
        return v

    @field_validator("tolerance")
    @classmethod
    def _tolerance_finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("tolerance must be a finite number")
        return v

    @field_validator("readings")
    @classmethod
    def _readings_finite(cls, v: dict[str, Optional[float]]) -> dict[str, Optional[float]]:
        for well, value in v.items():
            if value is not None and not math.isfinite(value):
                raise ValueError(f"reading at well '{well}' must be a finite number or null")
        return v


class BlindRequest(BaseModel):
    """Two-party collaborative blinding of an existing arrangement result.

    ``batch_id`` names the production batch; ``layout`` is the sample → well
    mapping exactly as returned in the ``layout`` field of
    ``POST /api/v1/arrangements``. Every sample must map to exactly one legal
    well and every well to at most one sample, otherwise the request is
    rejected with 422 and no blind codes, mapping package or key shares are
    produced.
    """

    model_config = ConfigDict(extra="forbid")

    plate: PlateSpec = Field(default_factory=PlateSpec)
    batch_id: str = Field(min_length=1, description="批次标识")
    layout: dict[str, str] = Field(
        min_length=1, description="样本 id → 孔位(编排接口响应中的 layout)"
    )


class BlindViewEntry(BaseModel):
    """One row of the blinded operational view: well only, no sample id."""

    model_config = ConfigDict(extra="forbid")

    well: str = Field(min_length=1)
    blind_id: str = Field(min_length=1)


class UnblindRequest(BaseModel):
    """Unblinding requires all four artifacts at once.

    The mapping package binds ``batch_id``, the plate and the operational
    view; a single key share reveals nothing. Any integrity, share or batch
    mismatch yields 422 with no partial correspondence leaked.
    """

    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(min_length=1, description="批次标识,必须与盲化时一致")
    plate: PlateSpec = Field(default_factory=PlateSpec)
    view: list[BlindViewEntry] = Field(
        min_length=1, description="盲化时返回的完整操作视图(well + blind_id),顺序不敏感"
    )
    mapping_package: str = Field(min_length=1, description="盲化时返回的经认证加密映射包")
    key_shares: list[str] = Field(
        min_length=2,
        max_length=2,
        description="两份不同的密钥份额(顺序不敏感);缺少、相同或多余均拒绝",
    )


class DilutionSource(BaseModel):
    """One source sample with its stock concentration and usable volume."""

    model_config = ConfigDict(extra="forbid")

    sample_id: str = Field(min_length=1, description="来源样本 id,必须出现在目标 layout 中")
    concentration: float = Field(gt=0, description="来源液浓度(>0,任意一致单位)")
    available_volume_ul: float = Field(ge=0, description="该来源样本可用于分配的总体积(µL,≥0)")

    @field_validator("concentration", "available_volume_ul")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("must be a finite number")
        return v


class DilutionTarget(BaseModel):
    """One target well of the existing layout with its desired dilution."""

    model_config = ConfigDict(extra="forbid")

    well: str = Field(min_length=1, description="目标孔位,如 'C5',必须是 layout 中的孔")
    sample_id: Optional[str] = Field(
        default=None,
        min_length=1,
        description="供给该孔的来源样本 id;省略时取该孔在 layout 中的占孔样本。"
        "同一来源供给多个目标孔(稀释梯度/标准曲线/复孔)时显式指定,共享其库存",
    )
    target_concentration: float = Field(
        gt=0, description="目标浓度(>0,不得超过其来源样本浓度)"
    )
    final_volume_ul: float = Field(gt=0, description="该孔最终体积(µL,>0)")

    @field_validator("target_concentration", "final_volume_ul")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("must be a finite number")
        return v


class PipetteSpec(BaseModel):
    """Pipetting precision: transfer volumes lie on a fixed volume grid."""

    model_config = ConfigDict(extra="forbid")

    resolution_ul: float = Field(gt=0, description="移液分辨率(µL,>0);每次移液体积必须为其整数倍")
    min_aspirate_ul: float = Field(
        default=0.0, ge=0, description="单次最小吸取量(µL,≥0);小于该值的移液不允许,0 表示无下限"
    )

    @field_validator("resolution_ul", "min_aspirate_ul")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("must be a finite number")
        return v


class DiluentSpec(BaseModel):
    """The zero-concentration diluent used to top each well up."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, default="diluent", description="稀释液标识")


class DilutionPlanRequest(BaseModel):
    """Deterministic serial-free dilution plan for an existing layout.

    ``layout`` is the sample → well mapping exactly as returned in the
    ``layout`` field of ``POST /api/v1/arrangements`` (or the ``target``
    field of the reconcile endpoint). Every ``targets`` entry names a well
    of that layout and states the desired concentration and final volume;
    by default the well's occupant in ``layout`` feeds the well, but a
    target may name an explicit ``sample_id`` source (dilution gradients,
    standard curves and replicate wells typically share one source that
    way). A single source shared by several target wells draws on one
    common stock inventory.

    Each well receives (in order) one diluent transfer (possibly zero, i.e.
    omitted) followed by one source-stock transfer. Both transfer volumes
    are multiples of ``pipette.resolution_ul`` and respect
    ``pipette.min_aspirate_ul``; their sum equals ``final_volume_ul``. The
    resulting actual concentration must be within the relative
    ``concentration_tolerance`` of the target, and ``final_volume_ul`` must
    not exceed ``well_capacity_ul``. Any well for which no rounding grid
    solution exists — or any source whose shared inventory is exceeded —
    fails the whole request with HTTP 422; no partial plan is produced. The
    same request payload always yields byte-identical plans.
    """

    model_config = ConfigDict(extra="forbid")

    plate: PlateSpec = Field(default_factory=PlateSpec)
    layout: dict[str, str] = Field(
        min_length=1, description="目标 layout:样本 id → 孔位(原编排接口响应中的 layout)"
    )
    sources: list[DilutionSource] = Field(min_length=1)
    targets: list[DilutionTarget] = Field(min_length=1)
    well_capacity_ul: float = Field(gt=0, description="每个目标孔的容量上限(µL,>0)")
    diluent: DiluentSpec = Field(default_factory=DiluentSpec)
    pipette: PipetteSpec
    concentration_tolerance: float = Field(
        gt=0,
        description="允许的相对浓度误差(>0,如 0.01 表示 ±1%):"
        "|实际浓度-目标浓度|/目标浓度 ≤ 该值",
    )

    @field_validator("well_capacity_ul", "concentration_tolerance")
    @classmethod
    def _finite_positive(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("must be a finite number")
        return v


class CertificateCreateRequest(BaseModel):
    """Establish a certificate for a batch that passes release verification.

    ``release`` is a batch release verification request with exactly the
    structure of ``POST /api/v1/arrangements/batch-release``; the service
    reuses the reconciliation rules and the spatial QC gates unchanged. Only
    a ``RELEASED`` verdict is certified: the layout, scan, readings,
    thresholds and the optional blinded-view digest are then frozen into a
    SQLite snapshot in a single transaction. ``view`` is the blinded
    operational view (well + blind_id) produced by ``POST /api/v1/blinding/blind``;
    only its SHA-256 digest is stored — never the view itself, never key
    shares, never the plaintext blind mapping.

    ``request_key`` is the idempotency key: replaying the same key with
    identical content returns the recorded response; the same key with
    different content is rejected with 409.
    """

    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(min_length=1, description="批次标识;每个批次至多存在一份证书")
    request_key: str = Field(
        min_length=1, description="幂等键:同键同内容重放已记录响应,同键不同内容返回 409"
    )
    release: BatchReleaseRequest = Field(
        description="批次放行核验请求(结构与 batch-release 接口相同)"
    )
    view: Optional[list[BlindViewEntry]] = Field(
        default=None,
        min_length=1,
        description="可选的盲化操作视图(well + blind_id);仅保存其摘要,不保存视图本身",
    )


class CertificateReverifyRequest(BaseModel):
    """Re-verify a certified batch with a fresh scan and fresh readings.

    The plate, layout, samples, seed and thresholds are restored from the
    frozen certificate snapshot; only ``scan``, ``readings`` and the optional
    blinded operational ``view`` are supplied anew. ``expected_revision``
    must equal the certificate's current revision (older revisions can never
    be overwritten). If the batch still passes, the revision is incremented
    atomically; any misplacement, QC failure or view-digest change marks the
    certificate REVOKED and returns the first failure reason. A revoked
    certificate can never be restored.
    """

    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(min_length=1, description="批次标识")
    request_key: str = Field(
        min_length=1, description="幂等键:同键同内容重放已记录响应,同键不同内容返回 409"
    )
    expected_revision: int = Field(
        ge=1, description="期望的当前 revision;与证书当前值不一致时返回 409,旧 revision 不可覆盖"
    )
    scan: dict[str, str] = Field(
        default_factory=dict, description="新的完整扫描:孔位 → 样本 id(空孔不列出)"
    )
    readings: dict[str, Optional[float]] = Field(
        default_factory=dict,
        description="新的孔位 → 数值读数;省略孔位或显式 null 表示读数缺失",
    )
    view: Optional[list[BlindViewEntry]] = Field(
        default=None,
        min_length=1,
        description="可选的盲化操作视图;携带时其摘要必须与证书冻结摘要一致,否则吊销证书",
    )

    @field_validator("readings")
    @classmethod
    def _readings_finite(cls, v: dict[str, Optional[float]]) -> dict[str, Optional[float]]:
        for well, value in v.items():
            if value is not None and not math.isfinite(value):
                raise ValueError(f"reading at well '{well}' must be a finite number or null")
        return v
