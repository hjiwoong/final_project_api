"""보관 기간이 지난 이벤트 사진 삭제 (개인정보 처리 방안: 아동 사진 14일 보관 후 삭제)

사진은 두 곳에 저장된다.
  - 파일   : uploads/trip_{id}/boarding_xxx.jpg  (boarding_events.image_path)
  - DB     : event_images.image_data (MEDIUMBLOB)
기간이 지나면 둘 다 지우고 image_path 를 비운다.
인원 수·검출 결과(detections)·판정·경고 기록은 운행 기록이므로 남긴다.

- 기간: .env IMAGE_RETENTION_DAYS (기본 14일, services/storage.py 와 같은 값)
- 대상: image_expires_at 이 지난 이벤트
        + image_expires_at 이 비어 있는 예전 이벤트는 uploaded_at 기준으로 기간이 지났으면 삭제
- 호출: main.py 스케줄러가 서버 시작 직후 1번, 이후 1시간마다
"""
import logging
import os
from datetime import datetime, timedelta

from sqlalchemy import and_, or_

from database.db_connection import SessionLocal
from models.models import BoardingEvent, EventImage
from services.storage import RETENTION_DAYS, UPLOAD_DIR

logger = logging.getLogger(__name__)

BATCH_SIZE = 200   # 한 번에 너무 많은 행을 잠그지 않도록 나눠서 처리


def _remove_file(path: str) -> bool:
    """파일 삭제. 이미 없으면 False (종점·불일치 이벤트가 같은 파일을 가리킬 수 있음)"""
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        logger.warning("사진 파일 삭제 실패: %s", path, exc_info=True)
        return False


def _remove_empty_dir(path: str) -> None:
    """uploads/ 안의 빈 trip_{id} 폴더만 지운다"""
    folder = os.path.dirname(path)
    upload_root = os.path.abspath(UPLOAD_DIR)
    if not folder or os.path.abspath(folder) == upload_root:
        return
    if not os.path.abspath(folder).startswith(upload_root + os.sep):
        return
    try:
        os.rmdir(folder)        # 비어 있을 때만 지워진다
    except OSError:
        pass


def purge_expired_images(now: datetime | None = None) -> dict:
    """보관 기간이 지난 사진 파일과 DB 사진을 지운다.
    -> {"events": 처리한 이벤트 수, "files": 지운 파일 수, "db_images": 지운 DB 사진 수}"""
    now = now or datetime.now()
    legacy_cutoff = now - timedelta(days=RETENTION_DAYS)
    expired = or_(
        BoardingEvent.image_expires_at <= now,
        and_(BoardingEvent.image_expires_at.is_(None), BoardingEvent.uploaded_at <= legacy_cutoff),
    )
    total = {"events": 0, "files": 0, "db_images": 0}

    with SessionLocal() as db:
        last_id = 0
        while True:
            events = (
                db.query(BoardingEvent)
                .filter(expired, BoardingEvent.id > last_id)
                .order_by(BoardingEvent.id)
                .limit(BATCH_SIZE)
                .all()
            )
            if not events:
                break
            last_id = events[-1].id
            ids = [e.id for e in events]

            with_db_image = {
                event_id
                for (event_id,) in db.query(EventImage.event_id).filter(EventImage.event_id.in_(ids))
            }
            if with_db_image:
                db.query(EventImage).filter(EventImage.event_id.in_(with_db_image)).delete(
                    synchronize_session=False
                )

            paths = set()
            cleaned = set(with_db_image)
            for e in events:
                if e.image_path:
                    paths.add(e.image_path)
                    e.image_path = None
                    cleaned.add(e.id)

            if not cleaned:
                continue            # 이미 정리된 이벤트

            db.commit()             # DB 를 먼저 확정한 뒤 파일 삭제

            for path in paths:
                if _remove_file(path):
                    total["files"] += 1
                _remove_empty_dir(path)

            total["events"] += len(cleaned)
            total["db_images"] += len(with_db_image)

    if total["files"] or total["db_images"]:
        logger.info(
            "보관 기간(%s일)이 지난 사진 삭제: 이벤트 %s건, 파일 %s개, DB 사진 %s개",
            RETENTION_DAYS, total["events"], total["files"], total["db_images"],
        )
    return total


def run_purge_job() -> None:
    """스케줄러용: 실패해도 서버를 멈추지 않는다"""
    try:
        purge_expired_images()
    except Exception:  # noqa: BLE001
        logger.exception("사진 보관 기간 정리 작업 실패")
