from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import exists, func
from sqlalchemy.orm import Session

from core.security import require_admin, require_parent
from database.db_connection import get_db
from models.models import (
    Alert, AlertLevel, AlertStatus, Attendance, BusStudent, Guardian, Trip, User,
)
from services import event_bus
from services.serializers import (
    alert_to_dict, alert_to_parent_dict, event_to_dict, trip_to_dict,
)

router = APIRouter(tags=["경고"])


class ResolveBody(BaseModel):
    note: str | None = None
    safety_checked: bool = False
    simulation_mode: bool = False


def get_alert_or_404(db: Session, alert_id: int) -> Alert:
    a = db.get(Alert, alert_id)
    if not a:
        raise HTTPException(404, "경고를 찾을 수 없습니다")
    return a


# ================= 관리자 =================
@router.get("/alerts", dependencies=[Depends(require_admin)])
def list_alerts(
    status: AlertStatus | None = None,
    level: AlertLevel | None = None,
    simulation_mode: bool | None = None,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    q = db.query(Alert)
    if status:
        q = q.filter(Alert.status == status)
    if level:
        q = q.filter(Alert.level == level)
    if simulation_mode is not None:
        q = q.join(Trip, Alert.trip_id == Trip.id).filter(Trip.simulation_mode == simulation_mode)
    total = q.count()
    rows = q.order_by(Alert.id.desc()).offset(offset).limit(limit).all()
    return {"items": [alert_to_dict(a) for a in rows], "total": total}


@router.get("/alerts/{alert_id}", dependencies=[Depends(require_admin)])
def get_alert(alert_id: int, db: Session = Depends(get_db)):
    """경고 상세: llm_summary + 연결된 운행/이벤트"""
    a = get_alert_or_404(db, alert_id)
    return {
        **alert_to_dict(a),
        "trip": trip_to_dict(a.trip),
        "event": event_to_dict(a.event, with_detections=True) if a.event else None,
    }


@router.post("/alerts/{alert_id}/ack", dependencies=[Depends(require_admin)])
def ack_alert(alert_id: int, db: Session = Depends(get_db)):
    """OPEN -> ACKED (경고 확인)"""
    a = get_alert_or_404(db, alert_id)
    if a.status == AlertStatus.RESOLVED:
        raise HTTPException(409, "이미 해결된 경고입니다")
    if a.status == AlertStatus.OPEN:
        a.status = AlertStatus.ACKED
        a.acked_at = datetime.now()
        db.commit()
        db.refresh(a)
        event_bus.publish("alert", alert_to_dict(a))
    return alert_to_dict(a)


@router.post("/alerts/{alert_id}/resolve", dependencies=[Depends(require_admin)])
def resolve_alert(alert_id: int, body: ResolveBody, db: Session = Depends(get_db)):
    """ACKED(또는 OPEN) -> RESOLVED, 조치 메모 기록"""
    a = get_alert_or_404(db, alert_id)

    if body.simulation_mode and not a.trip.simulation_mode:
        raise HTTPException(
            status_code=400,
            detail="시뮬레이션 운행의 경고만 시뮬레이션 방식으로 해결할 수 있습니다."
        )

    if a.trip.simulation_mode and body.safety_checked:
        raise HTTPException(
            status_code=400,
            detail="시뮬레이션 운행의 경고는 실제 안전 확인 완료로 처리할 수 없습니다."
        )

    if not body.safety_checked and not body.simulation_mode:
        raise HTTPException(
            status_code=400,
            detail="차량 내부 안전 확인 후 경고를 해결할 수 있습니다."
        )

    if a.status == AlertStatus.RESOLVED:
        raise HTTPException(409, "이미 해결된 경고입니다")
    now = datetime.now()
    a.status = AlertStatus.RESOLVED
    a.note = body.note
    a.acked_at = a.acked_at or now
    a.resolved_at = now
    db.commit()
    db.refresh(a)
    event_bus.publish("alert", alert_to_dict(a))
    return alert_to_dict(a)


# ================= 학부모 =================
def _parent_alert_query(db: Session, me: User):
    """내 아이가 배정된 버스의 긴급 경고 (그날 결석 설정한 아이만 있는 경우는 제외)"""
    was_absent = exists().where(
        Attendance.student_id == BusStudent.student_id,
        Attendance.date == func.date(Trip.started_at),
        Attendance.is_absent.is_(True),
    )
    return (
        db.query(Alert)
        .join(Trip, Trip.id == Alert.trip_id)
        .join(BusStudent, BusStudent.bus_id == Trip.bus_id)
        .join(Guardian, Guardian.student_id == BusStudent.student_id)
        .filter(Guardian.user_id == me.id, Alert.level == AlertLevel.URGENT, ~was_absent)
        .distinct()
    )


@router.get("/me/alerts")
def my_alerts(
    status: AlertStatus | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    me: User = Depends(require_parent),
    db: Session = Depends(get_db),
):
    q = _parent_alert_query(db, me)
    if status:
        q = q.filter(Alert.status == status)
    total = q.count()
    rows = q.order_by(Alert.id.desc()).offset(offset).limit(limit).all()
    return {"items": [alert_to_parent_dict(a) for a in rows], "total": total}


@router.get("/me/alerts/{alert_id}")
def my_alert_detail(
    alert_id: int,
    me: User = Depends(require_parent),
    db: Session = Depends(get_db),
):
    """학부모 경고 상세 - 내 아이 버스의 경고가 아니면 404"""
    a = _parent_alert_query(db, me).filter(Alert.id == alert_id).first()
    if not a:
        raise HTTPException(404, "경고를 찾을 수 없습니다")
    return alert_to_parent_dict(a)
