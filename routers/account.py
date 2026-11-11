from datetime import datetime, timedelta
from math import ceil

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, model_validator
from sqlalchemy.orm import Session

from core.security import get_current_user
from database.db_connection import get_db
from models.models import SocialAccount, User, UserRole
from routers.user import auth_response, find_user_by_email, verify_password
from services.google_auth import verify_google_id_token

router = APIRouter(tags=["계정"])

GRACE_PERIOD_DAYS = 30


class DeletionRequest(BaseModel):
    current_password: str | None = None
    google_id_token: str | None = None

    @model_validator(mode="after")
    def exactly_one_credential(self):
        if (self.current_password is None) == (self.google_id_token is None):
            raise ValueError("비밀번호 또는 Google 인증 정보 중 하나를 입력하세요")
        return self


class DeletionAuthRequest(BaseModel):
    email: EmailStr
    password: str | None = None
    google_id_token: str | None = None

    @model_validator(mode="after")
    def exactly_one_credential(self):
        if (self.password is None) == (self.google_id_token is None):
            raise ValueError("비밀번호 또는 Google 인증 정보 중 하나를 입력하세요")
        return self


def _authenticate_deletion(
    db: Session,
    email: str,
    current_password: str | None,
    google_id_token: str | None,
) -> User:
    user = find_user_by_email(db, email)
    if user is None:
        raise HTTPException(401, "인증 정보가 올바르지 않습니다")

    if current_password is not None:
        if not verify_password(current_password, user.password_hash):
            raise HTTPException(401, "인증 정보가 올바르지 않습니다")
    else:
        google_user = verify_google_id_token(google_id_token or "")
        linked_account = (
            db.query(SocialAccount.id)
            .filter_by(
                user_id=user.id,
                provider="google",
                subject=google_user["sub"],
            )
            .first()
        )
        if google_user["email"] != user.email.lower() or linked_account is None:
            raise HTTPException(401, "인증 정보가 올바르지 않습니다")
    return user


def _deletion_schedule(user: User) -> dict:
    requested_at = user.deletion_requested_at
    scheduled_at = requested_at + timedelta(days=GRACE_PERIOD_DAYS)
    remaining = max(0, ceil((scheduled_at - datetime.now()).total_seconds() / 86400))
    return {
        "status": "PENDING_DELETION",
        "requested_at": requested_at.isoformat(),
        "scheduled_deletion_at": scheduled_at.isoformat(),
        "grace_period_days": GRACE_PERIOD_DAYS,
        "days_remaining": remaining,
    }


@router.post("/me/account-deletion", status_code=202)
def request_account_deletion(
    body: DeletionRequest,
    me: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if me.role != UserRole.PARENT:
        raise HTTPException(403, "학부모 계정만 삭제를 요청할 수 있습니다")
    _authenticate_deletion(
        db, me.email, body.current_password, body.google_id_token
    )
    if me.deletion_requested_at is not None:
        raise HTTPException(409, "이미 계정 삭제가 예약되어 있습니다")
    me.deletion_requested_at = datetime.now()
    db.commit()
    db.refresh(me)
    return _deletion_schedule(me)


@router.post("/auth/account-deletion/status")
def get_account_deletion_status(
    body: DeletionAuthRequest,
    db: Session = Depends(get_db),
):
    user = _authenticate_deletion(
        db, str(body.email), body.password, body.google_id_token
    )
    if user.role != UserRole.PARENT:
        raise HTTPException(403, "학부모 계정이 아닙니다")
    if user.deletion_requested_at is None:
        raise HTTPException(409, "진행 중인 계정 삭제 예약이 없습니다")
    schedule = _deletion_schedule(user)
    if schedule["days_remaining"] == 0:
        raise HTTPException(410, "계정 삭제 유예 기간이 끝났습니다")
    return schedule


@router.post("/auth/account-deletion/cancel")
def cancel_account_deletion(
    body: DeletionAuthRequest,
    db: Session = Depends(get_db),
):
    user = _authenticate_deletion(
        db, str(body.email), body.password, body.google_id_token
    )
    if user.role != UserRole.PARENT:
        raise HTTPException(403, "학부모 계정만 삭제를 취소할 수 있습니다")
    if user.deletion_requested_at is None:
        raise HTTPException(409, "진행 중인 계정 삭제 예약이 없습니다")
    if (
        user.deletion_requested_at + timedelta(days=GRACE_PERIOD_DAYS)
        <= datetime.now()
    ):
        raise HTTPException(410, "계정 삭제 유예 기간이 끝났습니다")
    user.deletion_requested_at = None
    db.commit()
    db.refresh(user)
    return auth_response(user)
