from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response

from app.api.dependencies import current_principal
from app.core.pagination import Page, page_result
from app.core.security import Principal
from app.database import get_connection, transaction
from app.schemas.imports import ImportSubmitRequest, RowResolutionRequest
from app.services.imports import ResidentImportService

router = APIRouter(prefix="/api/resident-imports", tags=["居民导入"])


@router.post("", status_code=201)
def submit_import(
    data: ImportSubmitRequest,
    response: Response,
    principal: Principal = Depends(current_principal),
) -> dict:
    with transaction(immediate=True) as connection:
        summary, replayed = ResidentImportService(connection).submit(principal, data.model_dump())
    if replayed:
        response.status_code = 200
    return summary


@router.get("")
def list_imports(
    status: str | None = Query(default=None, pattern="^(pending_review|confirmed|superseded)$"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    principal: Principal = Depends(current_principal),
) -> dict:
    pagination = Page(page, size)
    total, rows = ResidentImportService(get_connection()).list_batches(
        principal, status=status, limit=pagination.size, offset=pagination.offset
    )
    return page_result(total=total, page=pagination, rows=rows)


@router.get("/{batch_id}")
def get_import(batch_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return ResidentImportService(get_connection()).detail(principal, batch_id)


@router.get("/{batch_id}/rows")
def get_import_rows(
    batch_id: int,
    status: str | None = Query(default=None, pattern="^(valid|conflict|error|written|skipped)$"),
    resolution: str | None = Query(default=None, pattern="^(pending|insert|update|skip)$"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    principal: Principal = Depends(current_principal),
) -> dict:
    pagination = Page(page, size)
    total, rows = ResidentImportService(get_connection()).list_rows(
        principal, batch_id, status=status, resolution=resolution, limit=pagination.size, offset=pagination.offset
    )
    return page_result(total=total, page=pagination, rows=rows)


@router.post("/{batch_id}/recheck")
def recheck_import(batch_id: int, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return ResidentImportService(connection).recheck(principal, batch_id)


@router.post("/{batch_id}/rows/{row_id}/resolution")
def resolve_row(
    batch_id: int,
    row_id: int,
    data: RowResolutionRequest,
    principal: Principal = Depends(current_principal),
) -> dict:
    with transaction(immediate=True) as connection:
        return ResidentImportService(connection).resolve_row(principal, batch_id, row_id, data.resolution)


@router.post("/{batch_id}/confirm")
def confirm_import(batch_id: int, principal: Principal = Depends(current_principal)) -> dict:
    # confirm 需要"先落库冲突标记再返回 409"，由服务自行管理事务边界
    return ResidentImportService(get_connection()).confirm(principal, batch_id)
