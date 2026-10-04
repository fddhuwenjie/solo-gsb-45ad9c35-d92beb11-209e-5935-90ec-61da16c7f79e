"""FastAPI application: microplate sample arrangement service."""
from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse

from .metrics import compute_spreads, count_adjacent_pairs, verify_layout
from .planner import InfeasibleArrangement, SolverTimeout, plan_layout
from .plates import parse_well
from .blinding import BlindInvalid, blind, unblind
from .certificates import create_certificate, reverify_certificate
from .dilution import DilutionPlanInvalid, dilution_plan
from .reconciler import ReconcileInvalid, StagingConflict, reconcile
from .release import ReleaseInvalid, batch_release, release_invalid_body
from .schemas import ArrangeRequest, BatchReleaseRequest, BlindRequest, CertificateCreateRequest, CertificateReverifyRequest, DilutionPlanRequest, ReconcileRequest, SpatialQCRequest, UnblindRequest
from .spatial_qc import SpatialQCInvalid, spatial_qc

app = FastAPI(
    title="微孔板样本编排 API",
    description=(
        "提交板型、样本(含实验组与批次)、固定孔位、禁用孔位与 seed，"
        "返回字典序最优布局:先最小化各实验组行/列数量差，"
        "再最小化同批次样本正交相邻对数。相同输入与 seed 必然得到相同布局。"
    ),
    version="1.0.0",
)


@app.exception_handler(InfeasibleArrangement)
async def infeasible_handler(request: Request, exc: InfeasibleArrangement) -> JSONResponse:
    # 422 with the constraint categories causing infeasibility;
    # no partial layout is ever returned.
    return JSONResponse(
        status_code=422,
        content={
            "detail": "No feasible layout exists for the submitted constraints.",
            "infeasibility_categories": exc.categories,
            "messages": exc.messages,
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "detail": "Request payload failed validation.",
            "infeasibility_categories": ["REQUEST_VALIDATION_ERROR"],
            "messages": [
                ".".join(str(x) for x in e["loc"] if x != "body") + ": " + e["msg"]
                for e in exc.errors()
            ],
        },
    )


@app.exception_handler(SolverTimeout)
async def timeout_handler(request: Request, exc: SolverTimeout) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "detail": "The solver did not finish within the configured time budget; "
            "no partial layout is returned.",
            "stage": exc.stage,
            "elapsed_seconds": round(exc.elapsed, 3),
        },
    )


@app.exception_handler(ReconcileInvalid)
async def reconcile_invalid_handler(request: Request, exc: ReconcileInvalid) -> JSONResponse:
    # 422 with every inconsistency found between target layout, scan and
    # blocked wells; no move instructions are generated.
    return JSONResponse(
        status_code=422,
        content={
            "detail": "The reconciliation request is inconsistent; "
            "no move instructions were generated.",
            "infeasibility_categories": exc.categories,
            "messages": exc.messages,
        },
    )


@app.exception_handler(StagingConflict)
async def staging_conflict_handler(request: Request, exc: StagingConflict) -> JSONResponse:
    # 409: closed loops cannot be broken without a usable staging well;
    # the loops are listed and no partial instruction list is emitted.
    return JSONResponse(
        status_code=409,
        content={
            "detail": "Closed-loop misplacements require a usable staging well; "
            "no move instructions were generated.",
            "cycles": exc.cycles,
            "staging_well": exc.staging_well,
            "problems": exc.problems,
        },
    )


@app.exception_handler(SpatialQCInvalid)
async def spatial_qc_invalid_handler(request: Request, exc: SpatialQCInvalid) -> JSONResponse:
    # 422: insufficient readings, too sparse a grid, or non-convergence;
    # no effects, corrected readings or p-value are ever produced.
    return JSONResponse(
        status_code=422,
        content={
            "detail": "The spatial QC request cannot be analysed; "
            "no partial conclusion was produced.",
            "infeasibility_categories": exc.categories,
            "messages": exc.messages,
        },
    )


@app.exception_handler(ReleaseInvalid)
async def release_invalid_handler(request: Request, exc: ReleaseInvalid) -> JSONResponse:
    # 422: the scan does not agree with the plan (missing/unplanned samples,
    # open/closed-loop misplacement, blocked-well occupancy...) or the
    # spatial QC input is structurally unanalysable. The problem categories
    # and the misplaced-well list are returned; no release verdict is given.
    return JSONResponse(status_code=422, content=release_invalid_body(exc))


