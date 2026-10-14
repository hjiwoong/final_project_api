"""통계 · KPI 계산

KPI '사진 업로드 후 알림 지연 시간' (목표: 평균 5초 이내)
  = 경고의 첫 알림 발송 시각 - 그 경고를 만든 불일치 이벤트의 서버 수신 시각
"""
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from models.models import (
    Alert, AlertLevel, AlertStatus, BoardingEvent, EventType, Notification,
    Trip, TripResult, TripStatus,
)

NOTIFY_DELAY_TARGET_MS = 5000


def _between(q, col, from_: datetime | None, to: datetime | None):
    if from_:
        q = q.filter(col >= from_)
    if to:
        q = q.filter(col <= to)
    return q


def notify_delays_ms(db: Session, alerts: list[Alert]) -> list[float]:
    """경고별 (첫 알림 발송 - 트리거 이벤트 수신) 지연(ms)"""
    if not alerts:
        return []
    first_sent = dict(
        db.query(Notification.alert_id, func.min(Notification.sent_at))
        .filter(Notification.alert_id.in_([a.id for a in alerts]))
        .group_by(Notification.alert_id)
        .all()
    )
    delays = []
    for a in alerts:
        sent = first_sent.get(a.id)
        if sent is None:
            continue
        # 첫 알림 직전에 만들어진 불일치 이벤트 = 알림을 유발한 사진
        trigger = (
            db.query(BoardingEvent.uploaded_at)
            .filter(
                BoardingEvent.trip_id == a.trip_id,
                BoardingEvent.event_type == EventType.MISMATCH,
                BoardingEvent.uploaded_at <= sent,
            )
            .order_by(BoardingEvent.uploaded_at.desc())
            .first()
        )
        if trigger is None or trigger[0] is None:
            continue
        delays.append((sent - trigger[0]).total_seconds() * 1000)
    return delays


def compute_summary(db: Session, from_: datetime | None, to: datetime | None) -> dict:
    trips = _between(db.query(Trip), Trip.started_at, from_, to).all()
    alerts = _between(db.query(Alert), Alert.created_at, from_, to).all()

    delays = notify_delays_ms(db, alerts)
    avg_ms = round(sum(delays) / len(delays), 1) if delays else None

    # 일별 추이 (경고 추이 차트용)
    daily: dict[str, dict] = {}
    for t in trips:
        d = t.started_at.date().isoformat()
        daily.setdefault(d, {"date": d, "trips": 0, "alerts": 0})["trips"] += 1
    for a in alerts:
        d = a.created_at.date().isoformat()
        daily.setdefault(d, {"date": d, "trips": 0, "alerts": 0})["alerts"] += 1

    return {
        "period": {"from": from_, "to": to},
        "trip_count": len(trips),
        "closed_count": sum(t.status == TripStatus.CLOSED for t in trips),
        "in_progress_count": sum(t.status == TripStatus.IN_PROGRESS for t in trips),
        "result_counts": {r.value: sum(t.result == r for t in trips) for r in TripResult},
        "alert_count": len(alerts),
        "alert_by_level": {l.value: sum(a.level == l for a in alerts) for l in AlertLevel},
        "alert_by_status": {s.value: sum(a.status == s for a in alerts) for s in AlertStatus},
        "avg_notify_delay_ms": avg_ms,
        "notify_delay": {
            "sample_count": len(delays),
            "avg_ms": avg_ms,
            "min_ms": round(min(delays), 1) if delays else None,
            "max_ms": round(max(delays), 1) if delays else None,
            "target_ms": NOTIFY_DELAY_TARGET_MS,
            "within_target": (avg_ms <= NOTIFY_DELAY_TARGET_MS) if avg_ms is not None else None,
        },
        "daily": [daily[k] for k in sorted(daily)],
    }
