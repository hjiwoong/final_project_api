"""JWT 발급 · 검증 · 권한(role) 검사"""
import os
from datetime import datetime, timedelta, timezone

import jwt
from dotenv import load_dotenv
from fastapi import Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from database.db_connection import SessionLocal, get_db
from models.models import User, UserRole

load_dotenv()

JWT_SECRET = os.getenv("JWT_SECRET", "change-me")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "720"))

bearer_scheme = HTTPBearer(auto_error=False)   # Swagger(/docs)에 Authorize 버튼 생성


def create_access_token(user: User) -> str:
    payload = {
        "sub": str(user.id),
        "role": user.role.value,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _user_id_from_token(token: str) -> int:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return int(payload["sub"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "토큰이 만료되었습니다")
    except (jwt.InvalidTokenError, KeyError, ValueError):
        raise HTTPException(401, "유효하지 않은 토큰입니다")


def get_current_user(
    cred: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """토큰 검증 → 현재 로그인한 User 반환 (실패 시 401)"""
    if cred is None:
        raise HTTPException(401, "로그인이 필요합니다")
    user = db.get(User, _user_id_from_token(cred.credentials))
    if user is None:
        raise HTTPException(401, "존재하지 않는 사용자입니다")
    if user.deletion_requested_at is not None:
        raise HTTPException(403, "계정 삭제 유예 중입니다. 계정 복구 API를 이용하세요")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != UserRole.ADMIN:
        raise HTTPException(403, "관리자 권한이 필요합니다")
    return user


def require_parent(user: User = Depends(get_current_user)) -> User:
    if user.role != UserRole.PARENT:
        raise HTTPException(403, "학부모 권한이 필요합니다")
    return user


# ---------- 헤더를 못 붙이는 경우용 (SSE EventSource, <img src>) ----------
def require_admin_header_or_query(
    cred: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    token: str | None = Query(None, description="Authorization 헤더를 못 쓰는 경우 JWT"),
) -> User:
    """Authorization 헤더 또는 ?token= 으로 관리자 인증.
    SSE 처럼 오래 열려 있는 연결이 DB 세션을 붙잡지 않도록 자체 세션을 쓰고 바로 닫는다."""
    raw = cred.credentials if cred else token
    if not raw:
        raise HTTPException(401, "로그인이 필요합니다")
    user_id = _user_id_from_token(raw)
    with SessionLocal() as db:
        user = db.get(User, user_id)
        if user is None:
            raise HTTPException(401, "존재하지 않는 사용자입니다")
        if user.role != UserRole.ADMIN:
            raise HTTPException(403, "관리자 권한이 필요합니다")
        db.expunge(user)
    return user
