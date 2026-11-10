"""경고(alert) 생성 + 수신자별 알림(notification) 기록

수신자
- 관리자(ADMIN) 전체
- 학부모: URGENT(미하차 의심)일 때만, 해당 버스 탑승 예정(오늘 결석 제외) 학생의 보호자
LLM 요약은 services/llm_service.py, Agent 판단은 services/agent_service.py
TODO: FCM 푸시 발송
"""
from datetime import date, datetime

from sqlalchemy.orm import Session

from models.models import (
    Alert, AlertLevel, AlertStatus, Attendance, BoardingEvent, BusStudent,
    Guardian, Notification, Trip, User, UserRole,
)


def build_summary(boarding: int, remaining: int, level: AlertLevel) -> str:
    # 경고를 만들 때 즉시 쓰는 템플릿 요약.
    # 응답 후 llm_service.enrich_alert_summary 가 LLM 문장을 덧붙여 갱신한다. (첫 줄은 이 문장과 같음)
    if level == AlertLevel.URGENT:
        return f"하차 후 {remaining}명 잔류 의심 · 승차 {boarding} / 잔류 {remaining}"
    return "사진의 검출 신뢰도가 낮아 재촬영 또는 관리자 확인이 필요합니다"


def open_alerts(db: Session, trip_id: int) -> list[Alert]:
    return (
        db.query(Alert)
        .filter(Alert.trip_id == trip_id, Alert.status != AlertStatus.RESOLVED)
        .all()
    )


def raise_alert(
    db: Session, trip: Trip, event: BoardingEvent, level: AlertLevel,
    message: str, boarding: int, remaining: int,
) -> Alert:
    """열린 경고가 있으면 갱신(재촬영), 없으면 새로 만들고 알림 발송"""
    summary = build_summary(boarding, remaining, level)
    existing = open_alerts(db, trip.id)
    if existing:
        a = existing[0]
        a.event_id, a.level, a.message, a.llm_summary = event.id, level, message, summary
        db.flush()
        return a

    a = Alert(trip_id=trip.id, event_id=event.id, level=level,
              message=message, llm_summary=summary)
    db.add(a)
    db.flush()
    _notify(db, a, trip, level)
    return a


def resolve_open_alerts(db: Session, trip_id: int, note: str) -> list[Alert]:
    alerts = open_alerts(db, trip_id)
    for a in alerts:
        a.status = AlertStatus.RESOLVED
        a.note = note
        a.resolved_at = datetime.now()
    return alerts


def _notify(db: Session, alert: Alert, trip: Trip, level: AlertLevel) -> None:
    bus_name = trip.bus.name if trip.bus else f"{trip.bus_id}번 버스"
    title = ("🚨 " if level == AlertLevel.URGENT else "⚠️ ") + (alert.message or "경고")
    body = f"{bus_name} · {alert.llm_summary}"

    user_ids = {u.id for u in db.query(User).filter(User.role == UserRole.ADMIN).all()}
    if level == AlertLevel.URGENT:
        # 오늘 결석으로 설정한 학생은 버스에 타지 않았으므로 제외
        absent_ids = db.query(Attendance.student_id).filter(
            Attendance.date == date.today(), Attendance.is_absent.is_(True)
        )
        parents = (
            db.query(Guardian.user_id)
            .join(BusStudent, BusStudent.student_id == Guardian.student_id)
            .filter(BusStudent.bus_id == trip.bus_id,
                    Guardian.student_id.notin_(absent_ids))
            .all()
        )
        user_ids |= {p[0] for p in parents}

    for uid in user_ids:
        db.add(Notification(alert_id=alert.id, user_id=uid, title=title, body=body))
    # TODO: users.push_token 으로 FCM 푸시 발송
