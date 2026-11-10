
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.security import require_parent
from database.db_connection import get_db
from models.models import Bus, BusStudent, Guardian, Trip, TripStatus, User

router = APIRouter(prefix="/me", tags=["학부모-버스운행"])


@router.get("/bus-status")
def get_parent_bus_status(
    me: User = Depends(require_parent),
    db: Session = Depends(get_db),
):
    # 로그인한 학부모의 자녀가 배정된 버스 조회
    buses = (
        db.query(Bus)
        .join(BusStudent, BusStudent.bus_id == Bus.id)
        .join(Guardian, Guardian.student_id == BusStudent.student_id)
        .filter(Guardian.user_id == me.id)
        .distinct()
        .order_by(Bus.id)
        .all()
    )

    result = []

    for bus in buses:
        # 해당 버스의 현재 진행 중인 운행 조회
        trip = (
            db.query(Trip)
            .filter(
                Trip.bus_id == bus.id,
                Trip.status == TripStatus.IN_PROGRESS,
            )
            .order_by(Trip.started_at.desc(), Trip.id.desc())
            .first()
        )

        result.append({
            "bus_id": bus.id,
            "bus_name": bus.name,
            "plate_no": bus.plate_no,
            "trip_id": trip.id if trip else None,
            "status": trip.status.value if trip else "NOT_RUNNING",
            "started_at": (
                trip.started_at.isoformat()
                if trip and trip.started_at
                else None
            ),
        })

    return result
