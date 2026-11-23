from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.security import require_admin, require_parent_terms_agreed
from core.time import KST, now_kst_naive
from database.db_connection import get_db
from routers.location import student_location
from models.models import (
    Attendance, AttendanceReasonType, AttendanceStatus, Bus, BusStudent,
    Guardian, Student, Trip, TripStatus, User, TripStudentAttendance,
)
from services import notifier
from services.attendance_deadline import get_deadline
from services.attendance_file_storage import attendance_file_to_dict

router = APIRouter(tags=["학생"])


class StudentCreate(BaseModel):
    name: str
    class_name: str | None = None
    consent_at: datetime | None = None


class AttendanceSet(BaseModel):
    date: date
    is_absent: bool
    reason_type: AttendanceReasonType | None = None
    reason_memo: str | None = None


class AttendanceRangeSet(BaseModel):
    from_date: date = Field(alias="from")
    to_date: date = Field(alias="to")
    is_absent: bool
    reason_type: AttendanceReasonType | None = None
    reason_memo: str | None = None


def _attendance_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=KST, microsecond=0).isoformat()


def _attendance_status(attendance: Attendance | None) -> str | None:
    if attendance is None or not attendance.is_absent or attendance.status is None:
        return None
    return attendance.status.value


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
    me: User = Depends(require_parent_terms_agreed),
    db: Session = Depends(get_db),
):
    """내 자녀의 날짜별 결석 설정과 배정 버스의 실제 운행 상태"""
    target_date = attendance_date or now_kst_naive().date()
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
    attendance_by_student = {row.student_id: row for row in attendances}
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
        attendance = attendance_by_student.get(student.id)
        deadline_at, editable = get_deadline(db, student.id, target_date)
        children.append({
            **student_to_dict(student),
            "location": student_location(student),
            "is_absent": attendance.is_absent if attendance else False,
            "attendance": {
                "status": _attendance_status(attendance),
                "deadline_at": _attendance_iso(deadline_at),
                "editable": editable,
                "files": (
                    [attendance_file_to_dict(file) for file in attendance.files]
                    if attendance
                    else []
                ),
            },
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
    me: User = Depends(require_parent_terms_agreed),
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
def my_buses(me: User = Depends(require_parent_terms_agreed), db: Session = Depends(get_db)):
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
    me: User = Depends(require_parent_terms_agreed),
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
    me: User = Depends(require_parent_terms_agreed),
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
    background_tasks: BackgroundTasks,
    me: User = Depends(require_parent_terms_agreed),
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

    _, editable = get_deadline(db, student_id, body.date)
    if not editable:
        raise HTTPException(409, "결석 신청 마감 시간이 지났습니다")

    a = (
        db.query(Attendance)
        .filter(Attendance.student_id == student_id, Attendance.date == body.date)
        .first()
    )
    should_notify = bool(
        body.is_absent
        and (
            a is None
            or not a.is_absent
            or a.status == AttendanceStatus.REJECTED
        )
    )
    if a:
        a.is_absent = body.is_absent
        if body.reason_type is not None:
            a.reason_type = body.reason_type
        if body.reason_memo is not None:
            a.reason_memo = body.reason_memo
        if body.is_absent and (not a.status or a.status == AttendanceStatus.REJECTED):
            a.status = AttendanceStatus.REQUESTED
            a.confirmed_by = None
            a.confirmed_at = None
            a.reject_reason = None
        elif not body.is_absent:
            a.status = None
            a.confirmed_by = None
            a.confirmed_at = None
            a.reject_reason = None
    else:
        a = Attendance(
            student_id=student_id,
            date=body.date,
            is_absent=body.is_absent,
            reason_type=body.reason_type,
            reason_memo=body.reason_memo,
            status=AttendanceStatus.REQUESTED if body.is_absent else None,
        )
        db.add(a)
    db.commit()
    if should_notify:
        notification_ids = notifier.create_attendance_notifications(
            db, a.student, body.date, "ABSENCE_REQUESTED"
        )
        db.commit()
        if notification_ids:
            background_tasks.add_task(notifier.send_trip_pushes, notification_ids)
    return {"student_id": student_id, "date": body.date, "is_absent": body.is_absent}


@router.put("/me/children/{student_id}/attendances/range")
def set_attendance_range(
    student_id: int,
    body: AttendanceRangeSet,
    background_tasks: BackgroundTasks,
    me: User = Depends(require_parent_terms_agreed),
    db: Session = Depends(get_db),
):
    is_mine = db.query(Guardian.id).filter(
        Guardian.user_id == me.id,
        Guardian.student_id == student_id,
    ).first()
    if not is_mine:
        raise HTTPException(403, "내 자녀가 아닙니다")
    if body.from_date > body.to_date:
        raise HTTPException(422, "시작 날짜는 종료 날짜보다 늦을 수 없습니다")

    dates = []
    current = body.from_date
    while current <= body.to_date:
        _, editable = get_deadline(db, student_id, current)
        if not editable:
            raise HTTPException(409, "결석 신청 마감 시간이 지났습니다")
        dates.append(current)
        current += timedelta(days=1)

    rows = {
        row.date: row
        for row in db.query(Attendance).filter(
            Attendance.student_id == student_id,
            Attendance.date.in_(dates),
        )
    }
    notification_dates = []
    for attendance_date in dates:
        row = rows.get(attendance_date)
        if body.is_absent:
            if row is None:
                row = Attendance(
                    student_id=student_id,
                    date=attendance_date,
                    is_absent=True,
                    reason_type=body.reason_type,
                    reason_memo=body.reason_memo,
                    status=AttendanceStatus.REQUESTED,
                )
                db.add(row)
                notification_dates.append(attendance_date)
                continue
            if not row.is_absent or row.status == AttendanceStatus.REJECTED:
                notification_dates.append(attendance_date)
                row.status = AttendanceStatus.REQUESTED
                row.confirmed_by = None
                row.confirmed_at = None
                row.reject_reason = None
            row.is_absent = True
            if body.reason_type is not None:
                row.reason_type = body.reason_type
            if body.reason_memo is not None:
                row.reason_memo = body.reason_memo
        elif row is not None:
            row.is_absent = False
            row.status = None
            row.confirmed_by = None
            row.confirmed_at = None
            row.reject_reason = None

    db.commit()
    if notification_dates:
        notification_ids = notifier.create_attendance_notifications(
            db,
            db.get(Student, student_id),
            min(notification_dates),
            "ABSENCE_REQUESTED",
            max(notification_dates),
        )
        db.commit()
        if notification_ids:
            background_tasks.add_task(notifier.send_trip_pushes, notification_ids)
    return {
        "student_id": student_id,
        "from": body.from_date,
        "to": body.to_date,
        "is_absent": body.is_absent,
    }


@router.get("/me/children/{student_id}/attendance")
def get_attendance(
    student_id: int,
    date: date,
    me: User = Depends(require_parent_terms_agreed),
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
    deadline_at, editable = get_deadline(db, student_id, date)

    return {
        "student_id": student_id,
        "date": date,
        "is_absent": attendance.is_absent if attendance else False,
        "status": _attendance_status(attendance),
        "confirmed_at": _attendance_iso(attendance.confirmed_at if attendance else None),
        "reject_reason": attendance.reject_reason if attendance else None,
        "files": (
            [attendance_file_to_dict(file) for file in attendance.files]
            if attendance
            else []
        ),
        "deadline_at": _attendance_iso(deadline_at),
        "editable": editable,
    }


@router.get("/me/children/{student_id}/attendances")
def get_attendances(
    student_id: int,
    from_date: date = Query(alias="from"),
    to_date: date = Query(alias="to"),
    me: User = Depends(require_parent_terms_agreed),
    db: Session = Depends(get_db),
):
    is_mine = db.query(Guardian.id).filter(
        Guardian.user_id == me.id,
        Guardian.student_id == student_id,
    ).first()
    if not is_mine:
        raise HTTPException(403, "내 자녀가 아닙니다")
    if from_date > to_date:
        raise HTTPException(422, "시작 날짜는 종료 날짜보다 늦을 수 없습니다")

    rows = {
        row.date: row
        for row in db.query(Attendance).filter(
            Attendance.student_id == student_id,
            Attendance.date >= from_date,
            Attendance.date <= to_date,
        )
    }
    result = []
    current = from_date
    while current <= to_date:
        attendance = rows.get(current)
        deadline_at, editable = get_deadline(db, student_id, current)
        result.append({
            "student_id": student_id,
            "date": current.isoformat(),
            "is_absent": attendance.is_absent if attendance else False,
            "status": _attendance_status(attendance),
            "confirmed_at": _attendance_iso(attendance.confirmed_at if attendance else None),
            "reject_reason": attendance.reject_reason if attendance else None,
            "files": (
                [attendance_file_to_dict(file) for file in attendance.files]
                if attendance
                else []
            ),
            "deadline_at": _attendance_iso(deadline_at),
            "editable": editable,
        })
        current += timedelta(days=1)
    return result
