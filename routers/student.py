from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.security import require_admin, require_parent
from database.db_connection import get_db
from routers.location import student_location
from models.models import (
    Attendance, Bus, BusStudent, Guardian, Student, Trip, TripStatus, User,
    TripStudentAttendance,
)

router = APIRouter(tags=["학생"])


class StudentCreate(BaseModel):
    name: str
    class_name: str | None = None
    consent_at: datetime | None = None


class AttendanceSet(BaseModel):
    date: date
    is_absent: bool


class TripAttendanceConfirmation(BaseModel):
    event_type: Literal["BOARDING", "ALIGHTING"]


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
    trip_student_attendances = {}
    trip_ids = {trip.id for trip in latest_trip_by_bus.values()}
    if trip_ids:
        trip_student_attendances = {
            (row.trip_id, row.student_id): row
            for row in db.query(TripStudentAttendance)
            .filter(
                TripStudentAttendance.trip_id.in_(trip_ids),
                TripStudentAttendance.student_id.in_(student_ids),
            )
            .all()
        }

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

        selected_trip = latest_trip_by_bus.get(bus["id"]) if bus else None
        trip_attendance = (
            trip_student_attendances.get((selected_trip.id, student.id))
            if selected_trip
            else None
        )
        children.append({
            **student_to_dict(student),
            "location": student_location(student),
            "is_absent": absent_by_student.get(student.id, False),
            "bus_id": bus["id"] if bus else None,
            "bus_name": bus["name"] if bus else None,
            "bus_status": trip_status,
            "trip_id": selected_trip.id if selected_trip else None,
            "boarding_confirmed_at": (
                trip_attendance.boarding_confirmed_at if trip_attendance else None
            ),
            "alighting_confirmed_at": (
                trip_attendance.alighting_confirmed_at if trip_attendance else None
            ),
        })
    return children


@router.post("/me/trips/{trip_id}/students/{student_id}/confirmations")
def confirm_trip_attendance(
    trip_id: int,
    student_id: int,
    body: TripAttendanceConfirmation,
    me: User = Depends(require_parent),
    db: Session = Depends(get_db),
):
    """보호자가 자신의 자녀가 해당 운행에서 승차 또는 하차했음을 확인한다."""
    is_guardian = (
        db.query(Guardian.id)
        .filter(Guardian.user_id == me.id, Guardian.student_id == student_id)
        .first()
    )
    if not is_guardian:
        raise HTTPException(404, "확인할 자녀를 찾을 수 없습니다")

    trip = db.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(404, "운행을 찾을 수 없습니다")
    if trip.status != TripStatus.IN_PROGRESS:
        raise HTTPException(409, "진행 중인 운행만 승·하차 확인을 할 수 있습니다")
    if trip.simulation_mode:
        raise HTTPException(409, "시뮬레이션 운행은 승·하차 확인 대상이 아닙니다")

    assigned = (
        db.query(BusStudent.id)
        .filter(
            BusStudent.bus_id == trip.bus_id,
            BusStudent.student_id == student_id,
        )
        .first()
    )
    if not assigned:
        raise HTTPException(404, "해당 운행에 배정된 자녀가 아닙니다")

    trip_date = trip.started_at.date() if trip.started_at else date.today()
    is_absent = (
        db.query(Attendance.id)
        .filter(
            Attendance.student_id == student_id,
            Attendance.date == trip_date,
            Attendance.is_absent.is_(True),
        )
        .first()
    )
    if is_absent:
        raise HTTPException(409, "결석으로 등록된 자녀는 승·하차 확인을 할 수 없습니다")

    attendance = (
        db.query(TripStudentAttendance)
        .filter(
            TripStudentAttendance.trip_id == trip_id,
            TripStudentAttendance.student_id == student_id,
        )
        .first()
    )
    if attendance is None:
        attendance = TripStudentAttendance(trip_id=trip_id, student_id=student_id)
        db.add(attendance)
        db.flush()

    now = datetime.now()
    if body.event_type == "BOARDING":
        if attendance.boarding_confirmed_at is None:
            attendance.boarding_confirmed_at = now
            attendance.boarding_confirmed_by = me.id
    else:
        if attendance.boarding_confirmed_at is None:
            raise HTTPException(409, "승차 확인 후 하차 확인을 할 수 있습니다")
        if attendance.alighting_confirmed_at is None:
            attendance.alighting_confirmed_at = now
            attendance.alighting_confirmed_by = me.id

    db.commit()
    db.refresh(attendance)
    return {
        "trip_id": trip_id,
        "student_id": student_id,
        "boarding_confirmed_at": attendance.boarding_confirmed_at,
        "alighting_confirmed_at": attendance.alighting_confirmed_at,
    }


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


@router.delete("/me/children/{student_id}", status_code=204)
def unlink_my_child(
    student_id: int,
    me: User = Depends(require_parent),
    db: Session = Depends(get_db),
):
    guardian = (
        db.query(Guardian)
        .filter(Guardian.user_id == me.id, Guardian.student_id == student_id)
        .first()
    )
    if guardian is None:
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
