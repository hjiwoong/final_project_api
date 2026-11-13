
"""운행 예정 시각 10분 초과 시 학부모 지연 알림 생성."""

import logging
from datetime import datetime, timedelta

from database.db_connection import SessionLocal
from models.models import (
    Bus, BusStudent, Guardian, Notification, TripSchedule,
)
from services import notifier

logger = logging.getLogger(__name__)

DELAY_MINUTES = 10


def check_delayed_trips() -> None:
    """서버 스케줄러가 1분마다 호출한다."""
    now = datetime.now()
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
                # 해당 시간표에 연결된 운행이 시작되었는지 확인
                if schedule.trip_id is not None:
                    schedule.delayed_notified_at = now
                    continue

                bus = db.get(Bus, schedule.bus_id)
                bus_name = bus.name if bus else f"{schedule.bus_id}번 버스"

                parent_ids = {
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

                for user_id in parent_ids:
                    notification = Notification(
                        user_id=user_id,
                        alert_id=None,
                        trip_id=None,
                        category="TRIP",
                        event_type="DELAYED",
                        title="운행 시작 지연 알림",
                        body=(
                            f"{bus_name}의 "
                            f"{schedule.scheduled_at:%H:%M} 예정 운행이 "
                            f"10분 이상 시작되지 않았습니다."
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
