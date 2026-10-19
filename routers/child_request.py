from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from core.security import require_admin, require_parent
from database.db_connection import get_db
from models.models import ChildRequest, ChildRequestStatus, Guardian, Student, User
from services.child_requests import (
    MAX_PENDING_PER_USER, ChildInput, admin_request_dict, parent_request_dict,
)

router = APIRouter(tags=["자녀 연결"])


def _get_request_or_404(db: Session, request_id: int) -> ChildRequest:
    r = db.get(ChildRequest, request_id)
    if not r:
        raise HTTPException(404, "요청을 찾을 수 없습니다")
    return r


# ====================== 학부모 (앱) ======================
@router.get("/me/child-requests")
def my_child_requests(me: User = Depends(require_parent), db: Session = Depends(get_db)):
    """내가 올린 자녀 연결 요청과 처리 상태 (PENDING / APPROVED / REJECTED + 거절 사유)"""
    rows = db.query(ChildRequest).filter(ChildRequest.user_id == me.id).order_by(ChildRequest.id.desc()).all()
    return [parent_request_dict(r) for r in rows]


@router.post("/me/child-requests", status_code=201)
def add_child_request(body: ChildInput, me: User = Depends(require_parent), db: Session = Depends(get_db)):
    """앱에서 자녀 추가: 이름·반·관계를 적어 올리면 관리자 승인을 기다린다"""
    pending = db.query(ChildRequest).filter(
        ChildRequest.user_id == me.id, ChildRequest.status == ChildRequestStatus.PENDING)
    if pending.count() >= MAX_PENDING_PER_USER:
        raise HTTPException(409, f"승인 대기 중인 요청이 너무 많습니다 (최대 {MAX_PENDING_PER_USER}건)")

    same_pending = pending.filter(func.lower(ChildRequest.child_name) == body.name.lower())
    same_pending = same_pending.filter(ChildRequest.class_name == body.class_name) if body.class_name \
        else same_pending.filter(ChildRequest.class_name.is_(None))
    if same_pending.first():
        raise HTTPException(409, "같은 자녀로 승인 대기 중인 요청이 있습니다")

    linked = (
        db.query(Student).join(Guardian, Guardian.student_id == Student.id)
        .filter(Guardian.user_id == me.id, func.lower(Student.name) == body.name.lower())
    )
    if body.class_name:
        linked = linked.filter(Student.class_name == body.class_name)
    if linked.first():
        raise HTTPException(409, "이미 연결된 자녀입니다")

    r = ChildRequest(user_id=me.id, child_name=body.name, class_name=body.class_name, relation=body.relation)
    db.add(r)
    db.commit()
    db.refresh(r)
    return parent_request_dict(r)


@router.delete("/me/child-requests/{request_id}", status_code=204)
def cancel_child_request(request_id: int, me: User = Depends(require_parent), db: Session = Depends(get_db)):
    """승인 대기 중인 내 요청 취소 (잘못 적었을 때)"""
    r = db.get(ChildRequest, request_id)
    if not r or r.user_id != me.id:            # 남의 요청은 존재 여부도 숨김
        raise HTTPException(404, "요청을 찾을 수 없습니다")
    if r.status != ChildRequestStatus.PENDING:
        raise HTTPException(409, "이미 처리된 요청은 취소할 수 없습니다")
    db.delete(r)
    db.commit()


# ====================== 관리자 (웹) ======================
@router.get("/web/child-requests", dependencies=[Depends(require_admin)])
def list_child_requests(
    status: ChildRequestStatus | None = None,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """연결 요청 목록. 승인 대기 건에는 이름이 같은 학생(candidates)을 함께 알려준다"""
    q = db.query(ChildRequest)
    if status:
        q = q.filter(ChildRequest.status == status)
    total = q.count()
    # 대기 목록은 먼저 들어온 순서대로, 그 외에는 최근 순
    q = q.order_by(ChildRequest.id.asc() if status == ChildRequestStatus.PENDING else ChildRequest.id.desc())
    items = []
    for r in q.offset(offset).limit(limit).all():
        cands = []
        if r.status == ChildRequestStatus.PENDING:
            same = db.query(Student).filter(func.lower(Student.name) == r.child_name.lower()).all()
            cands = sorted(
                ({"id": s.id, "name": s.name, "class_name": s.class_name,
                  "class_match": bool(r.class_name) and s.class_name == r.class_name} for s in same),
                key=lambda c: not c["class_match"])
        items.append(admin_request_dict(r, cands))
    return {"items": items, "total": total}


class ApproveBody(BaseModel):
    student_id: int


class RejectBody(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


@router.post("/web/child-requests/{request_id}/approve")
def approve_child_request(
    request_id: int, body: ApproveBody,
    me: User = Depends(require_admin), db: Session = Depends(get_db),
):
    """관리자가 학생을 골라 승인 -> 그 학부모가 학생의 보호자로 연결된다 (이후 그 아이 버스의 경고를 받음)"""
    r = _get_request_or_404(db, request_id)
    if r.status != ChildRequestStatus.PENDING:
        raise HTTPException(409, "이미 처리된 요청입니다")
    student = db.get(Student, body.student_id)
    if not student:
        raise HTTPException(404, "학생을 찾을 수 없습니다")
    already = db.query(Guardian).filter(Guardian.user_id == r.user_id, Guardian.student_id == student.id).first()
    if not already:
        db.add(Guardian(user_id=r.user_id, student_id=student.id, relation=r.relation))
    r.status = ChildRequestStatus.APPROVED
    r.student_id = student.id
    r.decided_by = me.id
    r.decided_at = datetime.now()
    db.commit()
    db.refresh(r)
    return admin_request_dict(r)


@router.post("/web/child-requests/{request_id}/reject")
def reject_child_request(
    request_id: int, body: RejectBody,
    me: User = Depends(require_admin), db: Session = Depends(get_db),
):
    r = _get_request_or_404(db, request_id)
    if r.status != ChildRequestStatus.PENDING:
        raise HTTPException(409, "이미 처리된 요청입니다")
    r.status = ChildRequestStatus.REJECTED
    r.reject_reason = (body.reason or "").strip() or None
    r.decided_by = me.id
    r.decided_at = datetime.now()
    db.commit()
    db.refresh(r)
    return admin_request_dict(r)