@app.exception_handler(BlindInvalid)
async def blind_invalid_handler(request: Request, exc: BlindInvalid) -> JSONResponse:
    # 422 for every blinding/unblinding failure. Messages deliberately
    # contain only public, non-secret identifiers: plaintext shares, keys
    # and the correspondence are never echoed or logged.
    return JSONResponse(
        status_code=422,
        content={
            "detail": "The blinding request failed; "
            "no blind codes, mapping package or shares are produced "
            "(unblinding reveals no partial mapping).",
            "infeasibility_categories": exc.categories,
            "messages": exc.messages,
        },
    )


@app.exception_handler(DilutionPlanInvalid)
async def dilution_invalid_handler(request: Request, exc: DilutionPlanInvalid) -> JSONResponse:
    # 422 whenever any target well cannot be met (insufficient shared
    # source stock, well over capacity, concentration unreachable, no
    # pipette-grid solution within tolerance); no partial plan is emitted.
    return JSONResponse(
        status_code=422,
        content={
            "detail": "No dilution plan can satisfy every target well; "
            "no pipetting instructions were generated.",
            "infeasibility_categories": exc.categories,
            "messages": exc.messages,
        },
    )


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/api/v1/arrangements",
    summary="编排微孔板布局",
    responses={
        200: {"description": "返回最优布局、两级指标与约束校验结果"},
        422: {"description": "约束无解或请求非法,列出不可行约束类别,不返回部分布局"},
        503: {"description": "求解超时,不返回部分布局"},
    },
)
async def create_arrangement(req: ArrangeRequest) -> dict:
    layout = plan_layout(req)
    rows, cols = layout.rows, layout.columns

    # Level-1 metric: group balance over rows/columns.
    spreads = compute_spreads(layout, rows, cols)
    global_d = max(
        [s.row_spread for s in spreads] + [s.col_spread for s in spreads],
        default=0,
    )
    level1 = {
        "global_max_spread": global_d,
        "definition": "max over groups of max(row max-min, column max-min); empty rows/columns count as 0",
        "groups": [
            {
                "group": s.group,
                "row_counts": s.row_counts,
                "col_counts": s.col_counts,
                "row_max": s.row_max,
                "row_min": s.row_min,
                "row_spread": s.row_spread,
                "col_max": s.col_max,
                "col_min": s.col_min,
                "col_spread": s.col_spread,
            }
            for s in spreads
        ],
    }

    # Level-2 metric: same-batch orthogonal adjacency (recounted independently).
    pairs, total_edges = count_adjacent_pairs(req, layout.placement, rows, cols)
    level2 = {
        "same_batch_adjacent_pairs_count": len(pairs),
        "same_batch_adjacent_pairs": [list(p) for p in pairs],
        "total_orthogonal_edges": total_edges,
        "same_batch_ratio": round(len(pairs) / total_edges, 6) if total_edges else 0.0,
    }

    validation = verify_layout(req, layout, rows, cols)

    # Overall optimality is OPTIMAL only when both optimization stages
    # proved their objectives; the tie-break stage is always OPTIMAL.
    optimization_stages = [s for s in layout.stages if s.stage != "3_tiebreak"]
    overall_status = (
        "OPTIMAL" if all(s.proven_optimal for s in optimization_stages) else "FEASIBLE"
    )

    return {
        "seed": req.seed,
        "plate": {
            "type": req.plate.type,
            "rows": rows,
            "columns": cols,
            "blocked_wells": sorted(req.blocked_wells, key=lambda w: _well_sort_key(w, rows, cols)),
            "available_wells": layout.available_wells,
        },
        "layout": {sid: layout.placement[sid] for sid in sorted(layout.placement)},
        "metrics": {
            "level1_group_balance": level1,
            "level2_batch_adjacency": level2,
        },
        "validation": validation,
        "solver": {
            "status": overall_status,
            "workers": int(os.getenv("CP_NUM_WORKERS", "1")),
            "stages": [
                {
                    "stage": st.stage,
                    "objective_value": st.objective_value,
                    "status": st.status,
                    "proven_optimal": st.proven_optimal,
                    "wall_time_seconds": round(st.wall_time_seconds, 4),
                }
                for st in layout.stages
            ],
        },
    }


def _well_sort_key(name: str, rows: int, cols: int):
    try:
        r, c = parse_well(name, rows, cols)
        return (r, c)
    except ValueError:
        return (rows, cols, name)


