from models.models import AttendanceFile


import os

from services.storage import UPLOAD_DIR as _BASE_UPLOAD_DIR

UPLOAD_DIR = os.path.join(_BASE_UPLOAD_DIR, "attendance")
# 결석 첨부파일도 차량 사진과 같은 기간(IMAGE_RETENTION_DAYS, 기본 14일)만 보관
ATTENDANCE_FILE_RETENTION_DAYS = int(
    os.getenv("ATTENDANCE_FILE_RETENTION_DAYS", os.getenv("IMAGE_RETENTION_DAYS", "14"))
)
MAX_FILE_SIZE = 5 * 1024 * 1024
MAX_FILES_PER_ATTENDANCE = 3


def attendance_file_to_dict(file: AttendanceFile) -> dict[str, object]:
    return {
        "id": file.id,
        "file_id": file.id,                  # 앱(ChildAttendanceFile·기사 앱)이 읽는 키
        "file_name": file.original_name,     # 앱이 읽는 키
        "original_name": file.original_name,
        "content_type": file.content_type,
        "size": file.size,
        "created_at": file.created_at,
    }
