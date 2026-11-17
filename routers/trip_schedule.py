"""날짜별 운행 시간표 등록 및 조회 API."""

from datetime import date, datetime, time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database.db_connection import get_db
from models.models import Bus, TripSchedule, User
from core.security import require_admin
from core.time import now_kst_naive

router = APIRouter(prefix="/trip-schedules", tags=["Trip Schedules"])


class TripScheduleCreate(BaseModel):
    bus_id: int
    service_date: date
    start_time: time


class TripScheduleUpdate(BaseModel):
    """앱(기사) 시간표 저장: PUT /trip-schedules/{bus_id}"""
    service_date: date
    start_time: time


def _schedule_dict(s: TripSchedule) -> dict:
    """시간표 응답 공통 형식 (앱이 trip_id 로 '이미 시작한 운행' 여부를 판단)"""
    return {
        "id": s.id,
        "bus_id": s.bus_id,
        "service_date": s.service_date,
        "scheduled_at": s.scheduled_at,
        "trip_id": s.trip_id,
        "delayed_notified_at": s.delayed_notified_at,
    }


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

    if scheduled_at <= now_kst_naive():
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

    return _schedule_dict(schedule)


@router.put("/{bus_id}")
def upsert_trip_schedule(
    bus_id: int,
    body: TripScheduleUpdate,
    me: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """배정 버스의 출발 시각을 등록하거나 아직 시작 전인 시간표를 수정한다.
    이미 그 시간표로 운행을 시작했으면 409."""
    bus = db.get(Bus, bus_id)
    if bus is None:
        raise HTTPException(status_code=404, detail="버스를 찾을 수 없습니다.")

    scheduled_at = datetime.combine(body.service_date, body.start_time)
    if scheduled_at <= now_kst_naive():
        raise HTTPException(
            status_code=400,
            detail="현재 시각 이후의 운행 시간만 등록할 수 있습니다.",
        )

    schedule = (
        db.query(TripSchedule)
        .filter(
            TripSchedule.bus_id == bus_id,
            TripSchedule.service_date == body.service_date,
        )
        .with_for_update()   # 동시에 저장해도 같은 시간표를 두 번 만들지 않게
        .first()
    )
    if schedule is None:
        schedule = TripSchedule(
            bus_id=bus_id,
            service_date=body.service_date,
            scheduled_at=scheduled_at,
        )
        db.add(schedule)
    else:
        if schedule.trip_id is not None:
            raise HTTPException(
                status_code=409,
                detail="이미 운행을 시작한 시간표는 수정할 수 없습니다.",
            )
        schedule.scheduled_at = scheduled_at
        schedule.delayed_notified_at = None   # 시각이 바뀌면 지연 알림을 다시 판단

    db.commit()
    db.refresh(schedule)
    return _schedule_dict(schedule)


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

    return [_schedule_dict(s) for s in schedules]