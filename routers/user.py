import bcrypt
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from core.security import create_access_token, get_current_user, require_admin
from database.db_connection import get_db
from models.models import User, UserRole

router = APIRouter(tags=["계정"])


class UserCreate(BaseModel):
    email: EmailStr
    password: str
    name: str
    role: UserRole = UserRole.PARENT


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class PushTokenRequest(BaseModel):
    push_token: str


def user_to_dict(u: User) -> dict:
    return {"id": u.id, "email": u.email, "name": u.name, "role": u.role.value}


# ---------- 로그인 (누구나) ----------
@router.post("/auth/login")
def login(body: LoginRequest, db: Session = Depends(get_db)):
    """email, password -> access_token, role, user"""
    u = db.query(User).filter(User.email == body.email).first()
    if not u or not bcrypt.checkpw(body.password.encode(), u.password_hash.encode()):
        raise HTTPException(401, "이메일 또는 비밀번호가 올바르지 않습니다")
    return {
        "access_token": create_access_token(u),
        "token_type": "bearer",
        "role": u.role.value,
        "user": user_to_dict(u),
    }


# ---------- 로그인한 사용자(관리자/학부모 공통) ----------
@router.get("/me")
def get_me(me: User = Depends(get_current_user)):
    return user_to_dict(me)


@router.put("/me/push-token", status_code=204)
def put_push_token(
    body: PushTokenRequest,
    me: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    me.push_token = body.push_token
    db.commit()


# ---------- 관리자 전용 ----------
@router.post("/web/users", status_code=201, dependencies=[Depends(require_admin)])
def create_user(body: UserCreate, db: Session = Depends(get_db)):
    """email, password, name, role -> user"""
    if db.query(User).filter(User.email == body.email).first():
        raise HTTPException(409, "이미 등록된 이메일입니다")
    hashed = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    u = User(email=body.email, password_hash=hashed, name=body.name, role=body.role)
    db.add(u)
    db.commit()
    db.refresh(u)
    return user_to_dict(u)


@router.get("/web/users", dependencies=[Depends(require_admin)])
def list_users(role: UserRole | None = None, db: Session = Depends(get_db)):
    q = db.query(User)
    if role:
        q = q.filter(User.role == role)
    return [user_to_dict(u) for u in q.all()]
