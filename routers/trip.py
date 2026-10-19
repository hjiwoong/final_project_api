from datetime import date, datetime

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Query, UploadFile,
)
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.security import require_admin
from database.db_connection import get_db
from models.models import (
    Alert, AlertStatus,
    Attendance, Bus, BusStudent, BoardingEvent, ConfirmMethod,
    EventType, Trip, TripResult, TripStatus, User,
)
from services import event_bus, notifier, storage
from services.detector import detect_people, low_conf_count
from services.judge import judge
from services.serializers import alert_to_dict, event_to_dict, trip_to_dict

# 운행 API 는 관리자(기사) 앱 전용
router = APIRouter(prefix="/trips", tags=["운행"], dependencies=[Depends(require_admin)])


class TripStart(BaseModel):
    bus_id: int
    simulation_mode: bool = False


class ConfirmBody(BaseModel):
    method: ConfirmMethod = ConfirmMethod.BUTTON
    safety_checked: bool = False
    simulation_mode: bool = False


# ---------- helpers ----------
def get_trip_or_404(db: Session, trip_id: int) -> Trip:
    t = db.get(Trip, trip_id)
    if not t:
        raise HTTPException(404, "운행을 찾을 수 없습니다")
    return t


def expected_students(db: Session, bus_id: int) -> int:
    """탑승 예정 학생 수 = 버스 배정 학생 - 오늘 결석 설정 학생"""
    ids = [r[0] for r in db.query(BusStudent.student_id).filter(BusStudent.bus_id == bus_id)]
    if not ids:
        return 0
    absent = (
        db.query(Attendance)
        .filter(Attendance.student_id.in_(ids), Attendance.date == date.today(),
                Attendance.is_absent.is_(True))
        .count()
    )
    return len(ids) - absent


async def read_image(image: UploadFile) -> bytes:
    if image.content_type and not image.content_type.startswith("image/"):
        raise HTTPException(400, "이미지 파일만 업로드할 수 있습니다")
    data = await image.read()
    if not data:
        raise HTTPException(400, "빈 파일입니다")
    return data


