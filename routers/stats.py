from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.security import require_admin
from database.db_connection import get_db
from services.stats import compute_summary

router = APIRouter(prefix="/stats", tags=["통계"], dependencies=[Depends(require_admin)])


@router.get("/summary")
def summary(
    from_: datetime | None = Query(None, alias="from", description="예: 2026-10-13T00:00:00 (생략하면 전체 기간)"),
    to: datetime | None = Query(None, description="예: 2026-10-13T23:59:59"),
    db: Session = Depends(get_db),
):
    """운행 수, 경고 수, 평균 업로드→알림 지연(ms) + 일별 추이"""
    return compute_summary(db, from_, to)
