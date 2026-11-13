import os
from datetime import datetime

from fastapi import HTTPException, Response
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from models.models import BoardingEvent, EventImage


def event_image_response(event: BoardingEvent, db: Session):
    if event.image_expires_at and datetime.now() >= event.image_expires_at:
        raise HTTPException(410, "사진 보관 기간이 만료되었습니다.")

    saved_image = db.get(EventImage, event.id)
    if saved_image:
        return Response(
            content=saved_image.image_data,
            media_type=saved_image.content_type,
        )

    if event.image_path and os.path.exists(event.image_path):
        return FileResponse(event.image_path)

    raise HTTPException(410, "사진 파일을 찾을 수 없습니다.")
