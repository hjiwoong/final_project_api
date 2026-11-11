import string
from datetime import datetime, timedelta
from math import ceil

import bcrypt
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.security import create_access_token, get_current_user, require_admin
from database.db_connection import get_db
from models.models import ChildRequest, PushToken, SocialAccount, User, UserRole
from services.account_deletions import ACCOUNT_DELETION_GRACE_DAYS
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


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _new_password(cls, v: str) -> str:
        return check_password(v)


class AccountDeletionRequest(BaseModel):
    current_password: str | None = Field(default=None, min_length=1)
    google_id_token: str | None = Field(default=None, min_length=10)

    @model_validator(mode="after")
    def require_one_credential(self):
        if (self.current_password is None) == (self.google_id_token is None):
            raise ValueError("비밀번호 또는 구글 ID 토큰 중 하나를 입력하세요")
        return self


class CancelAccountDeletionRequest(BaseModel):
    email: EmailStr
    password: str | None = None
    google_id_token: str | None = Field(default=None, min_length=10)

    @model_validator(mode="after")
    def require_one_credential(self):
        if (self.password is None) == (self.google_id_token is None):
            raise ValueError("비밀번호 또는 구글 ID 토큰 중 하나를 입력하세요")
        return self


class PushTokenRequest(BaseModel):
    push_token: str = Field(min_length=1, max_length=512)


# ---------- 공용 도구 ----------
def user_to_dict(u: User) -> dict:
    return {
        "id": u.id,
        "email": u.email,
        "name": u.name,
        "role": u.role.value,
        "signup_method": u.signup_method or "UNKNOWN",
        "created_at": u.created_at,
    }


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
    if u.deletion_requested_at is not None:
        raise HTTPException(403, "계정 삭제 유예 중입니다. 계정 복구 API를 이용하세요")
    return u


def create_user_row(
    db: Session, email: str, password: str, name: str, role: UserRole,
    children: list[ChildInput] | None = None,
    signup_method: str = "APP",
) -> User:
    """계정 생성 공통 처리 (자녀 연결 신청이 있으면 같은 트랜잭션에서 함께 저장). 이미 있는 이메일이면 409"""
    email = email.lower()
    if find_user_by_email(db, email):
        raise HTTPException(409, "이미 가입된 이메일입니다")
    u = User(
        email=email,
        password_hash=hash_password(password),
        name=name,
        role=role,
        signup_method=signup_method,
    )
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


def _account_for_deletion_recovery(body: CancelAccountDeletionRequest, db: Session) -> User:
    if body.password is not None:
        user = find_user_by_email(db, body.email)
        if not user or not verify_password(body.password, user.password_hash):
            raise HTTPException(401, "이메일 또는 인증 정보가 올바르지 않습니다")
    else:
        from services.google_auth import verify_google_id_token

        google_user = verify_google_id_token(body.google_id_token)
        if google_user["email"] != body.email.lower():
            raise HTTPException(401, "이메일 또는 인증 정보가 올바르지 않습니다")
        social_account = (
            db.query(SocialAccount)
            .filter_by(provider="google", subject=google_user["sub"])
            .first()
        )
        user = db.get(User, social_account.user_id) if social_account else None
        if not user:
            raise HTTPException(401, "이메일 또는 인증 정보가 올바르지 않습니다")

    if user.role != UserRole.PARENT:
        raise HTTPException(403, "학부모 계정만 복구할 수 있습니다")
    return user


# ---------- 가입 / 로그인 (누구나) ----------
@router.post("/auth/signup", status_code=201)
def signup(body: SignupRequest, db: Session = Depends(get_db)):
    """학부모 회원가입 (앱): email, password, name, children[] -> access_token, role, user, child_requests
    - 비밀번호: 4자 이상 + 영문 대문자 + 특수문자 포함
    - children 에 자녀 이름·반·관계를 적어 보내면 '승인 대기' 요청이 만들어지고, 관리자가 확인해서 학생에 연결한다
    - 가입 즉시 로그인된 상태의 토큰을 돌려준다. role 은 항상 PARENT (관리자 계정은 가입으로 만들 수 없다)"""
    u = create_user_row(
        db, body.email, body.password, body.name, UserRole.PARENT, body.children,
        signup_method="APP",
    )
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