# ---------- 운행 시작 / 조회 ----------
@router.post("", status_code=201)
def start_trip(body: TripStart, me: User = Depends(require_admin), db: Session = Depends(get_db)):
    """운행 시작: trips 레코드 생성 (탑승 예정 인원 = 배정 - 오늘 결석)"""
    bus = db.get(Bus, body.bus_id)
    if not bus:
        raise HTTPException(404, "버스를 찾을 수 없습니다")
    running = (
        db.query(Trip)
        .filter(Trip.bus_id == body.bus_id, Trip.status == TripStatus.IN_PROGRESS)
        .first()
    )
    if running:
        raise HTTPException(409, f"이미 진행 중인 운행이 있습니다 (trip_id={running.id})")

    t = Trip(
        bus_id=bus.id,
        driver_id=me.id,
        expected_count=expected_students(db, bus.id),
        simulation_mode=body.simulation_mode,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return trip_to_dict(t)


@router.get("/current")
def current_trip(me: User = Depends(require_admin), db: Session = Depends(get_db)):
    """내 진행 중 운행 또는 404"""
    t = (
        db.query(Trip)
        .filter(Trip.driver_id == me.id, Trip.status == TripStatus.IN_PROGRESS)
        .order_by(Trip.id.desc())
        .first()
    )
    if not t:
        raise HTTPException(404, "진행 중인 운행이 없습니다")
    return trip_to_dict(t)


@router.get("")
def list_trips(
    status: TripStatus | None = None,
    result: TripResult | None = None,
    bus_id: int | None = None,
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = None,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    q = db.query(Trip)
    if status:
        q = q.filter(Trip.status == status)
    if result:
        q = q.filter(Trip.result == result)
    if bus_id:
        q = q.filter(Trip.bus_id == bus_id)
    if from_:
        q = q.filter(Trip.started_at >= from_)
    if to:
        q = q.filter(Trip.started_at <= to)
    total = q.count()
    rows = q.order_by(Trip.id.desc()).offset(offset).limit(limit).all()
    return {"items": [trip_to_dict(t) for t in rows], "total": total}


@router.get("/{trip_id}")
def get_trip(trip_id: int, db: Session = Depends(get_db)):
    """운행 상세, 이벤트 목록, 열린 경고"""
    t = get_trip_or_404(db, trip_id)
    return {
        **trip_to_dict(t),
        "events": [event_to_dict(e) for e in t.events],
        "open_alerts": [alert_to_dict(a) for a in notifier.open_alerts(db, t.id)],
    }


# ---------- 승차 사진 ----------
@router.post("/{trip_id}/boarding-photo")
async def boarding_photo(
    trip_id: int,
    image: UploadFile = File(...),
    captured_at: datetime = Form(...),
    exclude_count: int = Form(0, ge=0),          # 기사·선생님 제외 보정
    mock_count: int | None = Form(None, ge=0),   # mock 모드 전용
    mock_low_conf: int = Form(0, ge=0),          # mock 모드 전용
    db: Session = Depends(get_db),
):
    received = datetime.now()      # KPI 기준: 서버가 요청을 받은 시각
    t = get_trip_or_404(db, trip_id)
    if t.status != TripStatus.IN_PROGRESS:
        raise HTTPException(409, "종료된 운행입니다")
    if t.remaining_count is not None:
        raise HTTPException(409, "이미 종점 사진이 처리되어 승차 사진을 다시 올릴 수 없습니다")

    data = await read_image(image)
    dets, mode = detect_people(data, mock_count, mock_low_conf, default_count=t.expected_count or 0)
    person_count = len(dets)
    boarding_count = max(person_count - exclude_count, 0)

    ev = BoardingEvent(
        trip_id=t.id, event_type=EventType.BOARDING,
        image_path=storage.save_image(t.id, "boarding", image.filename, data),
        person_count=person_count, exclude_count=exclude_count,
        detections=dets, low_conf_count=low_conf_count(dets),
        captured_at=captured_at, uploaded_at=received, image_expires_at=storage.expires_at(),
    )
    db.add(ev)
    t.boarding_count = boarding_count          # 재촬영이면 마지막 값으로 갱신
    db.commit()
    db.refresh(ev)
    event_bus.publish("event", event_to_dict(ev))
    return {
        "event_id": ev.id, "person_count": person_count,
        "boarding_count": boarding_count, "low_conf_count": ev.low_conf_count,
        "detector_mode": mode,
    }


# ---------- 종점 사진 ----------
@router.post("/{trip_id}/dropoff-photo")
async def dropoff_photo(
    trip_id: int,
    image: UploadFile = File(...),
    captured_at: datetime = Form(...),
    exclude_count: int = Form(0, ge=0),
    mock_count: int | None = Form(None, ge=0),
    mock_low_conf: int = Form(0, ge=0),
    db: Session = Depends(get_db),
):
    received = datetime.now()      # KPI 기준: 서버가 요청을 받은 시각
    t = get_trip_or_404(db, trip_id)
    if t.status != TripStatus.IN_PROGRESS:
        raise HTTPException(409, "종료된 운행입니다")
    if t.boarding_count is None:
        raise HTTPException(409, "승차 사진이 먼저 필요합니다")

    data = await read_image(image)
    dets, mode = detect_people(data, mock_count, mock_low_conf, default_count=0)
    person_count = len(dets)
    remaining = max(person_count - exclude_count, 0)
    low = low_conf_count(dets)
    verdict = judge(t.boarding_count, remaining, low)
    path = storage.save_image(t.id, "dropoff", image.filename, data)

    # 종점(하차) 이벤트
    ev = BoardingEvent(
        trip_id=t.id, event_type=EventType.DROPOFF, image_path=path,
        person_count=person_count, exclude_count=exclude_count,
        detections=dets, low_conf_count=low,
        captured_at=captured_at, uploaded_at=received, image_expires_at=storage.expires_at(),
    )
    db.add(ev)
    db.flush()

    t.remaining_count = remaining
    t.alighted_count = verdict["alighted_count"]
    t.result = verdict["result"]

    alert = mismatch = None
    resolved: list = []
    if verdict["result"] == TripResult.OK:
        # 재촬영으로 정상이 되면 열려 있던 경고를 닫는다
        resolved = notifier.resolve_open_alerts(db, t.id, "재촬영 결과 정상 (자동 해결)")
    else:
        # 불일치 이벤트 로그 + 경고/알림
        mismatch = BoardingEvent(
            trip_id=t.id, event_type=EventType.MISMATCH, image_path=path,
            person_count=remaining, exclude_count=exclude_count,
            detections=dets, low_conf_count=low,
            captured_at=captured_at, uploaded_at=received, image_expires_at=storage.expires_at(),
        )
        db.add(mismatch)
        db.flush()
        alert = notifier.raise_alert(
            db, t, mismatch, verdict["level"], verdict["message"],
            boarding=t.boarding_count, remaining=remaining,
        )

    db.commit()

    # 실시간(SSE) 전송
    event_bus.publish("event", event_to_dict(ev))
    if mismatch:
        event_bus.publish("event", event_to_dict(mismatch))
    for a in ([alert] if alert else []) + resolved:
        event_bus.publish("alert", alert_to_dict(a))

    alert_id = alert.id if alert else None
    return {
        "event_id": ev.id,
        "remaining_count": remaining,
        "alighted_count": verdict["alighted_count"],
        "result": verdict["result"].value,
        "message": verdict["message"],
        "low_conf_count": low,
        "alert_id": alert_id,
        "detector_mode": mode,
    }


# ---------- 운행 종료 ----------
@router.post("/{trip_id}/confirm")
def confirm_trip(trip_id: int, body: ConfirmBody, db: Session = Depends(get_db)):
    """하차 확인 후 운행 종료. 결과가 OK 가 아니면 409"""
    t = get_trip_or_404(db, trip_id)
    if t.status == TripStatus.CLOSED:
        raise HTTPException(409, "이미 종료된 운행입니다")

    if body.simulation_mode != t.simulation_mode:
        raise HTTPException(
            status_code=400,
            detail="운행 시작 시 설정한 시뮬레이션 모드와 종료 방식이 일치하지 않습니다."
        )

    if t.simulation_mode and body.safety_checked:
        raise HTTPException(
            status_code=400,
            detail="시뮬레이션 운행은 실제 안전 확인 완료로 처리할 수 없습니다."
        )

    if not body.safety_checked and not body.simulation_mode:
        raise HTTPException(
            status_code=400,
            detail="차량 내부 안전 확인 후 운행을 종료할 수 있습니다."
        )

    if t.boarding_count is None or t.remaining_count is None:
        raise HTTPException(
            status_code=409,
            detail="승차 사진과 종점 사진이 모두 등록되어야 운행을 종료할 수 있습니다."
        )

    if t.result != TripResult.OK:
        unresolved_alert = (
            db.query(Alert)
            .filter(
                Alert.trip_id == trip_id,
                Alert.status != AlertStatus.RESOLVED
            )
            .first()
        )

        if unresolved_alert:
            raise HTTPException(
                status_code=409,
                detail="미해결 경고가 남아 있어 운행을 종료할 수 없습니다."
            )

        # 미하차 의심 경고가 모두 해결됐고
        # 관리자가 안전 확인을 요청한 경우 종료 허용
        t.status = TripStatus.CLOSED
        t.confirm_method = body.method
        # 시뮬레이션 여부는 운행 시작 시 결정하며 종료 시 변경하지 않는다.
        t.ended_at = datetime.now()
        db.commit()

        return {
            "id": t.id,
            "status": t.status.value,
            "result": t.result.value,
            "confirm_method": t.confirm_method.value,
            "ended_at": t.ended_at,
        }

