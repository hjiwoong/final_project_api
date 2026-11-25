import os
from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
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


# 한 달에 결석할 수 있는 날 수 (학부모 앱에서 결석 등록은 즉시 반영되므로 남용 방지)

MONTHLY_ABSENCE_LIMIT = int(os.getenv("MONTHLY_ABSENCE_LIMIT", "4"))


def _month_bounds(d: date) -> tuple[date, date]:
    start = d.replace(day=1)
    end = date(start.year + start.month // 12, start.month % 12 + 1, 1)
    return start, end


def monthly_absence_used(db: Session, student_id: int, d: date, exclude: set[date] | None = None) -> int:
    """그 달에 이미 결석으로 등록된 날 수 (exclude 날짜는 빼고 센다)"""
    start, end = _month_bounds(d)
    q = db.query(Attendance.date).filter(
        Attendance.student_id == student_id,
        Attendance.is_absent.is_(True),
        Attendance.date >= start,
        Attendance.date < end,
    )
    return sum(1 for (day,) in q if not exclude or day not in exclude)


def _limit_message(d: date) -> str:
    return (
        f"{d.month}월에는 이미 결석을 {MONTHLY_ABSENCE_LIMIT}번 신청해서 더 이상 결석할 수 없어요. "
        "꼭 결석해야 하면 기관에 연락해 주세요."
    )


def check_monthly_absence_limit(db: Session, student_id: int, new_dates: list[date]) -> None:
    """새로 결석으로 바꿀 날짜들을 더하면 달마다 한도를 넘는지 확인 → 넘으면 409"""
    by_month: dict[tuple[int, int], list[date]] = {}
    for d in new_dates:
        by_month.setdefault((d.year, d.month), []).append(d)
    for days in by_month.values():
        used = monthly_absence_used(db, student_id, days[0], exclude=set(days))
        if used + len(days) > MONTHLY_ABSENCE_LIMIT:
            raise HTTPException(409, _limit_message(days[0]))


REASON_LABELS = {"SICK": "질병", "FIELD_TRIP": "현장체험", "FAMILY": "가족 사유", "OTHER": "기타"}


def _reason_code(attendance: Attendance | None) -> str | None:
    value = attendance.reason_type if attendance else None
    if value is None:
        return None
    return value.value if hasattr(value, "value") else str(value)


def reason_text(attendance: Attendance | None) -> str | None:
    """'질병 · 감기 기운' 처럼 사람이 읽는 사유 (관리자 화면용)"""
    if not attendance or not attendance.is_absent:
        return None
    code = _reason_code(attendance)
    kind = REASON_LABELS.get(code or "", code)
    text = " · ".join(x.strip() for x in (kind, attendance.reason_memo) if x and x.strip())
    return text or None


def _attendance_extra(attendance: Attendance | None) -> dict:
    """앱이 쓰는 추가 정보: 결석 기록 ID(첨부 업로드용), 사유"""
    absent = bool(attendance and attendance.is_absent)
    return {
        "attendance_id": attendance.id if attendance else None,
        "reason_type": _reason_code(attendance) if absent else None,
        "reason_memo": attendance.reason_memo if absent else None,
    }


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

    def current() -> Attendance | None:
        return (
            db.query(Attendance)
            .filter(Attendance.student_id == student_id, Attendance.date == body.date)
            .first()
        )

    a = current()
    if body.is_absent and not (a and a.is_absent):
        check_monthly_absence_limit(db, student_id, [body.date])
    if a is None and body.is_absent:
        # 같은 날짜 요청이 거의 동시에 오면 UNIQUE(student_id, date) 충돌로 500 이 났다
        # → 빈 행을 먼저 만들어 두고(이미 생겼으면 그 행을) 아래에서 '기존 행 수정'으로 처리
        try:
            with db.begin_nested():
                db.add(Attendance(student_id=student_id, date=body.date, is_absent=False))
        except IntegrityError:
            pass
        a = current()
    was_absent = bool(a and a.is_absent)
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
    event = (
        "ABSENCE_REQUESTED" if should_notify
        else "ABSENCE_CANCELED" if was_absent and not body.is_absent
        else None
    )
    if event:
        notification_ids = notifier.create_attendance_notifications(
            db, a.student, body.date, event, attendance_id=a.id
        )
        db.commit()
        if notification_ids:
            background_tasks.add_task(notifier.send_trip_pushes, notification_ids)
    deadline_at, editable = get_deadline(db, student_id, body.date)
    return {
        "student_id": student_id,
        "date": body.date,
        "is_absent": body.is_absent,
        **_attendance_extra(a),
        "status": _attendance_status(a),
        "files": [attendance_file_to_dict(file) for file in a.files] if a else [],
        "deadline_at": _attendance_iso(deadline_at),
        "editable": editable,
    }


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
    if body.is_absent:
        check_monthly_absence_limit(
            db, student_id,
            [d for d in dates if not (rows.get(d) and rows[d].is_absent)],
        )
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


@router.get("/me/children/{student_id}/absence-limit")
def get_absence_limit(
    student_id: int,
    date: date,
    me: User = Depends(require_parent_terms_agreed),
    db: Session = Depends(get_db),
):
    """그 날짜가 속한 달의 결석 한도·사용 횟수 (앱이 결석 신청 전에 확인)"""
    is_mine = db.query(Guardian.id).filter(
        Guardian.user_id == me.id, Guardian.student_id == student_id
    ).first()
    if not is_mine:
        raise HTTPException(403, "내 자녀가 아닙니다")
    used = monthly_absence_used(db, student_id, date)
    already = db.query(Attendance.id).filter(
        Attendance.student_id == student_id,
        Attendance.date == date,
        Attendance.is_absent.is_(True),
    ).first() is not None
    can_request = already or used < MONTHLY_ABSENCE_LIMIT
    return {
        "student_id": student_id,
        "year": date.year,
        "month": date.month,
        "limit": MONTHLY_ABSENCE_LIMIT,
        "used": used,
        "remaining": max(MONTHLY_ABSENCE_LIMIT - used, 0),
        "can_request": can_request,
        "message": None if can_request else _limit_message(date),
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
        **_attendance_extra(attendance),
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
            **_attendance_extra(attendance),
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


@router.get("/me/children/{student_id}/attendance/files/{file_id}")
def download_child_attendance_file(
    student_id: int,
    file_id: int,
    me: User = Depends(require_parent_terms_agreed),
    db: Session = Depends(get_db),
):
    """학부모 앱 '출석·결석 관리'의 첨부 보기 (앱이 쓰는 주소). 실제 파일은 attendance_file 과 같은 곳"""
    from models.models import AttendanceFile
    from routers.attendance_file import _download_response   # 순환 import 방지

    is_mine = db.query(Guardian.id).filter(
        Guardian.user_id == me.id, Guardian.student_id == student_id,
    ).first()
    if not is_mine:
        raise HTTPException(403, "내 자녀가 아닙니다")
    file = db.get(AttendanceFile, file_id)
    if file is None or file.attendance.student_id != student_id:
        raise HTTPException(404, "첨부 파일을 찾을 수 없습니다")
    return _download_response(file)
