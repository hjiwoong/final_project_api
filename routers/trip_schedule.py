
"""날짜별 운행 시간표 등록 및 조회 API."""

from datetime import date, datetime, time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database.db_connection import get_db
from models.models import Bus, TripSchedule, User
from core.security import require_admin

router = APIRouter(prefix="/trip-schedules", tags=["Trip Schedules"])


class TripScheduleCreate(BaseModel):
    bus_id: int
    service_date: date
    start_time: time


@router.post("", status_code=201)
def create_trip_schedule(
    body: TripScheduleCreate,
    me: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    bus = db.get(Bus, body.bus_id)

    if bus is None:
        raise HTTPException(status_code=404, detail="버스를 찾을 수 없습니다.")

    scheduled_at = datetime.combine(body.service_date, body.start_time)

    if scheduled_at <= datetime.now():
        raise HTTPException(
            status_code=400,
            detail="현재 시각 이후의 운행 시간만 등록할 수 있습니다.",
        )

    existing = (
        db.query(TripSchedule)
        .filter(
            TripSchedule.bus_id == body.bus_id,
            TripSchedule.service_date == body.service_date,
        )
        .first()
    )

    if existing:
        raise HTTPException(
            status_code=409,
            detail="해당 날짜에 이미 등록된 운행 시간표가 있습니다.",
        )

    schedule = TripSchedule(
        bus_id=body.bus_id,
        service_date=body.service_date,
        scheduled_at=scheduled_at,
    )

    db.add(schedule)
    db.commit()
    db.refresh(schedule)

    return {
        "id": schedule.id,
        "bus_id": schedule.bus_id,
        "service_date": schedule.service_date,
        "scheduled_at": schedule.scheduled_at,
        "delayed_notified_at": schedule.delayed_notified_at,
    }


@router.get("")
def list_trip_schedules(
    service_date: date | None = None,
    me: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    query = db.query(TripSchedule)

    if service_date is not None:
        query = query.filter(TripSchedule.service_date == service_date)

    schedules = (
        query.order_by(TripSchedule.scheduled_at)
        .limit(100)
        .all()
    )

    return [
        {
            "id": s.id,
            "bus_id": s.bus_id,
            "service_date": s.service_date,
            "scheduled_at": s.scheduled_at,
            "delayed_notified_at": s.delayed_notified_at,
        }
        for s in schedules
    ]
