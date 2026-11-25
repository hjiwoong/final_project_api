"""사진 저장 (uploads/ 폴더) 및 보관기간"""
import os
import uuid
from datetime import datetime, timedelta

# 서버를 어느 폴더에서 실행하든 같은 곳에 저장되도록 프로젝트 폴더 기준 절대 경로로 맞춘다
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_upload_env = os.getenv("UPLOAD_DIR", "uploads")
UPLOAD_DIR = _upload_env if os.path.isabs(_upload_env) else os.path.join(BASE_DIR, _upload_env)
RETENTION_DAYS = int(os.getenv("IMAGE_RETENTION_DAYS", "14"))


def save_image(trip_id: int, kind: str, filename: str | None, data: bytes) -> str:
    ext = os.path.splitext(filename or "")[1].lower() or ".jpg"
    folder = os.path.join(UPLOAD_DIR, f"trip_{trip_id}")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{kind}_{uuid.uuid4().hex}{ext}")
    with open(path, "wb") as f:
        f.write(data)
    return path


def resolve_path(path: str | None) -> str | None:
    """DB 에 상대 경로(uploads/trip_1/...)로 저장된 예전 사진도 프로젝트 폴더 기준으로 찾는다"""
    if not path:
        return path
    return path if os.path.isabs(path) else os.path.join(BASE_DIR, path)


def expires_at() -> datetime:
    return datetime.now() + timedelta(days=RETENTION_DAYS)
