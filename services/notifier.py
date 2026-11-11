"""경고(alert) 생성 + 수신자별 알림(notification) 및 푸시 전송

수신자
- 관리자(ADMIN) 전체
- 학부모: URGENT(미하차 의심)일 때만, 해당 버스 탑승 예정(오늘 결석 제외) 학생의 보호자
- URGENT 경고는 응답 후 send_alert_pushes 가 수신자의 모든 기기(push_tokens)로 FCM 푸시 발송
  (실패해도 알림 기록은 유지, 만료된 토큰은 자동 삭제)
LLM 요약은 services/llm_service.py, Agent 판단은 services/agent_service.py
"""
import logging
import os
from datetime import date, datetime

from sqlalchemy.orm import Session

from database.db_connection import SessionLocal
from models.models import (
    Alert, AlertLevel, AlertStatus, Attendance, BoardingEvent, BusStudent,
    Guardian, Notification, PushToken, Trip, User, UserRole,
)

logger = logging.getLogger(__name__)


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
        escalated = a.level != AlertLevel.URGENT and level == AlertLevel.URGENT
        a.event_id, a.level, a.message, a.llm_summary = event.id, level, message, summary
        db.flush()
        if escalated:
            # 긴급으로 올라가면 아직 알림을 못 받은 보호자에게 알림 추가 (관리자 중복 없음)
            _notify(db, a, trip, level)
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

    already_notified = {
        row[0]
        for row in db.query(Notification.user_id).filter(
            Notification.alert_id == alert.id
        )
    }
    for uid in user_ids - already_notified:
        db.add(Notification(alert_id=alert.id, user_id=uid, title=title, body=body))


ANDROID_CHANNEL_ID = os.getenv("FCM_ANDROID_CHANNEL_ID", "safety_alerts")


def _credentials_path() -> str:
    return (
        os.getenv("FIREBASE_SERVICE_ACCOUNT_FILE")
        or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        or r"D:\secrets\final-project-b66de-firebase-adminsdk-fbsvc-c2cb0dc867.json"
    )


def push_status() -> dict:
    """이 서버의 FCM 설정 상태를 헬스 응답에 제공한다."""
    path = _credentials_path()
    try:
        import firebase_admin  # noqa: F401
        installed = True
    except ImportError:
        installed = False
    return {
        "fcm_installed": installed,
        "fcm_credentials_found": os.path.isfile(path),
        "fcm_credentials_from_env": bool(
            os.getenv("FIREBASE_SERVICE_ACCOUNT_FILE")
            or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        ),
        "android_channel": ANDROID_CHANNEL_ID,
    }


def _firebase_messaging():
    credentials_path = _credentials_path()
    try:
        from firebase_admin import credentials, get_app, initialize_app, messaging
        from firebase_admin.exceptions import FirebaseError
    except ImportError:
        logger.warning("firebase-admin 이 설치되지 않아 FCM 푸시를 건너뜁니다")
        return None

    try:
        try:
            app = get_app()
        except ValueError:
            if not os.path.isfile(credentials_path):
                logger.error("FCM 서비스 계정 키 파일이 없습니다: %s", credentials_path)
                return None
            app = initialize_app(credentials.Certificate(credentials_path))
        return messaging, app
    except (FirebaseError, OSError, ValueError):
        logger.exception("Firebase Admin 초기화 실패")
        return None


def _push_targets(db: Session, alert_id: int) -> list[tuple[int, str, str, str]]:
    rows = (
        db.query(Notification.user_id, Notification.title, Notification.body)
        .filter(Notification.alert_id == alert_id)
        .all()
    )
    if not rows:
        return []
    user_ids = {row[0] for row in rows}
    tokens: dict[int, set[str]] = {}
    for user_id, token in db.query(PushToken.user_id, PushToken.token).filter(
        PushToken.user_id.in_(user_ids)
    ):
        tokens.setdefault(user_id, set()).add(token)
    for user_id, token in db.query(User.id, User.push_token).filter(
        User.id.in_(user_ids), User.push_token.isnot(None)
    ):
        tokens.setdefault(user_id, set()).add(token)

    parents = {
        user_id
        for (user_id,) in db.query(User.id).filter(
            User.id.in_(user_ids), User.role == UserRole.PARENT
        )
    }
    targets, seen = [], set()
    for user_id, title, body in sorted(rows, key=lambda row: row[0] not in parents):
        for token in sorted(tokens.get(user_id, ())):
            if token not in seen:
                seen.add(token)
                targets.append((user_id, title, body or "", token))
    return targets


def send_alert_pushes(alert_id: int) -> None:
    """DB 에 커밋된 경고 알림을 기기별 FCM 토큰으로 전송한다."""
    firebase = _firebase_messaging()
    if firebase is None:
        return

    from firebase_admin.exceptions import FirebaseError

    messaging, app = firebase
    with SessionLocal() as db:
        targets = _push_targets(db, alert_id)
    if not targets:
        logger.info("alert_id=%s: 등록된 FCM 수신 기기가 없습니다", alert_id)
        return

    invalid_tokens = []
    sent = 0
    for user_id, title, body, token in targets:
        try:
            messaging.send(
                messaging.Message(
                    token=token,
                    notification=messaging.Notification(title=title, body=body),
                    data={"type": "safety_alert", "alert_id": str(alert_id)},
                    android=messaging.AndroidConfig(
                        priority="high",
                        ttl=3600,
                        notification=messaging.AndroidNotification(
                            channel_id=ANDROID_CHANNEL_ID,
                            sound="default",
                            priority="max",
                            visibility="public",
                            tag=f"alert-{alert_id}",
                        ),
                    ),
                    apns=messaging.APNSConfig(
                        headers={"apns-priority": "10"},
                        payload=messaging.APNSPayload(
                            aps=messaging.Aps(sound="default")
                        ),
                    ),
                ),
                app=app,
            )
            sent += 1
        except FirebaseError as error:
            logger.exception(
                "FCM 전송 실패: user_id=%s alert_id=%s", user_id, alert_id
            )
            if isinstance(error, messaging.UnregisteredError) or getattr(
                error, "code", None
            ) in {
                "NOT_FOUND",
                "messaging/registration-token-not-registered",
                "messaging/invalid-registration-token",
            }:
                invalid_tokens.append(token)
    logger.info("alert_id=%s: FCM 푸시 %d/%d건 발송", alert_id, sent, len(targets))

    if invalid_tokens:
        with SessionLocal() as db:
            db.query(PushToken).filter(
                PushToken.token.in_(invalid_tokens)
            ).delete(synchronize_session=False)
            db.query(User).filter(User.push_token.in_(invalid_tokens)).update(
                {"push_token": None}, synchronize_session=False
            )
            db.commit()