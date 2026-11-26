from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.security import require_admin
from core.time import KST
from database.db_connection import get_db
from models.models import (
    Attendance,
    AttendanceStatus,
)
from services.attendance_file_storage import attendance_file_to_dict

router = APIRouter(
    tags=["관리-출결"],
    dependencies=[Depends(require_admin)],
)


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
