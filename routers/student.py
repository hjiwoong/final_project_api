from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.security import require_admin, require_parent
from database.db_connection import get_db
from models.models import (
    Attendance, Bus, BusStudent, Guardian, Student, Trip, TripStatus, User,
)

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
def my_children(
    attendance_date: date | None = Query(default=None, alias="date"),
    me: User = Depends(require_parent),
    db: Session = Depends(get_db),
):
    """내 자녀의 날짜별 결석 설정과 배정 버스의 실제 운행 상태"""
    target_date = attendance_date or date.today()
    rows = (
        db.query(Student)
        .join(Guardian, Guardian.student_id == Student.id)
        .filter(Guardian.user_id == me.id)
        .all()
    )
<<<<<<< HEAD
    return [student_to_dict(s) for s in rows]
=======
    if not rows:
        return []

    student_ids = [student.id for student in rows]
    assignments = (
        db.query(BusStudent.student_id, Bus.id, Bus.name)
        .join(Bus, Bus.id == BusStudent.bus_id)
        .filter(BusStudent.student_id.in_(student_ids))
        .order_by(BusStudent.student_id, Bus.id)
        .all()
    )
    buses_by_student: dict[int, list[dict]] = {}
    bus_ids = set()
    for student_id, bus_id, bus_name in assignments:
        buses_by_student.setdefault(student_id, []).append(
            {"id": bus_id, "name": bus_name}
        )
        bus_ids.add(bus_id)

    start = datetime.combine(target_date, datetime.min.time())
    end = start + timedelta(days=1)
    latest_trip_by_bus = {}
    if bus_ids:
        trips = (
            db.query(Trip)
            .filter(
                Trip.bus_id.in_(bus_ids),
                Trip.started_at >= start,
                Trip.started_at < end,
                Trip.simulation_mode.is_(False),
            )
            .order_by(Trip.started_at.desc(), Trip.id.desc())
            .all()
        )
        for trip in trips:
            latest_trip_by_bus.setdefault(trip.bus_id, trip)

    attendances = (
        db.query(Attendance)
        .filter(
            Attendance.student_id.in_(student_ids),
            Attendance.date == target_date,
        )
        .all()
    )
    absent_by_student = {row.student_id: row.is_absent for row in attendances}

    children = []
    for student in rows:
        buses = buses_by_student.get(student.id, [])
        buses_with_trips = [
            bus for bus in buses if bus["id"] in latest_trip_by_bus
        ]
        if buses_with_trips:
            bus = max(
                buses_with_trips,
                key=lambda item: latest_trip_by_bus[item["id"]].started_at,
            )
            trip_status = latest_trip_by_bus[bus["id"]].status.value
        elif buses:
            bus = buses[0]
            trip_status = "NOT_STARTED"
        else:
            bus = None
            trip_status = "NOT_ASSIGNED"

        children.append({
            **student_to_dict(student),
            "is_absent": absent_by_student.get(student.id, False),
            "bus_id": bus["id"] if bus else None,
            "bus_name": bus["name"] if bus else None,
            "bus_status": trip_status,
        })
    return children
>>>>>>> 7c72627 (학부모 및 관리자 좌표 및 타 기기 지원에 대한 API 일부 수정)


@router.get("/me/buses")
def my_buses(me: User = Depends(require_parent), db: Session = Depends(get_db)):
    """현재 학부모의 자녀에게 배정된 버스만 조회한다."""
    rows = (
        db.query(Bus.id, Bus.name, Bus.plate_no, BusStudent.student_id)
        .join(BusStudent, BusStudent.bus_id == Bus.id)
        .join(Guardian, Guardian.student_id == BusStudent.student_id)
        .filter(Guardian.user_id == me.id)
        .order_by(Bus.name, Bus.id)
        .all()
    )

    buses: dict[int, dict] = {}
    for bus_id, name, plate_no, student_id in rows:
        bus = buses.setdefault(
            bus_id,
            {
                "id": bus_id,
                "name": name,
                "plate_no": plate_no,
                "student_count": 0,
            },
        )
        bus["student_count"] += 1
    return list(buses.values())


@router.delete("/me/children/{student_id}", status_code=204)
def unlink_child(
    student_id: int,
    me: User = Depends(require_parent),
    db: Session = Depends(get_db),
):
    """내 계정과 자녀의 연결만 해제한다"""
    guardian = (
        db.query(Guardian)
        .filter(Guardian.user_id == me.id, Guardian.student_id == student_id)
        .first()
    )
    if not guardian:
        raise HTTPException(404, "연결된 자녀를 찾을 수 없습니다")

    db.delete(guardian)
    db.commit()


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

@router.get("/me/children/{student_id}/attendance")
def get_attendance(
    student_id: int,
    date: date,
    me: User = Depends(require_parent),
    db: Session = Depends(get_db),
):
    """내 자녀의 특정 날짜 출석/결석 상태 조회"""

    is_mine = (
        db.query(Guardian)
        .filter(
            Guardian.user_id == me.id,
            Guardian.student_id == student_id,
        )
        .first()
    )

    if not is_mine:
        raise HTTPException(403, "내 자녀가 아닙니다")

    attendance = (
        db.query(Attendance)
        .filter(
            Attendance.student_id == student_id,
            Attendance.date == date,
        )
        .first()
    )

    return {
        "student_id": student_id,
        "date": date,
        "is_absent": attendance.is_absent if attendance else False,
    }
