from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database.db_connection import get_db
from models.models import Guardian, Student, User

router = APIRouter(prefix="/web/students", tags=["관리-보호자"])


class GuardianLink(BaseModel):
    user_id: int
    relation: str | None = None


@router.post("/{student_id}/guardians", status_code=204)
def link_guardian(student_id: int, body: GuardianLink, db: Session = Depends(get_db)):
    """학생 ↔ 보호자(users) 연결"""
    if not db.get(Student, student_id):
        raise HTTPException(404, "학생을 찾을 수 없습니다")
    if not db.get(User, body.user_id):
        raise HTTPException(404, "사용자를 찾을 수 없습니다")
    exists = (
        db.query(Guardian)
        .filter(Guardian.student_id == student_id, Guardian.user_id == body.user_id)
        .first()
    )
    if not exists:
        db.add(Guardian(student_id=student_id, user_id=body.user_id, relation=body.relation))
        db.commit()


@router.get("/{student_id}/guardians")
def list_guardians(student_id: int, db: Session = Depends(get_db)):
    rows = (
        db.query(Guardian, User)
        .join(User, User.id == Guardian.user_id)
        .filter(Guardian.student_id == student_id)
        .all()
    )
    return [{"user_id": u.id, "name": u.name, "relation": g.relation} for g, u in rows]
