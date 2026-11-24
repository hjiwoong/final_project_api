from datetime import date

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.security import require_admin
from core.time import KST, now_kst_naive
from database.db_connection import get_db
from models.models import (
    Attendance,
    AttendanceStatus,
    Student,
    User,
)
from services import notifier
from services.attendance_file_storage import attendance_file_to_dict

router = APIRouter(
    tags=["관리-출결"],
    dependencies=[Depends(require_admin)],
)


class AttendanceReject(BaseModel):
    reason: str = Field(min_length=1, max_length=200)


def _iso_kst(value):
    if value is None:
        return None
    return value.replace(tzinfo=KST, microsecond=0).isoformat()


@router.get("/web/attendances")
def list_attendances(
    attendance_date: date = Query(alias="date"),
    status: AttendanceStatus | None = None,
    db: Session = Depends(get_db),
):
    pending_count = db.query(Attendance.id).filter(
        Attendance.date == attendance_date,
        Attendance.is_absent.is_(True),
        Attendance.status == AttendanceStatus.REQUESTED,
    ).count()
    query = db.query(Attendance).filter(
        Attendance.date == attendance_date,
        Attendance.is_absent.is_(True),
    )
    if status is not None:
        query = query.filter(Attendance.status == status)
    rows = query.order_by(Attendance.id).all()
    return {
        "date": attendance_date.isoformat(),
        "pending_count": pending_count,
        "items": [
            {
                "id": attendance.id,
                "student_id": attendance.student_id,
                "student_name": attendance.student.name,
                "date": attendance.date.isoformat(),
                "is_absent": attendance.is_absent,
                "reason_type": (
                    attendance.reason_type.value if attendance.reason_type else None
                ),
                "reason_memo": attendance.reason_memo,
                "status": attendance.status.value if attendance.status else None,
                "confirmed_by": attendance.confirmed_by,
                "confirmed_at": _iso_kst(attendance.confirmed_at),
                "reject_reason": attendance.reject_reason,
                "files": [
                    attendance_file_to_dict(file) for file in attendance.files
                ],
            }
            for attendance in rows
        ],
    }


def _process_attendance(
    attendance_id: int,
    status: AttendanceStatus,
    reason: str | None,
    me: User,
    db: Session,
    background_tasks: BackgroundTasks,
):
    updated = (
        db.query(Attendance)
        .filter(
            Attendance.id == attendance_id,
            Attendance.status == AttendanceStatus.REQUESTED,
        )
        .update(
            {
                Attendance.status: status,
                Attendance.reject_reason: reason,
                Attendance.confirmed_by: me.id,
                Attendance.confirmed_at: now_kst_naive(),
            },
            synchronize_session=False,
        )
    )
    if not updated:
        attendance = db.get(Attendance, attendance_id)
        if attendance is None:
            raise HTTPException(404, "결석 신청을 찾을 수 없습니다")
        raise HTTPException(409, "이미 처리된 신청입니다")

    attendance = db.get(Attendance, attendance_id)
    student = db.get(Student, attendance.student_id)
    db.commit()
    event_type = (
        "ABSENCE_CONFIRMED"
        if status == AttendanceStatus.CONFIRMED
        else "ABSENCE_REJECTED"
    )
    notification_ids = notifier.create_attendance_notifications(
        db, student, attendance.date, event_type
    )
    db.commit()
    if notification_ids:
        background_tasks.add_task(notifier.send_trip_pushes, notification_ids)
    return {
        "id": attendance_id,
        "status": status.value,
        "confirmed_by": me.id,
        "confirmed_at": _iso_kst(attendance.confirmed_at),
        "reject_reason": reason,
    }


@router.post("/web/attendances/{attendance_id}/confirm")
def confirm_attendance(
    attendance_id: int,
    background_tasks: BackgroundTasks,
    me: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return _process_attendance(
        attendance_id,
        AttendanceStatus.CONFIRMED,
        None,
        me,
        db,
        background_tasks,
    )


@router.post("/web/attendances/{attendance_id}/reject")
def reject_attendance(
    attendance_id: int,
    body: AttendanceReject,
    background_tasks: BackgroundTasks,
    me: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return _process_attendance(
        attendance_id,
        AttendanceStatus.REJECTED,
        body.reason,
        me,
        db,
        background_tasks,
    )
