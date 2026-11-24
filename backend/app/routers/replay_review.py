"""区间重采复核领取接口：授权发布/更正、按负责测点读取、片段领取、部分提交与领用范围回查。

路由挂在 /api/range_replay/review 下，需在通用资源路由之前注册，
避免被 /api/{module}/{entry_id} 抢先匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services import service_for
from app.services.range_replay import RangeReplayService

router = APIRouter(prefix="/api/range_replay/review", tags=["区间重采复核"])


class PermissionPayload(BaseModel):
    reviewer: str
    points: list[str] = Field(default_factory=list)


class ScopePayload(BaseModel):
    approved_start: str
    approved_end: str
    note: str | None = None


class ClaimPayload(BaseModel):
    reviewer: str
    start: str
    end: str


class ReviewerPayload(BaseModel):
    reviewer: str


class SubmitPayload(BaseModel):
    reviewer: str
    points: list[str] = Field(default_factory=list)


def _service() -> RangeReplayService:
    service = service_for("range_replay")
    assert isinstance(service, RangeReplayService)
    return service


def _unwrap(result: dict[str, Any]) -> dict[str, Any]:
    """领域服务用 error/code 表达失败，这里映射成合适的 HTTP 状态码。"""
    if isinstance(result, dict) and "error" in result:
        status = {"forbidden": 403, "not_found": 404}.get(str(result.get("code")), 400)
        detail = {key: value for key, value in result.items() if key != "code"}
        raise HTTPException(status_code=status, detail=detail)
    return result


# -- 复核权限 --

@router.post("/permissions/grant")
def grant_permission(payload: PermissionPayload) -> dict[str, Any]:
    return _unwrap(_service().grant_review_permission(payload.reviewer, payload.points))


@router.post("/permissions/revoke")
def revoke_permission(payload: PermissionPayload) -> dict[str, Any]:
    return _unwrap(_service().revoke_review_permission(payload.reviewer, payload.points))


@router.get("/permissions")
def list_permissions(reviewer: str | None = Query(default=None)) -> dict[str, Any]:
    return _service().review_permissions(reviewer)


# -- 复核授权：发布与更正 --

@router.post("/{replay_id}/publish")
def publish_scope(replay_id: int, payload: ScopePayload) -> dict[str, Any]:
    return _unwrap(_service().publish_review_scope(
        replay_id, payload.approved_start, payload.approved_end, payload.note or ""))


@router.post("/{replay_id}/correct")
def correct_scope(replay_id: int, payload: ScopePayload) -> dict[str, Any]:
    return _unwrap(_service().correct_review_scope(
        replay_id, payload.approved_start, payload.approved_end, payload.note or ""))


@router.get("/{replay_id}/scope")
def current_scope(replay_id: int) -> dict[str, Any]:
    return _unwrap(_service().current_review_scope(replay_id))


# -- 按负责测点和时段读取重采数据 --

@router.get("/data")
def read_replay_data(
    point: str = Query(...),
    start: str = Query(...),
    end: str = Query(...),
    reviewer: str = Query(...),
) -> dict[str, Any]:
    return _unwrap(_service().read_replay_data(point, start, end, reviewer))


# -- 复核单领取与提交 --

@router.post("/{replay_id}/claim")
def claim_segment(replay_id: int, payload: ClaimPayload) -> dict[str, Any]:
    return _unwrap(_service().claim_review_segment(
        replay_id, payload.reviewer, payload.start, payload.end))


@router.get("/sheets")
def list_own_sheets(reviewer: str = Query(...)) -> dict[str, Any]:
    return {"items": _service().reviewer_sheets(reviewer)}


@router.get("/sheets/{sheet_id}")
def sheet_detail(sheet_id: str) -> dict[str, Any]:
    return _unwrap(_service().review_sheet_detail(sheet_id))


@router.post("/sheets/{sheet_id}/claim-next")
def claim_next_batch(sheet_id: str, payload: ReviewerPayload) -> dict[str, Any]:
    return _unwrap(_service().claim_next_batch(sheet_id, payload.reviewer))


@router.post("/sheets/{sheet_id}/submit")
def submit_points(sheet_id: str, payload: SubmitPayload) -> dict[str, Any]:
    return _unwrap(_service().submit_review_points(sheet_id, payload.reviewer, payload.points))


# -- 归属核对与报告回查 --

@router.get("/{replay_id}/ownership")
def segment_ownership(replay_id: int, points: str = Query(...)) -> dict[str, Any]:
    point_list = [item.strip() for item in points.split(",") if item.strip()]
    return _unwrap(_service().segment_ownership(replay_id, point_list))


@router.get("/reports/{report_id}/scope")
def report_claim_scope(report_id: int) -> dict[str, Any]:
    return _unwrap(_service().report_claim_scope(report_id))
