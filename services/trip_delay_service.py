
"""운행 예정 시각 5분 초과 시 학부모·관리자 미출발 알림 생성."""

import logging
from datetime import datetime, timedelta

from core.time import now_kst_naive
from database.db_connection import SessionLocal
from models.models import (
    Bus, BusStudent, Guardian, Notification, Trip, TripSchedule, User, UserRole,
)
from services import notifier

logger = logging.getLogger(__name__)

DELAY_MINUTES = 5


def check_delayed_trips(now: datetime | None = None) -> None:
    """서버 스케줄러가 1분마다 호출한다."""
    now = now or now_kst_naive()
    push_ids = []

    try:
        with SessionLocal() as db:
            # 같은 스케줄을 중복 처리하지 않도록 행 잠금
            schedules = (
                db.query(TripSchedule)
                .filter(
                    TripSchedule.scheduled_at <= now - timedelta(minutes=DELAY_MINUTES),
                    TripSchedule.delayed_notified_at.is_(None),
                    TripSchedule.service_date == now.date(),
                )
                .order_by(TripSchedule.id)
                .with_for_update(skip_locked=True)
                .all()
            )

            for schedule in schedules:
                departure = (
                    db.get(Trip, schedule.trip_id)
                    if schedule.trip_id is not None
                    else None
                )
                if departure is None:
                    departure = (
                        db.query(Trip)
                        .filter(
                            Trip.bus_id == schedule.bus_id,
                            Trip.started_at
                            >= schedule.scheduled_at
                            - timedelta(minutes=DELAY_MINUTES),
                            Trip.started_at
                            <= schedule.scheduled_at
                            + timedelta(minutes=DELAY_MINUTES),
                        )
                        .order_by(Trip.started_at)
                        .first()
                    )
                if departure is not None and departure.started_at is not None:
                    if departure.started_at <= schedule.scheduled_at + timedelta(
                        minutes=DELAY_MINUTES
                    ):
                        schedule.delayed_notified_at = now
                        continue

                if schedule.trip_id is not None:
                    if departure is None:
                        schedule.delayed_notified_at = now
                        continue

                bus = db.get(Bus, schedule.bus_id)
                bus_name = bus.name if bus else f"{schedule.bus_id}번 버스"

                user_ids = {
                    user_id
                    for (user_id,) in (
                        db.query(Guardian.user_id)
                        .join(
                            BusStudent,
                            BusStudent.student_id == Guardian.student_id,
                        )
                        .filter(BusStudent.bus_id == schedule.bus_id)
                        .all()
                    )
                }
                user_ids.update(
                    user_id
                    for (user_id,) in db.query(User.id)
                    .filter(User.role == UserRole.ADMIN)
                    .all()
                )

                for user_id in user_ids:
                    notification = Notification(
                        user_id=user_id,
                        alert_id=None,
                        trip_id=None,
                        category="TRIP",
                        event_type="NOT_STARTED",
                        title="버스 미출발 알림",
                        body=(
                            f"{bus_name}의 "
                            f"{schedule.scheduled_at:%H:%M} 예정 운행이 "
                            f"5분 이상 지났으나 출발이 확인되지 않았습니다."
                        ),
                    )
                    db.add(notification)
                    db.flush()
                    push_ids.append(notification.id)

                schedule.delayed_notified_at = now

            db.commit()

        if push_ids:
            notifier.send_trip_pushes(push_ids)
            logger.info("운행 지연 알림 %d건 처리", len(push_ids))

    except Exception:
        logger.exception("운행 시작 지연 자동 확인 실패")