@router.put("/me/password")
def change_password(
    body: ChangePasswordRequest,
    me: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """현재 비밀번호를 검증한 뒤 로그인 계정의 비밀번호를 변경한다."""
    if me.signup_method == "GOOGLE":
        raise HTTPException(403, "소셜 로그인 계정은 비밀번호를 변경할 수 없습니다")
    if not verify_password(body.current_password, me.password_hash):
        raise HTTPException(401, "현재 비밀번호가 올바르지 않습니다")
    if body.current_password == body.new_password:
        raise HTTPException(400, "새 비밀번호는 현재 비밀번호와 달라야 합니다")

    me.password_hash = hash_password(body.new_password)
    db.commit()
    return {"status": "password_changed"}


@router.post("/me/account-deletion", status_code=202)
def request_account_deletion(
    body: AccountDeletionRequest,
    me: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """학부모 계정 삭제를 예약하고 30일 동안 복구 기회를 둔다"""
    if me.role != UserRole.PARENT:
        raise HTTPException(403, "학부모 계정만 삭제를 예약할 수 있습니다")
    if me.deletion_requested_at is not None:
        raise HTTPException(409, "이미 계정 삭제가 예약되어 있습니다")
    if body.current_password is not None:
        if not verify_password(body.current_password, me.password_hash):
            raise HTTPException(401, "현재 비밀번호가 올바르지 않습니다")
    else:
        from services.google_auth import verify_google_id_token

        google_user = verify_google_id_token(body.google_id_token)
        if google_user["email"] != me.email.lower():
            raise HTTPException(401, "Google 인증 정보가 올바르지 않습니다")
        social_account = (
            db.query(SocialAccount)
            .filter_by(provider="google", subject=google_user["sub"], user_id=me.id)
            .first()
        )
        if not social_account:
            raise HTTPException(401, "이 계정에 연결된 Google 인증 정보가 없습니다")

    now = datetime.now()
    me.deletion_requested_at = now
    db.commit()
    return {
        "status": "PENDING_DELETION",
        "requested_at": now,
        "scheduled_deletion_at": now + timedelta(days=ACCOUNT_DELETION_GRACE_DAYS),
        "grace_period_days": ACCOUNT_DELETION_GRACE_DAYS,
    }


@router.post("/auth/account-deletion/status")
def account_deletion_status(
    body: CancelAccountDeletionRequest,
    db: Session = Depends(get_db),
):
    """재인증 후 본인 계정의 삭제 유예 상태를 확인한다"""
    user = _account_for_deletion_recovery(body, db)
    requested_at = user.deletion_requested_at
    scheduled_at = (
        requested_at + timedelta(days=ACCOUNT_DELETION_GRACE_DAYS)
        if requested_at else None
    )
    days_remaining = (
        max(0, ceil((scheduled_at - datetime.now()).total_seconds() / 86400))
        if scheduled_at else 0
    )
    return {
        "status": (
            "DELETION_DUE" if scheduled_at and scheduled_at <= datetime.now()
            else "PENDING_DELETION" if requested_at
            else "ACTIVE"
        ),
        "requested_at": requested_at,
        "scheduled_deletion_at": scheduled_at,
        "grace_period_days": ACCOUNT_DELETION_GRACE_DAYS,
        "days_remaining": days_remaining,
    }


@router.post("/auth/account-deletion/cancel")
def cancel_account_deletion(
    body: CancelAccountDeletionRequest,
    db: Session = Depends(get_db),
):
    """비밀번호 또는 연결된 구글 계정으로 삭제 예약을 복구한다"""
    verified_user = _account_for_deletion_recovery(body, db)
    user = (
        db.query(User)
        .filter(User.id == verified_user.id)
        .with_for_update()
        .one_or_none()
    )
    if user is None:
        raise HTTPException(401, "이메일 또는 인증 정보가 올바르지 않습니다")
    if user.deletion_requested_at is None:
        raise HTTPException(409, "삭제 예약된 계정이 아닙니다")
    if user.deletion_requested_at + timedelta(days=ACCOUNT_DELETION_GRACE_DAYS) <= datetime.now():
        raise HTTPException(410, "계정 복구 가능 기간이 만료되었습니다")

    user.deletion_requested_at = None
    db.commit()
    db.refresh(user)
    return {
        **auth_response(user),
        "deletion_cancelled": True,
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
    token = body.push_token
    user_id = me.id
    row = db.query(PushToken).filter(PushToken.token == token).first()
    if row:
        row.user_id = user_id
        row.updated_at = datetime.now()
    else:
        db.add(PushToken(user_id=user_id, token=token))

    db.query(User).filter(User.push_token == token, User.id != user_id).update(
        {"push_token": None}, synchronize_session=False
    )
    me.push_token = token
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        row = db.query(PushToken).filter(PushToken.token == token).first()
        if row is None:
            raise
        row.user_id = user_id
        db.query(User).filter(User.push_token == token, User.id != user_id).update(
            {"push_token": None}, synchronize_session=False
        )
        db.query(User).filter(User.id == user_id).update(
            {"push_token": token}, synchronize_session=False
        )
        db.commit()


@router.delete("/me/push-token", status_code=204)
def delete_push_token(
    body: PushTokenRequest | None = None,
    me: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """로그아웃 시 푸시 토큰 해제.
    - push_token 을 보내면 이 기기의 토큰만 해제 (다른 기기 알림은 유지)
    - 본문 없이 보내면 이 계정의 모든 기기 토큰을 해제 (예전 앱 버전 호환)"""
    q = db.query(PushToken).filter(PushToken.user_id == me.id)
    if body is not None:
        q = q.filter(PushToken.token == body.push_token)
        if me.push_token == body.push_token:
            me.push_token = None
    else:
        me.push_token = None
    q.delete(synchronize_session=False)
    db.commit()


# ---------- 관리자 전용 ----------
@router.post("/web/users", status_code=201, dependencies=[Depends(require_admin)])
def create_user(body: UserCreate, db: Session = Depends(get_db)):
    """email, password, name, role -> user"""
    return user_to_dict(
        create_user_row(
            db, body.email, body.password, body.name, body.role,
            signup_method="ADMIN",
        )
    )


@router.get("/web/users", dependencies=[Depends(require_admin)])
def list_users(role: UserRole | None = None, db: Session = Depends(get_db)):
    q = db.query(User)
    if role:
        q = q.filter(User.role == role)
    return [user_to_dict(u) for u in q.all()]