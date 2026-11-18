"""구글 로그인 / 가입 (앱 전용)"""
import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from database.db_connection import get_db
from core.terms import AgreementInput
from models.models import ChildRequest, SocialAccount, User, UserRole
from routers.user import auth_response, create_user_row, find_user_by_email
from services.child_requests import (
    MAX_CHILDREN_AT_SIGNUP, ChildInput, parent_request_dict,
)
from services.google_auth import verify_google_id_token

router = APIRouter(tags=["구글 로그인"])


class GoogleLoginRequest(BaseModel):
    id_token: str = Field(min_length=10)
    # 아래는 '처음 가입하는 경우'에만 쓰인다. 이미 가입된 계정이면 무시된다 (자녀는 /me/child-requests 로 추가)
    name: str | None = Field(default=None, max_length=100)
    children: list[ChildInput] = Field(default_factory=list, max_length=MAX_CHILDREN_AT_SIGNUP)
    agreements: list[AgreementInput] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _blank_to_none(cls, v):
        v = (v or "").strip()
        return v or None


@router.post("/auth/google")
def google_login(body: GoogleLoginRequest, db: Session = Depends(get_db)):
    """구글 로그인 겸 가입 (앱): id_token [, name, children[]] -> access_token, role, user, is_new, linked, child_requests
    - 이미 이 구글 계정으로 가입했으면 로그인
    - 같은 이메일의 기존 계정(비밀번호로 가입)이 있으면 그 계정에 구글을 연결하고 로그인 (linked=true)
    - 처음이면 학부모(PARENT)로 새로 가입 (is_new=true). children 을 같이 보내면 가입과 동시에 승인 대기 요청이 만들어진다
    - 관리자 웹(/web/login)은 구글로 로그인할 수 없다"""
    g = verify_google_id_token(body.id_token)

    link = db.query(SocialAccount).filter_by(provider="google", subject=g["sub"]).first()
    is_new = linked = False
    if link:
        user = db.get(User, link.user_id)
    else:
        user = find_user_by_email(db, g["email"])
        try:
            if user:                                    # 기존 계정에 구글 연결 (이메일은 구글이 인증한 값)
                linked = True
            else:                                       # 신규 가입
                is_new = True
                user = create_user_row(
                    db, g["email"], secrets.token_urlsafe(32),
                    body.name or g["name"] or g["email"].split("@")[0],
                    UserRole.PARENT, body.children, signup_method="GOOGLE",
                    agreements=body.agreements, commit=False,
                )
            db.add(SocialAccount(user_id=user.id, provider="google", subject=g["sub"], email=g["email"]))
            db.commit()
        except IntegrityError:                          # 같은 요청이 동시에 두 번 들어온 경우
            db.rollback()
            if (
                db.query(SocialAccount).filter_by(provider="google", subject=g["sub"]).first()
                or (is_new and find_user_by_email(db, g["email"]))
            ):
                raise HTTPException(409, "처리 중 충돌이 발생했습니다. 다시 시도하세요")
            raise
        except SQLAlchemyError:
            db.rollback()
            raise
        db.refresh(user)

    if user.deletion_requested_at is not None:

        raise HTTPException(403, "계정 삭제 유예 중입니다")


    reqs = db.query(ChildRequest).filter(ChildRequest.user_id == user.id).order_by(ChildRequest.id).all()
    return {
        **auth_response(user, db),
        "is_new": is_new,
        "linked": linked,
        "child_requests": [parent_request_dict(r) for r in reqs],
    }
