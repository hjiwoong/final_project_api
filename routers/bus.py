import os
import shutil

from fastapi import APIRouter, Depends, HTTPException
from services.storage import UPLOAD_DIR
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database.db_connection import get_db
from models.models import (
    Bus,
    BusStudent,
    Trip,
    BoardingEvent,
    Alert,
    Notification,
)
from core.security import require_admin

router = APIRouter(
    prefix="/web/buses",
    tags=["관리-버스"],
    dependencies=[Depends(require_admin)],
)


class BusCreate(BaseModel):
    plate_no: str
    name: str
    driver_id: int | None = None


def bus_to_dict(b: Bus, student_count: int = 0) -> dict:
    return {
        "id": b.id, "plate_no": b.plate_no, "name": b.name,
        "driver_id": b.driver_id, "student_count": student_count,
    }


@router.post("", status_code=201)
def create_bus(body: BusCreate, db: Session = Depends(get_db)):
    if db.query(Bus).filter(Bus.plate_no == body.plate_no).first():
        raise HTTPException(409, "이미 등록된 차량번호입니다")
    b = Bus(**body.model_dump())
    db.add(b)
    db.commit()
    db.refresh(b)
    return bus_to_dict(b)


@router.get("")
def list_buses(db: Session = Depends(get_db)):
    buses = db.query(Bus).all()
    result = []
    for b in buses:
        cnt = db.query(BusStudent).filter(BusStudent.bus_id == b.id).count()
        result.append(bus_to_dict(b, cnt))
    return result

@router.put("/{bus_id}")
def update_bus(
    bus_id: int,
    body: BusCreate,
    db: Session = Depends(get_db),
):
    bus = db.get(Bus, bus_id)

    if not bus:
        raise HTTPException(
            status_code=404,
            detail="버스를 찾을 수 없습니다"
        )

    duplicate = (
        db.query(Bus)
        .filter(
            Bus.plate_no == body.plate_no,
            Bus.id != bus_id
        )
        .first()
    )

    if duplicate:
        raise HTTPException(
            status_code=409,
            detail="이미 등록된 차량번호입니다"
        )

    bus.name = body.name
    bus.plate_no = body.plate_no
    bus.driver_id = body.driver_id

    db.commit()
    db.refresh(bus)

    student_count = (
        db.query(BusStudent)
        .filter(BusStudent.bus_id == bus.id)
        .count()
    )

    return bus_to_dict(bus, student_count)

@router.get("/{bus_id}/delete-preview")
def preview_bus_delete(
    bus_id: int,
    db: Session = Depends(get_db),
):
    bus = db.get(Bus, bus_id)

    if not bus:
        raise HTTPException(404, "버스를 찾을 수 없습니다")

    trip_ids = db.query(Trip.id).filter(
        Trip.bus_id == bus_id
    )

    alert_ids = db.query(Alert.id).filter(
        Alert.trip_id.in_(trip_ids)
    )

    return {
        "bus_id": bus.id,
        "bus_name": bus.name,
        "plate_no": bus.plate_no,
        "assignments": db.query(BusStudent).filter(
            BusStudent.bus_id == bus_id
        ).count(),
        "trips": db.query(Trip).filter(
            Trip.bus_id == bus_id
        ).count(),
        "real_trips": db.query(Trip).filter(
            Trip.bus_id == bus_id,
            Trip.simulation_mode.is_(False)
        ).count(),
        "events": db.query(BoardingEvent).filter(
            BoardingEvent.trip_id.in_(trip_ids)
        ).count(),
        "alerts": db.query(Alert).filter(
            Alert.trip_id.in_(trip_ids)
        ).count(),
        "notifications": db.query(Notification).filter(
            Notification.alert_id.in_(alert_ids)
        ).count(),
    }

@router.delete("/{bus_id}")
def delete_test_bus(
    bus_id: int,
    plate_no: str,
    db: Session = Depends(get_db),
):
    bus = db.get(Bus, bus_id)

    if not bus:
        raise HTTPException(404, "버스를 찾을 수 없습니다")

    if bus.plate_no != plate_no:
        raise HTTPException(
            400, "차량번호가 일치하지 않습니다"
        )

    trips = db.query(Trip).filter(Trip.bus_id == bus_id)

    if trips.filter(
        Trip.simulation_mode.is_not(True)
    ).count() > 0:
        raise HTTPException(
            409,
            "실제 운행 기록이 포함된 버스는 삭제할 수 없습니다"
        )

    saved_trip_ids = [
        trip_id
        for (trip_id,) in db.query(Trip.id).filter(
            Trip.bus_id == bus_id
        ).all()
    ]

    trip_ids = db.query(Trip.id).filter(
        Trip.bus_id == bus_id
    )

    alert_ids = db.query(Alert.id).filter(
        Alert.trip_id.in_(trip_ids)
    )

    try:
        deleted = {
            "notifications": db.query(Notification).filter(
                Notification.alert_id.in_(alert_ids)
            ).delete(synchronize_session=False),

            "alerts": db.query(Alert).filter(
                Alert.trip_id.in_(trip_ids)
            ).delete(synchronize_session=False),

            "events": db.query(BoardingEvent).filter(
                BoardingEvent.trip_id.in_(trip_ids)
            ).delete(synchronize_session=False),

            "trips": trips.delete(
                synchronize_session=False
            ),

            "assignments": db.query(BusStudent).filter(
                BusStudent.bus_id == bus_id
            ).delete(synchronize_session=False),
        }

        db.delete(bus)
        db.commit()

        failed_folders = []

        for trip_id in saved_trip_ids:
            folder = os.path.join(
                UPLOAD_DIR, f"trip_{trip_id}"
            )

            try:
                if os.path.isdir(folder):
                    shutil.rmtree(folder)
            except OSError:
                failed_folders.append(folder)

    except Exception:
        db.rollback()
        raise

    return {
        "message": "버스 및 관련 기록 삭제 처리 완료",
        "bus_id": bus_id,
        "deleted": deleted,
        "failed_folders": failed_folders,
    }