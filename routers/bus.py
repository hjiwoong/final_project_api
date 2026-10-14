from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database.db_connection import get_db
from models.models import Bus, BusStudent

router = APIRouter(prefix="/web/buses", tags=["관리-버스"])


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
