"""구글 ID 토큰 검증

흐름: 안드로이드 앱이 구글 로그인으로 'ID 토큰'을 받는다 -> 서버로 보낸다 -> 서버가 구글 공개키로 직접 검증한다.
앱이 보내는 이메일/이름 같은 값은 믿지 않고, 검증을 통과한 토큰 안의 값만 사용한다.

.env 에 GOOGLE_CLIENT_ID 를 적는다. 앱에서 setServerClientId(...) 에 넣는 '웹 애플리케이션' 유형 클라이언트 ID 와 같은 값.
여러 개면 쉼표로 구분 (예: 웹 ID, 테스트용 ID).
"""
import os

from dotenv import load_dotenv
from fastapi import HTTPException
from google.auth.exceptions import GoogleAuthError, TransportError
from google.auth.transport import requests as g_requests
from google.oauth2 import id_token

load_dotenv()

GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")


def google_client_ids() -> list[str]:
    return [c.strip() for c in os.getenv("GOOGLE_CLIENT_ID", "").split(",") if c.strip()]


def verify_google_id_token(token: str) -> dict:
    """검증을 통과하면 {sub, email, name} 반환. 실패하면 HTTPException"""
    client_ids = google_client_ids()
    if not client_ids:
        raise HTTPException(503, "구글 로그인이 설정되지 않았습니다 (서버 .env 의 GOOGLE_CLIENT_ID 확인)")
    try:
        # audience=None 으로 서명·만료·발급자만 확인하고, 대상(aud)은 아래에서 우리 클라이언트 ID 목록과 직접 비교한다
        info = id_token.verify_oauth2_token(token, g_requests.Request(), audience=None)
    except TransportError:
        raise HTTPException(503, "구글 인증 서버에 연결할 수 없습니다. 잠시 후 다시 시도하세요")
    except (GoogleAuthError, ValueError):
        raise HTTPException(401, "유효하지 않은 구글 토큰입니다")
    if info.get("iss") not in GOOGLE_ISSUERS or info.get("aud") not in client_ids:
        raise HTTPException(401, "유효하지 않은 구글 토큰입니다")
    email = (info.get("email") or "").strip().lower()
    if not info.get("sub") or not email:
        raise HTTPException(401, "구글 계정에서 이메일을 받아오지 못했습니다")
    if info.get("email_verified") is not True:
        raise HTTPException(401, "이메일 인증이 안 된 구글 계정입니다")
    return {"sub": str(info["sub"]), "email": email, "name": (info.get("name") or "").strip()}
