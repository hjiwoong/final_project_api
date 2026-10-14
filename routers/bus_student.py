from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database.db_connection import get_db
from models.models import Bus, BusStudent, Student

router = APIRouter(prefix="/web/buses", tags=["관리-버스배정"])


class AssignStudent(BaseModel):
    student_id: int


@router.post("/{bus_id}/students", status_code=204)
def assign_student(bus_id: int, body: AssignStudent, db: Session = Depends(get_db)):
    """버스에 탑승 예정 학생 배정"""
    if not db.get(Bus, bus_id):
        raise HTTPException(404, "버스를 찾을 수 없습니다")
    if not db.get(Student, body.student_id):
        raise HTTPException(404, "학생을 찾을 수 없습니다")
    exists = (
        db.query(BusStudent)
        .filter(BusStudent.bus_id == bus_id, BusStudent.student_id == body.student_id)
        .first()
    )
    if not exists:
        db.add(BusStudent(bus_id=bus_id, student_id=body.student_id))
        db.commit()


@router.get("/{bus_id}/students")
def list_bus_students(bus_id: int, db: Session = Depends(get_db)):
    rows = (
        db.query(Student)
        .join(BusStudent, BusStudent.student_id == Student.id)
        .filter(BusStudent.bus_id == bus_id)
        .all()
    )
    return [{"id": s.id, "name": s.name, "class_name": s.class_name} for s in rows]
