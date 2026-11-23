import logging
import os
import tempfile
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from core.security import require_admin, require_parent_terms_agreed
from database.db_connection import get_db
from models.models import (
    Attendance,
    AttendanceFile,
    AttendanceStatus,
    Guardian,
    Student,
    User,
)
from services.attendance_deadline import get_deadline
from services.attendance_file_storage import (
    MAX_FILE_SIZE,
    MAX_FILES_PER_ATTENDANCE,
    UPLOAD_DIR,
    attendance_file_to_dict,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["결석 첨부파일"])
CHUNK_SIZE = 64 * 1024
ALLOWED_FILE_TYPES = {
    "image/jpeg": (".jpg", b"\xff\xd8\xff"),
    "image/png": (".png", b"\x89PNG\r\n\x1a\n"),
    "application/pdf": (".pdf", b"%PDF-"),
}

os.makedirs(UPLOAD_DIR, exist_ok=True)


def _parent_attendance(
    attendance_id: int, user_id: int, db: Session, lock: bool = False
) -> Attendance:
    query = (
        db.query(Attendance)
        .join(Student, Attendance.student_id == Student.id)
        .join(Guardian, Guardian.student_id == Student.id)
        .filter(
            Attendance.id == attendance_id,
            Attendance.is_absent.is_(True),
            Guardian.user_id == user_id,
        )
    )
    if lock:
        query = query.with_for_update()
    attendance = query.first()
    if attendance is None:
        raise HTTPException(404, "결석 신청을 찾을 수 없습니다")
    return attendance


def _attendance_or_404(attendance_id: int, db: Session) -> Attendance:
    attendance = db.get(Attendance, attendance_id)
    if attendance is None:
        raise HTTPException(404, "결석 신청을 찾을 수 없습니다")
    return attendance


def _check_editable(attendance: Attendance, db: Session) -> None:
    if attendance.status != AttendanceStatus.REQUESTED:
        raise HTTPException(409, "마감되었거나 수정할 수 없는 상태입니다")
    _, editable = get_deadline(db, attendance.student_id, attendance.date)
    if not editable:
        raise HTTPException(409, "마감되었거나 수정할 수 없는 상태입니다")


def _file_or_404(attendance_id: int, file_id: int, db: Session) -> AttendanceFile:
    file = (
        db.query(AttendanceFile)
        .filter(
            AttendanceFile.id == file_id,
            AttendanceFile.attendance_id == attendance_id,
        )
        .first()
    )
    if file is None:
        raise HTTPException(404, "결석 신청을 찾을 수 없습니다")
    return file


def _file_path(file: AttendanceFile) -> str:
    path = os.path.join(UPLOAD_DIR, file.stored_name)
    if not os.path.isfile(path):
        raise HTTPException(404, "결석 신청을 찾을 수 없습니다")
    return path


