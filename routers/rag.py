"""Agent·RAG API — 하차 확인 의무·대응 수칙 안내

POST /rag/ask   {question, alert_id(선택)} → {answer, sources, mode}
  - 관리자·학부모 모두 사용 (로그인 필요)
  - alert_id 를 주면 그 경고 상황(등급·인원)을 함께 넘겨 맞춤 답변
    · 학부모는 내 아이 버스의 긴급 경고만 (아니면 404) — /me/alerts 와 같은 기준
  - question 을 비우고 alert_id 만 주면 경고 등급에 맞는 '대응 수칙' 기본 질문으로 답한다
    → 앱 경고 상세의 '대응 수칙' 카드에서 그대로 호출하면 된다
"""
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.security import get_current_user
from database.db_connection import get_db
from models.models import Alert, User, UserRole
try:
    from routers.alert import _parent_alert_query
except ImportError:   # routers/alert.py 에 없는 버전이면 같은 조건으로 직접 정의
    from sqlalchemy import exists, func
    from models.models import AlertLevel, Attendance, BusStudent, Guardian, Trip

    def _parent_alert_query(db: Session, me: User):
        """내 아이가 배정된 버스의 긴급 경고 (그날 결석 설정한 아이만 있는 경우는 제외)"""
        was_absent = exists().where(
            Attendance.student_id == BusStudent.student_id,
            Attendance.date == func.date(Trip.started_at),
            Attendance.is_absent.is_(True),
        )
        return (
            db.query(Alert)
            .join(Trip, Trip.id == Alert.trip_id)
            .join(BusStudent, BusStudent.bus_id == Trip.bus_id)
            .join(Guardian, Guardian.student_id == BusStudent.student_id)
            .filter(Guardian.user_id == me.id, Alert.level == AlertLevel.URGENT, ~was_absent)
            .distinct()
        )
from services import rag_service

router = APIRouter(prefix="/rag", tags=["Agent·RAG"])

# Gemini 무료 할당량 보호용: 사용자당 1분에 이만큼만 (서버 메모리 기준, 단일 프로세스)
RATE_LIMIT_PER_MIN = 10
_calls: dict[int, deque] = defaultdict(deque)


class AskBody(BaseModel):
    question: str | None = Field(None, max_length=500)
    alert_id: int | None = None


def _check_rate(user_id: int) -> None:
    now = time.monotonic()
    q = _calls[user_id]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_LIMIT_PER_MIN:
        raise HTTPException(429, "질문이 너무 많습니다. 잠시 후 다시 시도해 주세요")
    q.append(now)


def _alert_for(db: Session, me: User, alert_id: int) -> Alert:
    if me.role == UserRole.ADMIN:
        a = db.get(Alert, alert_id)
    else:
        a = _parent_alert_query(db, me).filter(Alert.id == alert_id).first()
    if not a:
        raise HTTPException(404, "경고를 찾을 수 없습니다")
    return a


def _alert_context(a: Alert) -> str:
    t = a.trip
    lines = [
        f"등급: {'긴급(미하차 의심)' if a.level.value == 'URGENT' else '확인 필요'}",
        f"상태: {a.status.value}",
        f"내용: {a.message or ''}",
    ]
    if t is not None:
        lines.append(f"승차 {t.boarding_count if t.boarding_count is not None else '-'}명 / "
                     f"종점 잔류 {t.remaining_count if t.remaining_count is not None else '-'}명")
    return "\n".join(lines)


@router.post("/ask")
async def ask(body: AskBody, me: User = Depends(get_current_user), db: Session = Depends(get_db)):
    question = (body.question or "").strip()
    context = None
    if body.alert_id is not None:
        a = _alert_for(db, me, body.alert_id)
        context = _alert_context(a)
        question = question or rag_service.guide_question(a.level.value)
    if not question:
        raise HTTPException(400, "질문을 입력해 주세요")

    _check_rate(me.id)
    # Gemini 호출은 네트워크를 기다리므로 스레드에서 (다른 요청이 멈추지 않게)
    return await run_in_threadpool(rag_service.answer, question, context)
