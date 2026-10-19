"""관리자 웹 '학생 관리' 화면용 API: 목록·검색, 일괄 등록, 수정, 삭제(졸업 처리)

기존 routers/student.py 는 건드리지 않고 별도 파일로 둔다 (팀원 코드와 충돌 방지).
학생을 삭제하면 그 학생의 보호자 연결 · 버스 배정 · 출결 기록도 함께 지워진다.
(학부모 계정 자체와 운행·경고 기록은 그대로 남는다)
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, or_, update
from sqlalchemy.orm import Session

from core.security import require_admin
from database.db_connection import get_db
from models.models import Attendance, Bus, BusStudent, ChildRequest, Guardian, Student, User

router = APIRouter(tags=["관리-학생관리"], dependencies=[Depends(require_admin)])

MAX_BULK = 500


class StudentIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    class_name: str | None = Field(default=None, max_length=50)
    consent_at: datetime | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("이름을 입력하세요")
        return v

    @field_validator("class_name")
    @classmethod
    def _class(cls, v):
        v = (v or "").strip()
        return v or None


class BulkCreate(BaseModel):
    students: list[StudentIn] = Field(min_length=1, max_length=MAX_BULK)


class StudentUpdate(StudentIn):
    """이름·반 수정 (consent_at 은 이 API 에서 무시)"""


class BulkDelete(BaseModel):
    """ids 로 고르거나, class_name 으로 한 반 전체를 지정한다 (둘 중 하나만)"""
    ids: list[int] | None = Field(default=None, max_length=2000)
    class_name: str | None = Field(default=None, max_length=50)

    @model_validator(mode="after")
    def _one_of(self):
        if bool(self.ids) == bool(self.class_name and self.class_name.strip()):
            raise ValueError("삭제할 학생(ids) 또는 반(class_name) 중 하나만 보내세요")
        return self


def _row(s: Student, guardians: list[dict], buses: list[str]) -> dict:
    return {
        "id": s.id, "name": s.name, "class_name": s.class_name, "consent_at": s.consent_at,
        "guardians": guardians, "guardian_count": len(guardians), "buses": buses,
    }


def _delete_students(db: Session, ids: list[int]) -> int:
    """학생과 딸린 기록 삭제. DB 의 ON DELETE CASCADE 에 기대지 않고 직접 지운다 (ORM 이 FK 를 NULL 로 바꾸려다 실패하는 것도 방지)"""
    if not ids:
        return 0
    db.query(Guardian).filter(Guardian.student_id.in_(ids)).delete(synchronize_session=False)
    db.query(BusStudent).filter(BusStudent.student_id.in_(ids)).delete(synchronize_session=False)
    db.query(Attendance).filter(Attendance.student_id.in_(ids)).delete(synchronize_session=False)
    # 연결 요청 기록은 남기되 학생 연결만 끊는다 (승인 이력 보존)
    db.execute(update(ChildRequest).where(ChildRequest.student_id.in_(ids)).values(student_id=None))
    n = db.query(Student).filter(Student.id.in_(ids)).delete(synchronize_session=False)
    db.commit()
    return n


@router.get("/web/student-roster")
def roster(
    q: str | None = Query(None, description="이름 검색"),
    class_name: str | None = Query(None, description="반 (정확히 일치). '-' 는 반 미지정"),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """학생 목록 -> {items, total, classes}. items 에는 보호자 목록(이름·관계)과 배정된 버스 이름이 들어 있다"""
    query = db.query(Student)
    if q and q.strip():
        kw = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.filter(Student.name.like(f"%{kw}%", escape="\\"))
    if class_name == "-":
        query = query.filter(or_(Student.class_name.is_(None), Student.class_name == ""))
    elif class_name:
        query = query.filter(Student.class_name == class_name)
    total = query.count()
    items = query.order_by(Student.class_name, Student.name, Student.id).limit(limit).offset(offset).all()
    ids = [s.id for s in items]

    guardians: dict[int, list[dict]] = {}
    if ids:
        for sid, uid, uname, rel in (
            db.query(Guardian.student_id, User.id, User.name, Guardian.relation)
            .join(User, User.id == Guardian.user_id).filter(Guardian.student_id.in_(ids))
            .order_by(Guardian.id).all()
        ):
            guardians.setdefault(sid, []).append({"user_id": uid, "name": uname, "relation": rel})
    buses: dict[int, list[str]] = {}
    if ids:
        for sid, bname in (
            db.query(BusStudent.student_id, Bus.name)
            .join(Bus, Bus.id == BusStudent.bus_id).filter(BusStudent.student_id.in_(ids)).all()
        ):
            buses.setdefault(sid, []).append(bname)

    classes = [c for (c,) in db.query(Student.class_name).filter(Student.class_name.isnot(None))
               .filter(Student.class_name != "").distinct().order_by(Student.class_name).all()]
    return {
        "items": [_row(s, guardians.get(s.id, []), buses.get(s.id, [])) for s in items],
        "total": total,
        "classes": classes,
    }


@router.post("/web/students/bulk", status_code=201)
def bulk_create(body: BulkCreate, db: Session = Depends(get_db)):
    """학생 일괄 등록 (한 명이어도 사용 가능). 같은 이름+반이 이미 있거나 목록 안에서 겹치면 건너뛴다
    -> {created, skipped:[{name,class_name,reason}]}"""
    existing = {(n, c or None) for n, c in db.query(Student.name, Student.class_name).all()}
    created, skipped = 0, []
    for s in body.students:
        key = (s.name, s.class_name)
        if key in existing:
            skipped.append({"name": s.name, "class_name": s.class_name, "reason": "이미 등록된 학생(같은 이름·반)"})
            continue
        existing.add(key)
        db.add(Student(name=s.name, class_name=s.class_name, consent_at=s.consent_at))
        created += 1
    db.commit()
    return {"created": created, "skipped": skipped}


@router.put("/web/students/{student_id}")
def update_student(student_id: int, body: StudentUpdate, db: Session = Depends(get_db)):
    """이름·반 수정 (오타 정정, 진급)"""
    s = db.get(Student, student_id)
    if not s:
        raise HTTPException(404, "학생을 찾을 수 없습니다")
    dup = db.query(Student).filter(Student.id != s.id, Student.name == body.name,
                                   Student.class_name == body.class_name).first()
    if dup:
        raise HTTPException(409, "같은 이름·반의 학생이 이미 있습니다")
    s.name, s.class_name = body.name, body.class_name
    db.commit()
    return {"id": s.id, "name": s.name, "class_name": s.class_name}


@router.delete("/web/students/{student_id}", status_code=204)
def delete_student(student_id: int, db: Session = Depends(get_db)):
    if not db.get(Student, student_id):
        raise HTTPException(404, "학생을 찾을 수 없습니다")
    _delete_students(db, [student_id])


@router.post("/web/students/bulk-delete")
def bulk_delete(body: BulkDelete, db: Session = Depends(get_db)):
    """선택한 학생들 또는 한 반 전체 삭제 (졸업 처리) -> {deleted}"""
    if body.ids:
        ids = [i for (i,) in db.query(Student.id).filter(Student.id.in_(body.ids)).all()]
    else:
        ids = [i for (i,) in db.query(Student.id).filter(Student.class_name == body.class_name.strip()).all()]
    return {"deleted": _delete_students(db, ids)}
