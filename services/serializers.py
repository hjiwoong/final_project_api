"""응답용 dict 변환 (라우터 공용)"""
from datetime import datetime

from models.models import Alert, BoardingEvent, Trip


def trip_to_dict(t: Trip) -> dict:
    return {
        "id": t.id,
        "bus_id": t.bus_id,
        "bus_name": t.bus.name if t.bus else None,
        "plate_no": t.bus.plate_no if t.bus else None,
        "driver_id": t.driver_id,
        "status": t.status.value,
        "result": t.result.value,
        "simulation_mode": t.simulation_mode,
        "expected_count": t.expected_count,
        "boarding_count": t.boarding_count,
        "remaining_count": t.remaining_count,
        "alighted_count": t.alighted_count,
        "confirm_method": t.confirm_method.value if t.confirm_method else None,
        "started_at": t.started_at,
        "ended_at": t.ended_at,
    }


def image_available(e: BoardingEvent) -> bool:
    if not e.image_path:
        return False
    return not (e.image_expires_at and e.image_expires_at < datetime.now())


def event_to_dict(e: BoardingEvent, with_detections: bool = False) -> dict:
    d = {
        "id": e.id,
        "trip_id": e.trip_id,
        "event_type": e.event_type.value,
        "person_count": e.person_count,
        "exclude_count": e.exclude_count,
        "low_conf_count": e.low_conf_count,
        "captured_at": e.captured_at,
        "uploaded_at": e.uploaded_at,
        "image_available": image_available(e),
    }
    if with_detections:
        d["detections"] = e.detections or []
    return d


def alert_to_dict(a: Alert) -> dict:
    return {
        "id": a.id,
        "trip_id": a.trip_id,
        "event_id": a.event_id,
        "bus_name": a.trip.bus.name if a.trip and a.trip.bus else None,
        "level": a.level.value,
        "status": a.status.value,
        "message": a.message,
        "llm_summary": a.llm_summary,
        "note": a.note,
        "created_at": a.created_at,
        "acked_at": a.acked_at,
        "resolved_at": a.resolved_at,
    }


def alert_to_parent_dict(a: Alert) -> dict:
    """학부모에게는 내부 정보(trip/event id, 조치 메모) 없이 필요한 것만"""
    return {
        "id": a.id,
        "bus_name": a.trip.bus.name if a.trip and a.trip.bus else None,
        "level": a.level.value,
        "status": a.status.value,
        "message": a.message,
        "llm_summary": a.llm_summary,
        "created_at": a.created_at,
    }
