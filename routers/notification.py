
from datetime import datetime
from enum import Enum

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload

from core.security import require_terms_agreed
from core.time import KST, now_kst_naive
from database.db_connection import get_db
from models.models import Alert, Notification, Trip, User

# 로그인한 본인의 알림만 조회 (관리자·학부모 공통)
router = APIRouter(prefix="/me/notifications", tags=["알림"])


class NotificationCategory(str, Enum):
    URGENT = "URGENT"
    CHECK = "CHECK"
    TRIP = "TRIP"
    ATTENDANCE = "ATTENDANCE"


def kst_isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=KST, microsecond=0).isoformat()


def notification_to_dict(n: Notification) -> dict:
    trip = n.trip or (n.alert.trip if n.alert else None)
    return {
        "id": n.id,
        "alert_id": n.alert_id,
        "alert_level": n.alert.level.value if n.alert else None,
        "title": n.title,
        "body": n.body,
        "sent_at": kst_isoformat(n.sent_at),
        "read_at": kst_isoformat(n.read_at),
        "is_read": n.read_at is not None,

        # 신규: 알림 분류 및 운행 정보
        "trip_id": n.trip_id,
        "bus_name": trip.bus.name if trip and trip.bus else None,
        "category": n.category,
        "event_type": n.event_type,
    }


@router.get("")
def my_notifications(
    unread_only: bool = False,
    category: NotificationCategory | None = None,
    event_type: str | None = Query(None, max_length=30),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    me: User = Depends(require_terms_agreed),
    db: Session = Depends(get_db),
):
    base = db.query(Notification).filter(Notification.user_id == me.id)
    unread_count = base.filter(Notification.read_at.is_(None)).count()

    q = base
    if category:
        q = q.filter(Notification.category == category)
    if event_type:
        q = q.filter(Notification.event_type == event_type)
    if unread_only:
        q = q.filter(Notification.read_at.is_(None))

    total = q.count()
    rows = (
        q.options(
            joinedload(Notification.trip).joinedload(Trip.bus),
            joinedload(Notification.alert).joinedload(Alert.trip).joinedload(Trip.bus),
        )
        .order_by(Notification.sent_at.desc(), Notification.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return {
        "items": [notification_to_dict(n) for n in rows],
        "total": total,
        "unread_count": unread_count,
    }


@router.post("/{notification_id}/read")
def read_notification(
    notification_id: int,
    me: User = Depends(require_terms_agreed),
    db: Session = Depends(get_db),
):
    n = db.get(Notification, notification_id)
    if not n or n.user_id != me.id:
        # 남의 알림은 존재 여부도 숨김
        raise HTTPException(404, "알림을 찾을 수 없습니다")

    if n.read_at is None:
        n.read_at = now_kst_naive()
        db.commit()

    return {"id": n.id, "read_at": n.read_at}


@router.post("/read-all")
def read_all_notifications(
    me: User = Depends(require_terms_agreed),
    db: Session = Depends(get_db),
):
    updated = (
        db.query(Notification)
        .filter(Notification.user_id == me.id, Notification.read_at.is_(None))
        .update({Notification.read_at: now_kst_naive()}, synchronize_session=False)
    )
    db.commit()
    return {"updated": updated}
