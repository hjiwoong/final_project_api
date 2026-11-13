
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from core.security import get_current_user
from database.db_connection import get_db
from models.models import Notification, User

# 로그인한 본인의 알림만 조회 (관리자·학부모 공통)
router = APIRouter(prefix="/me/notifications", tags=["알림"])


def notification_to_dict(n: Notification) -> dict:
    return {
        "id": n.id,
        "alert_id": n.alert_id,
        "alert_level": n.alert.level.value if n.alert else None,
        "title": n.title,
        "body": n.body,
        "sent_at": n.sent_at,
        "read_at": n.read_at,
        "is_read": n.read_at is not None,

        # 신규: 알림 분류 및 운행 정보
        "trip_id": n.trip_id,
        "category": n.category,
        "event_type": n.event_type,
    }


@router.get("")
def my_notifications(
    unread_only: bool = False,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    me: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    base = db.query(Notification).filter(Notification.user_id == me.id)
    unread_count = base.filter(Notification.read_at.is_(None)).count()

    q = base.filter(Notification.read_at.is_(None)) if unread_only else base
    total = q.count()
    rows = q.order_by(Notification.id.desc()).offset(offset).limit(limit).all()

    return {
        "items": [notification_to_dict(n) for n in rows],
        "total": total,
        "unread_count": unread_count,
    }


@router.post("/{notification_id}/read")
def read_notification(
    notification_id: int,
    me: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    n = db.get(Notification, notification_id)
    if not n or n.user_id != me.id:
        # 남의 알림은 존재 여부도 숨김
        raise HTTPException(404, "알림을 찾을 수 없습니다")

    if n.read_at is None:
        n.read_at = datetime.now()
        db.commit()

    return {"id": n.id, "read_at": n.read_at}