@app.post(
    "/api/v1/arrangements/reconcile",
    summary="核对扫描板态并生成最少步数纠位指令",
    responses={
        200: {"description": "返回错孔清单、最少步数逐孔纠位指令与重放后的最终映射"},
        409: {"description": "存在闭环错位但未提供可用暂存孔,列出闭环,不输出部分指令"},
        422: {"description": "目标映射/扫描/禁用孔不一致,列出问题,不生成搬移指令"},
    },
)
async def reconcile_arrangement(req: ReconcileRequest) -> dict:
    return reconcile(req)


@app.post(
    "/api/v1/arrangements/spatial-qc",
    summary="空间效应质控:组内中心化 + 行列中位数剥离与置换检验",
    responses={
        200: {"description": "返回总体及逐行/逐列效应、校正读数、收敛轨迹与加一修正经验 p 值"},
        422: {"description": "组内有效读数不足、未覆盖至少2行2列或算法未收敛,不输出部分结论"},
    },
)
async def spatial_qc_arrangement(req: SpatialQCRequest) -> dict:
    return spatial_qc(req)


@app.post(
    "/api/v1/arrangements/batch-release",
    summary="批次放行核验:先核对扫描与计划完全一致,再以空间质控门控放行",
    responses={
        200: {"description": "扫描吻合且空间质控全部达标返回 RELEASED;否则返回 NOT_RELEASED 及原因,不输出部分结论"},
        422: {"description": "扫描与计划不一致(缺孔/计划外样本/错位/禁用孔占用)或请求非法,列出问题类别与错孔清单,不给放行结论"},
    },
)
async def batch_release_arrangement(req: BatchReleaseRequest) -> dict:
    return batch_release(req)


@app.post(
    "/api/v1/arrangements/dilution-plan",
    summary="为既有 layout 生成稀释梯度配制计划(确定性逐步移液指令)",
    responses={
        200: {"description": "返回逐步移液指令、每孔实际浓度与各来源样本用量/余量"},
        422: {"description": "来源库存不足、目标超容量或分辨率取整后无法达标,逐项列因,不输出部分计划"},
    },
)
async def create_dilution_plan(req: DilutionPlanRequest) -> dict:
    return dilution_plan(req)


@app.post(
    "/api/v1/blinding/blind",
    summary="双人协作盲化:为既有编排结果生成盲码、认证加密映射包与两份密钥份额",
    responses={
        200: {"description": "返回仅含 well 与 blind_id 的操作视图、映射包与两份独立份额"},
        422: {"description": "样本与孔位未一一对应或孔位非法;不生成盲码、映射包或份额"},
    },
)
async def blind_layout(req: BlindRequest) -> dict:
    return blind(req)


@app.post(
    "/api/v1/blinding/unblind",
    summary="双人协作解盲:批次+原操作视图+映射包+两份份额同时校验,一次性返回全量对应",
    responses={
        200: {"description": "一次性返回 blind_id、sample_id、well 全量对应"},
        422: {"description": "完整性、份额或批次不符;不泄露任何部分映射"},
    },
)
async def unblind_layout(req: UnblindRequest) -> dict:
    return unblind(req)


@app.post(
    "/api/v1/certificates",
    summary="为通过批次放行核验的批次建立证书(冻结快照,单一 SQLite 事务持久化)",
    responses={
        201: {"description": "放行通过(RELEASED),证书已建立,返回 certificate_id、status、revision 与 digest"},
        200: {"description": "放行门控未通过(NOT_RELEASED),不建立证书"},
        409: {"description": "批次已存在证书,或 request_key 曾被不同内容使用"},
        422: {"description": "扫描与计划不一致、请求非法或盲化视图非法,不建立证书"},
    },
)
async def create_batch_certificate(req: CertificateCreateRequest) -> JSONResponse:
    status, body = create_certificate(req)
    return JSONResponse(status_code=status, content=body)


@app.post(
    "/api/v1/certificates/reverify",
    summary="证书重核验:凭冻结快照恢复参数,以新扫描/读数复核;通过则追加新 revision 的不可变证据快照并重算 digest,否则吊销",
    responses={
        200: {"description": "复核通过返回递增后的 revision 与覆盖新证据快照的 digest;任何错位/质控失败/视图摘要变化返回 REVOKED 与首个失效原因"},
        404: {"description": "批次无证书"},
        409: {"description": "证书已吊销(不可恢复)、expected_revision 与当前 revision 不符,或 request_key 曾被不同内容使用"},
        422: {"description": "请求非法或盲化视图结构非法,证书保持不变"},
    },
)
async def reverify_batch_certificate(req: CertificateReverifyRequest) -> JSONResponse:
    status, body = reverify_certificate(req)
    return JSONResponse(status_code=status, content=body)
