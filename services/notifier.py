"""경고(alert) 생성 + 수신자별 알림(notification) 기록

수신자
- 관리자(ADMIN) 전체
- 학부모: URGENT(미하차 의심)일 때만, 해당 버스 탑승 예정(오늘 결석 제외) 학생의 보호자
TODO: LLM 요약 / Agent 발령 판단 / RAG 연동
"""
import logging
import os
from datetime import date, datetime

from sqlalchemy.orm import Session

from database.db_connection import SessionLocal
from models.models import (
    Alert, AlertLevel, AlertStatus, Attendance, BoardingEvent, BusStudent,
    Guardian, Notification, Trip, User, UserRole,
)

logger = logging.getLogger(__name__)


def build_summary(boarding: int, remaining: int, level: AlertLevel) -> str:
    # TODO: 멀티모달 LLM 요약으로 교체
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


def _firebase_messaging():
    credentials_path = (
        os.getenv("FIREBASE_SERVICE_ACCOUNT_FILE")
        or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        or r"D:\secrets\final-project-b66de-firebase-adminsdk-fbsvc-c2cb0dc867.json"
    )

    from firebase_admin import credentials, get_app, initialize_app, messaging
    from firebase_admin.exceptions import FirebaseError

    try:
        try:
            app = get_app()
        except ValueError:
            app = initialize_app(credentials.Certificate(credentials_path))
        return messaging, app
    except (FirebaseError, OSError, ValueError):
        logger.exception("Could not initialize Firebase Admin for FCM")
        return None


def send_alert_pushes(alert_id: int) -> None:
    """Deliver committed alert notifications through FCM after the API response."""
    firebase = _firebase_messaging()
    if firebase is None:
        return

    from firebase_admin.exceptions import FirebaseError

    messaging, app = firebase
    with SessionLocal() as db:
        targets = (
            db.query(Notification.user_id, Notification.title, Notification.body, User.push_token)
            .join(User, User.id == Notification.user_id)
            .filter(Notification.alert_id == alert_id, User.push_token.isnot(None))
            .all()
        )

    invalid_tokens: list[tuple[int, str]] = []
    for user_id, title, body, token in targets:
        try:
            messaging.send(
                messaging.Message(
                    token=token,
                    notification=messaging.Notification(title=title, body=body or ""),
                    data={"type": "safety_alert", "alert_id": str(alert_id)},
                    android=messaging.AndroidConfig(priority="high"),
                    apns=messaging.APNSConfig(
                        headers={"apns-priority": "10"},
                        payload=messaging.APNSPayload(
                            aps=messaging.Aps(sound="default"),
                        ),
                    ),
                ),
                app=app,
            )
        except FirebaseError as error:
            logger.exception(
                "FCM delivery failed for user_id=%s alert_id=%s",
                user_id,
                alert_id,
            )
            if getattr(error, "code", None) in {
                "messaging/registration-token-not-registered",
                "messaging/invalid-registration-token",
            }:
                invalid_tokens.append((user_id, token))

    if invalid_tokens:
        with SessionLocal() as db:
            for user_id, token in invalid_tokens:
                db.query(User).filter(
                    User.id == user_id,
                    User.push_token == token,
                ).update({"push_token": None}, synchronize_session=False)
            db.commit()
