"""챗봇 API — 하차 확인 의무·대응 수칙·앱 사용법 안내 (RAG 기반 대화)

GET  /chat/hello                              → {reply, suggestions}  (채팅 화면을 열 때 첫 인사)
POST /chat  {message, history[], alert_id?}   → {reply, suggestions, sources, mode}
  - 관리자·학부모 모두 사용 (로그인 필요)
  - history: 이전 대화 [{"role": "user" | "bot", "text": "..."}] — 앱이 들고 있다가 매번 보낸다 (서버는 저장 안 함)
  - alert_id: 경고 상세 화면에서 챗봇을 열면 그 경고 상황을 함께 반영 (권한은 /rag/ask 와 같음)
  - 사용자 상황(진행 중 경고·오늘 결석)을 서버가 붙여 준다. 아이 이름은 Gemini 로 보내지 않는다
"""
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.security import get_current_user
from database.db_connection import get_db
from models.models import (
    Alert, AlertLevel, AlertStatus, Attendance, Bus, BusStudent, Guardian, Trip, TripStatus, User, UserRole,
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
    if not student_ids:
        lines.append("연결된 자녀 없음")
    today = date.today()
    for i, sid in enumerate(student_ids, 1):
        bs = db.query(BusStudent).filter(BusStudent.student_id == sid).first()
        bus = db.get(Bus, bs.bus_id) if bs else None
        absent = db.query(Attendance).filter(
            Attendance.student_id == sid, Attendance.date == today, Attendance.is_absent.is_(True),
        ).first() is not None
        trip = None
        if bus:
            trip = (db.query(Trip).filter(Trip.bus_id == bus.id)
                    .order_by(Trip.started_at.desc()).first())
        state = "운행 중" if trip and trip.status == TripStatus.IN_PROGRESS else "운행 중 아님"
        lines.append(f"자녀{i}: {bus.name if bus else '버스 미배정'} · {'오늘 결석' if absent else '오늘 등원'} · {state}")
    active = _parent_alert_query(db, me).filter(Alert.status != AlertStatus.RESOLVED).count()
    lines.append(f"내 아이 버스의 진행 중 긴급 경고 {active}건")
    return {"role": "PARENT", "lines": lines}


@router.get("/hello")
def chat_hello(me: User = Depends(get_current_user)):
    return chat_service.hello(me.role.value)


@router.post("")
async def chat(body: ChatBody, me: User = Depends(get_current_user), db: Session = Depends(get_db)):
    context = _user_context(db, me)
    if body.alert_id is not None:
        a = _alert_for(db, me, body.alert_id)           # 남의 경고면 404
        context["lines"] = ["[지금 보고 있는 경고] " + _alert_context(a).replace("\n", " / ")] + context["lines"]
    _check_rate(me.id)                                   # /rag/ask 와 같은 분당 한도
    history = [h.model_dump() for h in body.history]
    return await run_in_threadpool(chat_service.chat, body.message.strip(), history, context)
