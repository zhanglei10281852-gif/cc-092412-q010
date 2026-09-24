from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response

from app.api.dependencies import current_principal
from app.core.errors import ConflictError
from app.core.pagination import Page
from app.core.security import Principal
from app.database import get_connection, transaction
from app.schemas.imports import ImportBatchSubmit, ImportDecisionsRequest
from app.services.imports import ResidentImportService

router = APIRouter(prefix="/api/imports", tags=["居民导入"])


@router.post("", status_code=201)
def submit_batch(data: ImportBatchSubmit, response: Response, principal: Principal = Depends(current_principal)) -> dict:
    """接收结构化批次并完成字段、证件唯一性与批内关联校验；相同文件与参数重复提交返回原批次。"""
    with transaction(immediate=True) as connection:
        result = ResidentImportService(connection).submit(principal, data)
    if result["replayed"]:
        response.status_code = 200
    return result


@router.get("")
def list_batches(
    status: str | None = None,
    source_key: str | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    principal: Principal = Depends(current_principal),
) -> dict:
    return ResidentImportService(get_connection()).list_batches(
        principal, status=status, source_key=source_key, page=Page(page, size)
    )


@router.get("/{batch_id}")
def batch_detail(batch_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return ResidentImportService(get_connection()).detail(principal, batch_id)


@router.get("/{batch_id}/rows")
def list_rows(
    batch_id: int,
    status: str | None = None,
    decision: str | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    principal: Principal = Depends(current_principal),
) -> dict:
    """查询批次行：失败行（status=invalid）、冲突差异、覆盖决定均可在此检索。"""
    return ResidentImportService(get_connection()).list_rows(
        principal, batch_id, status=status, decision=decision, page=Page(page, size)
    )


@router.get("/{batch_id}/writes")
def list_writes(batch_id: int, principal: Principal = Depends(current_principal)) -> dict:
    """查询确认后的最终写入结果。"""
    return ResidentImportService(get_connection()).list_writes(principal, batch_id)


@router.post("/{batch_id}/decisions")
def record_decisions(batch_id: int, data: ImportDecisionsRequest, principal: Principal = Depends(current_principal)) -> dict:
    """审核人员对冲突行登记覆盖或跳过决定。"""
    with transaction(immediate=True) as connection:
        return ResidentImportService(connection).record_decisions(principal, batch_id, data.decisions)


@router.post("/{batch_id}/confirm")
def confirm_batch(batch_id: int, principal: Principal = Depends(current_principal)) -> dict:
    """由另一名有权限人员确认入库：整批写入，失败则完整回滚。"""
    with transaction(immediate=True) as connection:
        outcome = ResidentImportService(connection).confirm(principal, batch_id)
    if outcome["outcome"] == "refreshed":
        raise ConflictError(
            "确认期间居民数据发生变化，相关行已重新标记为待处理",
            context={"batch_id": batch_id, "refreshed_rows": outcome["refreshed_rows"]},
        )
    return outcome
