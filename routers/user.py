import string

import bcrypt
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.security import create_access_token, get_current_user, require_admin
from database.db_connection import get_db
from models.models import ChildRequest, User, UserRole
from services.child_requests import (
    MAX_CHILDREN_AT_SIGNUP, ChildInput, dedupe, make_requests, parent_request_dict,
)

router = APIRouter(tags=["계정"])

PASSWORD_MIN_LENGTH = 4         # 비밀번호 최소 길이 (바꾸려면 이 숫자만 수정)
BCRYPT_MAX_BYTES = 72           # bcrypt 는 비밀번호를 72바이트까지만 처리한다 (한글은 글자당 3바이트)


def check_password(v: str) -> str:
    """가입 비밀번호 규칙: 4자 이상 + 영문 대문자 1개 이상 + 특수문자 1개 이상"""
    missing = []
    if len(v) < PASSWORD_MIN_LENGTH:
        missing.append(f"{PASSWORD_MIN_LENGTH}자 이상")
    if not any("A" <= c <= "Z" for c in v):
        missing.append("영문 대문자 1개 이상")
    if not any(c in string.punctuation for c in v):
        missing.append("특수문자 1개 이상")
    if missing:
        raise ValueError("비밀번호 조건을 만족하지 않습니다: " + ", ".join(missing))
    if len(v.encode("utf-8")) > BCRYPT_MAX_BYTES:
        raise ValueError("비밀번호가 너무 깁니다 (영문·숫자 72자, 한글 24자까지)")
    return v


class SignupRequest(BaseModel):
    """학부모 회원가입. role 필드가 없으므로 요청에 role 을 넣어도 무시되고 항상 PARENT 로 가입된다."""
    email: EmailStr
    password: str
    name: str = Field(min_length=1, max_length=100)
    # 자녀 연결 신청 (선택, 최대 5명). 가입 후 관리자가 확인해서 학생에 연결한다
    children: list[ChildInput] = Field(default_factory=list, max_length=MAX_CHILDREN_AT_SIGNUP)

    @field_validator("password")
    @classmethod
    def _password(cls, v: str) -> str:
        return check_password(v)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("이름을 입력하세요")
        return v


class UserCreate(BaseModel):
    """관리자가 계정을 직접 만들 때 (role 선택 가능). 비밀번호 규칙은 가입과 같다."""
    email: EmailStr
    password: str
    name: str
    role: UserRole = UserRole.PARENT

    @field_validator("password")
    @classmethod
    def _password(cls, v: str) -> str:
        return check_password(v)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class PushTokenRequest(BaseModel):
    push_token: str


# ---------- 공용 도구 ----------
def user_to_dict(u: User) -> dict:
    return {"id": u.id, "email": u.email, "name": u.name, "role": u.role.value}


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except ValueError:      # 72바이트 초과 등 bcrypt 가 처리할 수 없는 입력 -> 그냥 불일치로 처리
        return False


def find_user_by_email(db: Session, email: str) -> User | None:
    """이메일은 대소문자를 구분하지 않는다 (Kim@x.kr 와 kim@x.kr 는 같은 계정)"""
    return db.query(User).filter(func.lower(User.email) == email.lower()).first()


def auth_response(u: User) -> dict:
    return {
        "access_token": create_access_token(u),
        "token_type": "bearer",
        "role": u.role.value,
        "user": user_to_dict(u),
    }


def authenticate(db: Session, body: LoginRequest) -> User:
    u = find_user_by_email(db, body.email)
    if not u or not verify_password(body.password, u.password_hash):
        raise HTTPException(401, "이메일 또는 비밀번호가 올바르지 않습니다")
    return u


def create_user_row(
    db: Session, email: str, password: str, name: str, role: UserRole,
    children: list[ChildInput] | None = None,
) -> User:
    """계정 생성 공통 처리 (자녀 연결 신청이 있으면 같은 트랜잭션에서 함께 저장). 이미 있는 이메일이면 409"""
    email = email.lower()
    if find_user_by_email(db, email):
        raise HTTPException(409, "이미 가입된 이메일입니다")
    u = User(email=email, password_hash=hash_password(password), name=name, role=role)
    db.add(u)
    try:
        db.flush()                      # id 확보. 거의 동시에 같은 이메일이 들어오면 여기서 unique 제약에 걸린다
        db.add_all(make_requests(u.id, dedupe(children or [])))
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "이미 가입된 이메일입니다")
    db.refresh(u)
    return u


# ---------- 가입 / 로그인 (누구나) ----------
@router.post("/auth/signup", status_code=201)
def signup(body: SignupRequest, db: Session = Depends(get_db)):
    """학부모 회원가입 (앱): email, password, name, children[] -> access_token, role, user, child_requests
    - 비밀번호: 4자 이상 + 영문 대문자 + 특수문자 포함
    - children 에 자녀 이름·반·관계를 적어 보내면 '승인 대기' 요청이 만들어지고, 관리자가 확인해서 학생에 연결한다
    - 가입 즉시 로그인된 상태의 토큰을 돌려준다. role 은 항상 PARENT (관리자 계정은 가입으로 만들 수 없다)"""
    u = create_user_row(db, body.email, body.password, body.name, UserRole.PARENT, body.children)
    reqs = db.query(ChildRequest).filter(ChildRequest.user_id == u.id).order_by(ChildRequest.id).all()
    return {**auth_response(u), "child_requests": [parent_request_dict(r) for r in reqs]}


@router.post("/auth/login")
def login(body: LoginRequest, db: Session = Depends(get_db)):
    """앱 로그인: 학부모와 기사(관리자 계정) 모두 가능. email, password -> access_token, role, user"""
    return auth_response(authenticate(db, body))


@router.post("/web/login")
def web_login(body: LoginRequest, db: Session = Depends(get_db)):
    """관리자 웹 로그인: 관리자 계정만 가능. 학부모는 비밀번호가 맞아도 403 (앱에서만 로그인)"""
    u = authenticate(db, body)
    if u.role != UserRole.ADMIN:
        raise HTTPException(403, "관리자 계정만 이 화면에 로그인할 수 있습니다. 학부모는 앱에서 로그인하세요.")
    return auth_response(u)


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
    return user_to_dict(create_user_row(db, body.email, body.password, body.name, body.role))


@router.get("/web/users", dependencies=[Depends(require_admin)])
def list_users(role: UserRole | None = None, db: Session = Depends(get_db)):
    q = db.query(User)
    if role:
        q = q.filter(User.role == role)
    return [user_to_dict(u) for u in q.all()]
