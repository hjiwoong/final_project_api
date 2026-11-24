from models.models import AttendanceFile


UPLOAD_DIR = "uploads/attendance"
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
