"""챗봇 API — 하차 확인 의무·대응 수칙·앱 사용법 안내 (RAG 기반 대화)

GET  /chat/hello                              → {reply, suggestions}  (채팅 화면을 열 때 첫 인사)
POST /chat  {message, history[], alert_id?}   → {reply, suggestions, sources, mode}
  - 관리자·학부모 모두 사용 (로그인 필요)
  - history: 이전 대화 [{"role": "user" | "bot", "text": "..."}] — 앱이 들고 있다가 매번 보낸다 (서버는 저장 안 함)
  - alert_id: 경고 상세 화면에서 챗봇을 열면 그 경고 상황을 함께 반영 (권한은 /rag/ask 와 같음)
  - 사용자 상황을 서버가 붙여 준다. 아이 이름은 Gemini 로 보내지 않는다 ('자녀1', '자녀2')
    · 학부모: 자녀별 배정 버스, 오늘·내일 결석, 오늘 운행(시작·종료 시각, 승차·하차 확인 시각, 종점 판정),
              오늘 예정 출발 시각, 내 아이 버스의 진행 중 긴급 경고
    · 관리자: 처리 안 된 경고, 운행 중인 버스
"""
from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.security import require_terms_agreed
from database.db_connection import get_db
from models.models import (
    Alert, AlertLevel, AlertStatus, Attendance, Bus, BusStudent, Guardian, Trip, TripResult, TripSchedule,
    TripStatus, TripStudentAttendance, User, UserRole,
)
from routers.rag import _alert_context, _alert_for, _check_rate, _parent_alert_query
from services import chat_service

router = APIRouter(prefix="/chat", tags=["챗봇"])


class ChatTurn(BaseModel):
    role: Literal["user", "bot"]
    text: str = Field("", max_length=2000)


class ChatBody(BaseModel):
    message: str = Field(..., min_length=1, max_length=500)
    history: list[ChatTurn] = Field(default_factory=list, max_length=20)
    alert_id: int | None = None


_LEVEL = {AlertLevel.URGENT: "긴급(미하차 의심)", AlertLevel.CHECK: "확인 필요"}
_STATUS = {AlertStatus.OPEN: "열림", AlertStatus.ACKED: "확인 중", AlertStatus.RESOLVED: "해결"}

_RESULT = {
    TripResult.PENDING: "종점 확인 전",
    TripResult.OK: "종점 확인 결과 차 안에 남은 아이 없음",
    TripResult.MISSED: "종점 확인 결과 미하차 의심",
    TripResult.NEEDS_CHECK: "종점 사진 확인 필요(재촬영·직접 확인 중)",
}


def _hm(t: datetime | None) -> str:
    return t.strftime("%H:%M") if t else "?"


def _is_absent(db: Session, sid: int, d: date) -> bool:
    return db.query(Attendance).filter(
        Attendance.student_id == sid, Attendance.date == d, Attendance.is_absent.is_(True),
    ).first() is not None


