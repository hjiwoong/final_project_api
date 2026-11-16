"""사진 저장 (uploads/ 폴더) 및 보관기간"""
import os
import uuid
from datetime import datetime, timedelta

UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")
RETENTION_DAYS = int(os.getenv("IMAGE_RETENTION_DAYS", "14"))


def save_image(trip_id: int, kind: str, filename: str | None, data: bytes) -> str:
    ext = os.path.splitext(filename or "")[1].lower() or ".jpg"
    folder = os.path.join(UPLOAD_DIR, f"trip_{trip_id}")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{kind}_{uuid.uuid4().hex}{ext}")
    with open(path, "wb") as f:
        f.write(data)
    return path


def expires_at() -> datetime:
    return datetime.now() + timedelta(days=RETENTION_DAYS)
