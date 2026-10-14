from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.security import require_admin, require_parent
from database.db_connection import get_db
from models.models import Attendance, Guardian, Student, User

router = APIRouter(tags=["학생"])


class StudentCreate(BaseModel):
    name: str
    class_name: str | None = None
    consent_at: datetime | None = None


class AttendanceSet(BaseModel):
    date: date
    is_absent: bool


def student_to_dict(s: Student) -> dict:
    return {
        "id": s.id, "name": s.name, "class_name": s.class_name,
        "consent_at": s.consent_at,
    }


# ---------- 관리자 전용 ----------
@router.post("/web/students", status_code=201, dependencies=[Depends(require_admin)])
def create_student(body: StudentCreate, db: Session = Depends(get_db)):
    s = Student(**body.model_dump())
    db.add(s)
    db.commit()
    db.refresh(s)
    return student_to_dict(s)


@router.get("/web/students", dependencies=[Depends(require_admin)])
def list_students(db: Session = Depends(get_db)):
    return [student_to_dict(s) for s in db.query(Student).all()]


# ---------- 학부모 전용 (내 자녀만) ----------
@router.get("/me/children")
def my_children(me: User = Depends(require_parent), db: Session = Depends(get_db)):
    rows = (
        db.query(Student)
        .join(Guardian, Guardian.student_id == Student.id)
        .filter(Guardian.user_id == me.id)
        .all()
    )
    return [student_to_dict(s) for s in rows]


@router.put("/me/children/{student_id}/attendance")
def set_attendance(
    student_id: int,
    body: AttendanceSet,
    me: User = Depends(require_parent),
    db: Session = Depends(get_db),
):
    """자녀 출석/결석 설정 - 내 자녀가 아니면 403"""
    is_mine = (
        db.query(Guardian)
        .filter(Guardian.user_id == me.id, Guardian.student_id == student_id)
        .first()
    )
    if not is_mine:
        raise HTTPException(403, "내 자녀가 아닙니다")

    a = (
        db.query(Attendance)
        .filter(Attendance.student_id == student_id, Attendance.date == body.date)
        .first()
    )
    if a:
        a.is_absent = body.is_absent
    else:
        db.add(Attendance(student_id=student_id, date=body.date, is_absent=body.is_absent))
    db.commit()
    return {"student_id": student_id, "date": body.date, "is_absent": body.is_absent}
