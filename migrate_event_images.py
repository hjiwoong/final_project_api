import os
from datetime import datetime

from database.db_connection import SessionLocal
from models.models import BoardingEvent, EventImage

db = SessionLocal()

migrated = 0
skipped = 0
expired = 0

try:
    events = db.query(BoardingEvent).filter(
        BoardingEvent.image_path.isnot(None)
    ).all()

    for event in events:
        # 이미 MySQL에 저장된 사진은 건너뛰기
        if db.get(EventImage, event.id):
            skipped += 1
            continue

        # 보관 기간이 지난 사진은 이전하지 않기
        if event.image_expires_at and event.image_expires_at <= datetime.now():
            expired += 1
            continue

        # 로컬 사진 파일이 없으면 건너뛰기
        if not os.path.isfile(event.image_path):
            skipped += 1
            continue

        # 로컬 파일 크기 확인
        if os.path.getsize(event.image_path) > 5 * 1024 * 1024:
            skipped += 1
            continue

        with open(event.image_path, "rb") as f:
            data = f.read()

        if not data:
            skipped += 1
            continue

        ext = os.path.splitext(event.image_path)[1].lower()
        content_type = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }.get(ext, "application/octet-stream")

        db.add(EventImage(
            event_id=event.id,
            image_data=data,
            content_type=content_type,
        ))

        migrated += 1

    db.commit()

    print("Migration complete")
    print("Migrated:", migrated)
    print("Skipped:", skipped)
    print("Expired:", expired)

except Exception:
    db.rollback()
    raise

finally:
    db.close()