def _download_response(file: AttendanceFile) -> FileResponse:
    return FileResponse(
        _file_path(file),
        media_type=file.content_type,
        headers={
            "Content-Disposition": (
                "attachment; filename*=UTF-8''"
                f"{quote(file.original_name, safe='')}"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


def _detect_file_type(prefix: bytes) -> tuple[str, str] | None:
    for content_type, (extension, signature) in ALLOWED_FILE_TYPES.items():
        if prefix.startswith(signature):
            return content_type, extension
    return None


async def _save_upload(upload: UploadFile) -> tuple[str, str, int, str]:
    descriptor, temporary_path = tempfile.mkstemp(
        prefix=".attendance-", dir=UPLOAD_DIR
    )
    try:
        with os.fdopen(descriptor, "wb") as temporary_file:
            first_chunk = await upload.read(CHUNK_SIZE)
            detected = _detect_file_type(first_chunk)
            if detected is None:
                raise HTTPException(415, "JPG, PNG, PDF 파일만 가능합니다")
            content_type, extension = detected
            size = len(first_chunk)
            if size > MAX_FILE_SIZE:
                raise HTTPException(413, "파일은 5MB 이하만 가능합니다")
            temporary_file.write(first_chunk)

            while True:
                chunk = await upload.read(CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_FILE_SIZE:
                    raise HTTPException(413, "파일은 5MB 이하만 가능합니다")
                temporary_file.write(chunk)

        stored_name = f"{uuid.uuid4().hex}{extension}"
        final_path = os.path.join(UPLOAD_DIR, stored_name)
        os.replace(temporary_path, final_path)
        return final_path, stored_name, size, content_type
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)


@router.post(
    "/me/attendances/{attendance_id}/files",
    status_code=201,
)
async def upload_attendance_file(
    attendance_id: int,
    file: UploadFile = File(...),
    me: User = Depends(require_parent_terms_agreed),
    db: Session = Depends(get_db),
):
    attendance = _parent_attendance(attendance_id, me.id, db, lock=True)
    _check_editable(attendance, db)
    file_count = (
        db.query(AttendanceFile.id)
        .filter(AttendanceFile.attendance_id == attendance_id)
        .count()
    )
    if file_count >= MAX_FILES_PER_ATTENDANCE:
        raise HTTPException(409, "첨부파일은 최대 3개까지 가능합니다")

    final_path, stored_name, size, content_type = await _save_upload(file)
    original_name = os.path.basename((file.filename or "").replace("\\", "/"))[:255]
    record = AttendanceFile(
        attendance_id=attendance_id,
        stored_name=stored_name,
        original_name=original_name,
        content_type=content_type,
        size=size,
        uploaded_by=me.id,
    )
    try:
        db.add(record)
        db.commit()
    except Exception:
        db.rollback()
        try:
            os.remove(final_path)
        except FileNotFoundError:
            pass
        except OSError:
            logger.exception("DB 저장 실패 후 결석 첨부파일을 삭제하지 못했습니다")
        raise
    return attendance_file_to_dict(record)


@router.get("/me/attendances/{attendance_id}/files")
def list_my_attendance_files(
    attendance_id: int,
    me: User = Depends(require_parent_terms_agreed),
    db: Session = Depends(get_db),
):
    _parent_attendance(attendance_id, me.id, db)
    files = (
        db.query(AttendanceFile)
        .filter(AttendanceFile.attendance_id == attendance_id)
        .order_by(AttendanceFile.created_at, AttendanceFile.id)
        .all()
    )
    return [attendance_file_to_dict(file) for file in files]


@router.get("/me/attendances/{attendance_id}/files/{file_id}")
def download_my_attendance_file(
    attendance_id: int,
    file_id: int,
    me: User = Depends(require_parent_terms_agreed),
    db: Session = Depends(get_db),
):
    _parent_attendance(attendance_id, me.id, db)
    return _download_response(_file_or_404(attendance_id, file_id, db))


@router.delete(
    "/me/attendances/{attendance_id}/files/{file_id}",
    status_code=204,
)
def delete_my_attendance_file(
    attendance_id: int,
    file_id: int,
    me: User = Depends(require_parent_terms_agreed),
    db: Session = Depends(get_db),
):
    attendance = _parent_attendance(attendance_id, me.id, db)
    file = _file_or_404(attendance_id, file_id, db)
    _check_editable(attendance, db)
    path = os.path.join(UPLOAD_DIR, file.stored_name)
    db.delete(file)
    db.commit()
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    return None


@router.get(
    "/web/attendances/{attendance_id}/files",
    dependencies=[Depends(require_admin)],
)
def list_attendance_files(
    attendance_id: int, db: Session = Depends(get_db)
):
    _attendance_or_404(attendance_id, db)
    files = (
        db.query(AttendanceFile)
        .filter(AttendanceFile.attendance_id == attendance_id)
        .order_by(AttendanceFile.created_at, AttendanceFile.id)
        .all()
    )
    return [attendance_file_to_dict(file) for file in files]


@router.get(
    "/web/attendances/{attendance_id}/files/{file_id}",
    dependencies=[Depends(require_admin)],
)
def download_attendance_file(
    attendance_id: int, file_id: int, db: Session = Depends(get_db)
):
    _attendance_or_404(attendance_id, db)
    return _download_response(_file_or_404(attendance_id, file_id, db))
