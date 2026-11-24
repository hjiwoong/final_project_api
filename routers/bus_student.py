from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.security import require_admin
from database.db_connection import get_db
from models.models import Attendance, AttendanceFile, Bus, BusStudent, Student
from routers.location import student_location

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


@router.get("/attendance/calendar")
def attendance_calendar(
    year: int = Query(ge=2000, le=2100),
    month: int = Query(ge=1, le=12),
    db: Session = Depends(get_db),
):
    """월별 결석 신청을 날짜·버스별로 조회한다."""
    start = date(year, month, 1)
    end = date(year + month // 12, month % 12 + 1, 1)
    rows = (
        db.query(
            Attendance,
            Bus.id,
            Bus.name,
            Bus.plate_no,
            Student.id,
            Student.name,
            Student.class_name,
        )
        .join(Student, Student.id == Attendance.student_id)
        .join(BusStudent, BusStudent.student_id == Student.id)
        .join(Bus, Bus.id == BusStudent.bus_id)
        .filter(
            Attendance.date >= start,
            Attendance.date < end,
            Attendance.is_absent.is_(True),
        )
        .order_by(Attendance.date, Bus.name, Student.name, Student.id)
        .all()
    )

    days: dict[date, dict[int, dict]] = {}
    from routers.student import file_to_dict, reason_text   # 순환 import 방지

    for attendance, bus_id, bus_name, plate_no, student_id, name, class_name in rows:
        attendance_date = attendance.date
        buses = days.setdefault(attendance_date, {})
        bus = buses.setdefault(
            bus_id,
            {
                "bus_id": bus_id,
                "bus_name": bus_name,
                "plate_no": plate_no,
                "students": [],
            },
        )
        bus["students"].append({
            "id": student_id,
            "name": name,
            "class_name": class_name,
            "is_absent": True,
            "reason_type": attendance.reason_type,
            "reason_memo": attendance.reason_memo,
            "reason_text": reason_text(attendance),
            "files": [file_to_dict(f) for f in attendance.files],
        })

    return {
        "year": year,
        "month": month,
        "days": [
            {
                "date": attendance_date.isoformat(),
                "absence_count": sum(
                    len(bus["students"]) for bus in buses.values()
                ),
                "buses": list(buses.values()),
            }
            for attendance_date, buses in sorted(days.items())
        ],
    }


@router.get("/attendance/files/{file_id}")
def admin_attendance_file(file_id: int, db: Session = Depends(get_db)):
    """관리자: 결석 사유 첨부 파일 보기"""
    f = db.get(AttendanceFile, file_id)
    if not f:
        raise HTTPException(404, "첨부 파일을 찾을 수 없습니다")
    return Response(content=f.data, media_type=f.content_type,
                    headers={"Cache-Control": "private, no-store"})


@router.get("/attendance/{attendance_id}")
def admin_attendance_detail(attendance_id: int, db: Session = Depends(get_db)):
    """관리자: 결석 알림을 눌렀을 때 — 학생·날짜·사유·첨부 (학부모가 취소했으면 is_absent=false)"""
    from routers.student import REASON_LABELS, file_to_dict, reason_text   # 순환 import 방지

    a = db.get(Attendance, attendance_id)
    if not a:
        raise HTTPException(404, "결석 기록을 찾을 수 없습니다")
    s = a.student
    bus = (
        db.query(Bus)
        .join(BusStudent, BusStudent.bus_id == Bus.id)
        .filter(BusStudent.student_id == a.student_id)
        .first()
    )
    return {
        "attendance_id": a.id,
        "date": a.date,
        "is_absent": a.is_absent,
        "student": {"id": s.id, "name": s.name, "class_name": s.class_name} if s else None,
        "bus": {"id": bus.id, "name": bus.name, "plate_no": bus.plate_no} if bus else None,
        "reason_type": a.reason_type,
        "reason_label": REASON_LABELS.get(a.reason_type or "", a.reason_type),
        "reason_memo": a.reason_memo,
        "reason_text": reason_text(a),
        "files": [file_to_dict(f) for f in a.files],
    }


@router.get("/{bus_id}/students")
def list_bus_students(
    bus_id: int,
    attendance_date: date = Query(default_factory=date.today, alias="date"),
    db: Session = Depends(get_db),
):
    if not db.get(Bus, bus_id):
        raise HTTPException(404, "버스를 찾을 수 없습니다")

    rows = (
        db.query(Student)
        .join(BusStudent, BusStudent.student_id == Student.id)
        .filter(BusStudent.bus_id == bus_id)
        .order_by(Student.id)
        .all()
    )
    student_ids = [student.id for student in rows]
    absent_ids = set()
    if student_ids:
        absent_ids = {
            student_id
            for (student_id,) in db.query(Attendance.student_id).filter(
                Attendance.student_id.in_(student_ids),
                Attendance.date == attendance_date,
                Attendance.is_absent.is_(True),
            )
        }

    return [
        {
            "id": student.id,
            "name": student.name,
            "class_name": student.class_name,
            "is_absent": student.id in absent_ids,
            "location": student_location(student),
        }
        for student in rows
    ]