from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database.db_connection import get_db
from models.models import Bus, BusStudent
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