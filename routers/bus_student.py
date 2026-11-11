from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.security import require_admin
from database.db_connection import get_db
from models.models import Bus, BusStudent, Guardian, Student, User, UserRole
from services.location_crypto import decrypt_coordinate_value

router = APIRouter(
    prefix="/web/buses",
    tags=["관리-버스배정"],
    dependencies=[Depends(require_admin)],
)


class AssignStudent(BaseModel):
    student_id: int


@router.post("/{bus_id}/students", status_code=204)
def assign_student(bus_id: int, body: AssignStudent, db: Session = Depends(get_db)):
    """버스에 탑승 예정 학생 배정"""
    if not db.get(Bus, bus_id):
        raise HTTPException(404, "버스를 찾을 수 없습니다")
    if not db.get(Student, body.student_id):
        raise HTTPException(404, "학생을 찾을 수 없습니다")
    existing = (
        db.query(BusStudent)
        .filter(
            BusStudent.bus_id == bus_id,
            BusStudent.student_id == body.student_id
        )
        .first()
    )

    if existing:
        return

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
@router.delete("/{bus_id}/students/{student_id}", status_code=204)
def unassign_student(
    bus_id: int,
    student_id: int,
    db: Session = Depends(get_db),
):
    """버스에서 학생 배정 해제"""
    assignment = (
        db.query(BusStudent)
        .filter(
            BusStudent.bus_id == bus_id,
            BusStudent.student_id == student_id,
        )
        .first()
    )

    if not assignment:
        raise HTTPException(404, "학생 배정 정보를 찾을 수 없습니다")

    db.delete(assignment)
    db.commit()


@router.get("/{bus_id}/students")
def list_bus_students_with_guardian_locations(bus_id: int, db: Session = Depends(get_db)):
    rows = (
        db.query(Student)
        .join(BusStudent, BusStudent.student_id == Student.id)
        .filter(BusStudent.bus_id == bus_id)
        .all()
    )
    student_ids = [student.id for student in rows]
    guardian_locations: dict[int, list[dict]] = {}
    if student_ids:
        guardians = (
            db.query(
                Guardian.student_id,
                User.id,
                User.latitude_encrypted,
                User.longitude_encrypted,
            )
            .join(User, User.id == Guardian.user_id)
            .filter(
                Guardian.student_id.in_(student_ids),
                User.role == UserRole.PARENT,
            )
            .order_by(Guardian.id)
            .all()
        )
        for student_id, user_id, latitude, longitude in guardians:
            guardian_locations.setdefault(student_id, []).append(
                {
                    "user_id": user_id,
                    "latitude": (
                        decrypt_coordinate_value(latitude)
                        if latitude is not None
                        else None
                    ),
                    "longitude": (
                        decrypt_coordinate_value(longitude)
                        if longitude is not None
                        else None
                    ),
                }
            )

    return [
        {
            "id": student.id,
            "name": student.name,
            "class_name": student.class_name,
            "guardian_locations": guardian_locations.get(student.id, []),
        }
        for student in rows
    ]