def _child_lines(db: Session, label: str, sid: int, today: date) -> list[str]:
    """자녀 한 명의 오늘 상황 (이름 없이 label 로)"""
    bs = db.query(BusStudent).filter(BusStudent.student_id == sid).first()
    bus = db.get(Bus, bs.bus_id) if bs else None
    absent_today = _is_absent(db, sid, today)
    absent_tomorrow = _is_absent(db, sid, today + timedelta(days=1))
    head = (f"{label}: {bus.name if bus else '버스 미배정'} · 오늘 {'결석' if absent_today else '탑승 예정'}"
            f" · 내일 {'결석 설정됨' if absent_tomorrow else '탑승 예정(결석 설정 없음)'}")
    out = [head]
    if not bus or absent_today:
        return out

    start = datetime.combine(today, datetime.min.time())
    trips = (db.query(Trip).filter(Trip.bus_id == bus.id, Trip.started_at >= start,
                                   Trip.started_at < start + timedelta(days=1),
                                   Trip.simulation_mode.is_(False))
             .order_by(Trip.started_at).all())
    for n, t in enumerate(trips[-2:], 1):           # 등원·하원처럼 하루 2번까지
        parts = [f"오늘 운행{n}: {_hm(t.started_at)} 출발"]
        parts.append(f"{_hm(t.ended_at)} 운행 종료" if t.status == TripStatus.CLOSED else "운행 중")
        att = db.query(TripStudentAttendance).filter(
            TripStudentAttendance.trip_id == t.id, TripStudentAttendance.student_id == sid,
        ).first()
        if att:
            parts.append(f"승차 확인 {_hm(att.boarding_confirmed_at)}" if att.boarding_confirmed_at
                         else "승차 확인 기록 없음")
            parts.append(f"하차 확인 {_hm(att.alighting_confirmed_at)}" if att.alighting_confirmed_at
                         else "하차 확인 기록 없음")
        parts.append(_RESULT.get(t.result, t.result.value))
        out.append(f"{label} " + " · ".join(parts))
    sched = (db.query(TripSchedule).filter(TripSchedule.bus_id == bus.id,
                                           TripSchedule.service_date == today,
                                           TripSchedule.trip_id.is_(None))
             .order_by(TripSchedule.scheduled_at).first())
    if sched and sched.scheduled_at >= datetime.now():
        out.append(f"{label} 다음 운행 예정 출발 {_hm(sched.scheduled_at)} (아직 시작 전)")
    elif sched:
        out.append(f"{label} 예정 출발 {_hm(sched.scheduled_at)} 운행이 아직 시작 기록 없음 (출발 지연 가능)")
    elif not trips:
        out.append(f"{label} 오늘 아직 운행 기록 없음 (예정 시각 정보 없음)")
    return out


def _user_context(db: Session, me: User) -> dict:
    """챗봇이 참고할 사용자 상황 (숫자·상태만, 아이 이름 없음)"""
    lines: list[str] = []
    if me.role == UserRole.ADMIN:
        active = (db.query(Alert).filter(Alert.status != AlertStatus.RESOLVED)
                  .order_by(Alert.created_at.desc()).all())
        lines.append(f"처리 안 된 경고 {len(active)}건")
        for a in active[:3]:
            bus = a.trip.bus.name if a.trip and a.trip.bus else "차량 미상"
            lines.append(f"{bus}: {_LEVEL.get(a.level, a.level.value)} · {_STATUS.get(a.status, a.status.value)}")
        running = db.query(Trip).filter(Trip.status == TripStatus.IN_PROGRESS).count()
        lines.append(f"운행 중인 버스 {running}대")
        return {"role": "ADMIN", "lines": lines}

    student_ids = [g.student_id for g in db.query(Guardian).filter(Guardian.user_id == me.id)]
    now = datetime.now()
    today = now.date()
    lines.append(f"지금 시각: {now:%m월 %d일 %H:%M}")
    if not student_ids:
        lines.append("연결된 자녀 없음 (자녀 연결 요청 → 기관 승인 후 이용 가능)")
    for i, sid in enumerate(student_ids, 1):
        lines.extend(_child_lines(db, f"자녀{i}", sid, today))
    alerts = (_parent_alert_query(db, me).filter(Alert.status != AlertStatus.RESOLVED)
              .order_by(Alert.created_at.desc()).all())
    if alerts:
        for a in alerts[:2]:
            bus = a.trip.bus.name if a.trip and a.trip.bus else "차량 미상"
            lines.append(f"진행 중 경고: {bus} {_LEVEL.get(a.level, a.level.value)} · "
                         f"{_STATUS.get(a.status, a.status.value)} · {_hm(a.created_at)} 발생")
    else:
        lines.append("내 아이 버스의 진행 중 긴급 경고 없음")
    return {"role": "PARENT", "lines": lines}


@router.get("/hello")
def chat_hello(me: User = Depends(require_terms_agreed)):
    return chat_service.hello(me.role.value)


@router.post("")
async def chat(body: ChatBody, me: User = Depends(require_terms_agreed), db: Session = Depends(get_db)):
    context = _user_context(db, me)
    if body.alert_id is not None:
        a = _alert_for(db, me, body.alert_id)           # 남의 경고면 404
        context["lines"] = ["[지금 보고 있는 경고] " + _alert_context(a).replace("\n", " / ")] + context["lines"]
    _check_rate(me.id)                                   # /rag/ask 와 같은 분당 한도
    history = [h.model_dump() for h in body.history]
    return await run_in_threadpool(chat_service.chat, body.message.strip(), history, context)
