import os
from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from core.time import now_kst_naive
from models.models import BusStudent, Trip, TripSchedule


ATTENDANCE_DEADLINE_MINUTES = int(os.getenv("ATTENDANCE_DEADLINE_MINUTES", "30"))


def get_deadline(
    db: Session, student_id: int, attendance_date: date
) -> tuple[datetime | None, bool]:
    """Return the earliest assigned-bus deadline and whether attendance is editable."""
    now = now_kst_naive()
    if attendance_date < now.date():
        return None, False

    start = datetime.combine(attendance_date, time.min)
    end = start + timedelta(days=1)
    bus_ids = [
        bus_id
        for (bus_id,) in db.query(BusStudent.bus_id)
        .filter(BusStudent.student_id == student_id)
        .all()
    ]

    schedules = []
    started = False
    if bus_ids:
        schedules = (
            db.query(TripSchedule)
            .filter(
                TripSchedule.bus_id.in_(bus_ids),
                TripSchedule.service_date == attendance_date,
            )
            .all()
        )
        started = any(schedule.trip_id is not None for schedule in schedules)
        if not started:
            started = (
                db.query(Trip.id)
                .filter(
                    Trip.bus_id.in_(bus_ids),
                    Trip.started_at >= start,
                    Trip.started_at < end,
                    Trip.simulation_mode.is_(False),
                )
                .first()
                is not None
            )

    scheduled_at = min(
        (schedule.scheduled_at for schedule in schedules), default=None
    )
    deadline_at = (
        scheduled_at - timedelta(minutes=ATTENDANCE_DEADLINE_MINUTES)
        if scheduled_at is not None
        else None
    )
    editable = (
        not started
        and (deadline_at is None or now < deadline_at)
    )
    return deadline_at, editable
