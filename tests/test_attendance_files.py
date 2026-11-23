import os
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.security import require_admin, require_parent_terms_agreed
from database.db_connection import get_db
from database.orm import Base
from models.models import (
    Attendance,
    AttendanceFile,
    AttendanceStatus,
    Guardian,
    Student,
    User,
    UserRole,
)
from routers import attendance as attendance_router
from routers import attendance_file as attendance_file_router
from routers import student as student_router
from services import attendance_deadline


class AttendanceFileTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        tables = [
            table
            for table in Base.metadata.sorted_tables
            if table.name != "event_images"
        ]
        Base.metadata.create_all(self.engine, tables=tables)
        self.db = sessionmaker(bind=self.engine)()
        self.parent = User(
            email="parent@example.com",
            password_hash="hash",
            name="Parent",
            role=UserRole.PARENT,
        )
        self.other_parent = User(
            email="other@example.com",
            password_hash="hash",
            name="Other",
            role=UserRole.PARENT,
        )
        self.admin = User(
            email="admin@example.com",
            password_hash="hash",
            name="Admin",
            role=UserRole.ADMIN,
        )
        self.student = Student(name="학생")
        self.other_student = Student(name="다른 학생")
        self.db.add_all(
            [self.parent, self.other_parent, self.admin, self.student, self.other_student]
        )
        self.db.flush()
        self.db.add_all(
            [
                Guardian(user_id=self.parent.id, student_id=self.student.id),
                Guardian(
                    user_id=self.other_parent.id,
                    student_id=self.other_student.id,
                ),
            ]
        )
        self.attendance_date = date.today() + timedelta(days=1)
        self.attendance = Attendance(
            student_id=self.student.id,
            date=self.attendance_date,
            is_absent=True,
            status=AttendanceStatus.REQUESTED,
        )
        self.db.add(self.attendance)
        self.db.commit()

        self.temp_dir = tempfile.TemporaryDirectory()
        self.upload_dir_patch = patch.object(
            attendance_file_router, "UPLOAD_DIR", self.temp_dir.name
        )
        self.upload_dir_patch.start()
        self.clock_patch = patch.object(
            attendance_deadline,
            "now_kst_naive",
            return_value=datetime.combine(date.today(), time(8)),
        )
        self.clock_patch.start()

        app = FastAPI()
        app.include_router(student_router.router)
        app.include_router(attendance_router.router)
        app.include_router(attendance_file_router.router)
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[require_parent_terms_agreed] = lambda: self.parent
        app.dependency_overrides[require_admin] = lambda: self.admin
        self.app = app
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.clock_patch.stop()
        self.upload_dir_patch.stop()
        self.temp_dir.cleanup()
        self.db.close()
        self.engine.dispose()

    def upload(self, content=b"\xff\xd8\xffimage", filename="photo.jpg"):
        return self.client.post(
            f"/me/attendances/{self.attendance.id}/files",
            files={"file": (filename, content, "application/octet-stream")},
        )

    def test_upload_list_download_delete_and_attendance_views(self):
        response = self.upload(filename=r"C:\fakepath\증빙.jpg")
        self.assertEqual(response.status_code, 201)
        uploaded = response.json()
        self.assertEqual(uploaded["original_name"], "증빙.jpg")
        self.assertEqual(uploaded["content_type"], "image/jpeg")
        self.assertEqual(uploaded["size"], len(b"\xff\xd8\xffimage"))
        self.assertNotIn("stored_name", uploaded)
        self.assertNotIn("path", uploaded)

        listing = self.client.get(
            f"/me/attendances/{self.attendance.id}/files"
        )
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json(), [uploaded])

        download = self.client.get(
            f"/web/attendances/{self.attendance.id}/files/{uploaded['id']}"
        )
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content, b"\xff\xd8\xffimage")
        self.assertEqual(
            download.headers["content-disposition"],
            "attachment; filename*=UTF-8''%EC%A6%9D%EB%B9%99.jpg",
        )
        self.assertEqual(download.headers["x-content-type-options"], "nosniff")

        details = self.client.get(
            f"/me/children/{self.student.id}/attendance",
            params={"date": self.attendance_date.isoformat()},
        )
        self.assertEqual(details.json()["files"], [uploaded])
        web_list = self.client.get(
            "/web/attendances", params={"date": self.attendance_date.isoformat()}
        )
        self.assertEqual(web_list.json()["items"][0]["files"], [uploaded])
        app_list = self.client.get(
            f"/me/children/{self.student.id}/attendances",
            params={
                "from": self.attendance_date.isoformat(),
                "to": self.attendance_date.isoformat(),
            },
        )
        self.assertEqual(app_list.json()[0]["files"], [uploaded])
        web_files = self.client.get(
            f"/web/attendances/{self.attendance.id}/files"
        )
        self.assertEqual(web_files.json(), [uploaded])

        response = self.client.delete(
            f"/me/attendances/{self.attendance.id}/files/{uploaded['id']}"
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(os.listdir(self.temp_dir.name), [])
        self.assertEqual(self.db.query(AttendanceFile).count(), 0)

    def test_rejects_unsupported_and_oversized_content_and_caps_file_count(self):
        response = self.upload(content=b"not an image")
        self.assertEqual(response.status_code, 415)
        self.assertEqual(response.json()["detail"], "JPG, PNG, PDF 파일만 가능합니다")
        self.assertEqual(os.listdir(self.temp_dir.name), [])

        response = self.upload(content=b"\xff\xd8\xff" + b"x" * (5 * 1024 * 1024))
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["detail"], "파일은 5MB 이하만 가능합니다")
        self.assertEqual(os.listdir(self.temp_dir.name), [])

        for index in range(3):
            response = self.upload(filename=f"{index}.jpg")
            self.assertEqual(response.status_code, 201)
        response = self.upload()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"], "첨부파일은 최대 3개까지 가능합니다"
        )
        self.assertEqual(len(os.listdir(self.temp_dir.name)), 3)

    def test_hides_other_users_attendance_and_checks_status(self):
        response = self.client.get(
            f"/me/attendances/{self.attendance.id}/files"
        )
        self.assertEqual(response.status_code, 200)

        self.app.dependency_overrides[require_parent_terms_agreed] = (
            lambda: self.other_parent
        )
        response = self.client.get(
            f"/me/attendances/{self.attendance.id}/files"
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "결석 신청을 찾을 수 없습니다")

        self.app.dependency_overrides[require_parent_terms_agreed] = lambda: self.parent
        self.attendance.status = AttendanceStatus.CONFIRMED
        self.db.commit()
        response = self.upload()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"], "마감되었거나 수정할 수 없는 상태입니다"
        )

    def test_detects_png_and_pdf_from_content(self):
        samples = [
            (b"\x89PNG\r\n\x1a\nimage", "misnamed.pdf", "image/png", ".png"),
            (b"%PDF-document", "misnamed.png", "application/pdf", ".pdf"),
        ]
        for content, filename, content_type, extension in samples:
            response = self.upload(content=content, filename=filename)
            self.assertEqual(response.status_code, 201)
            uploaded = self.db.query(AttendanceFile).filter_by(
                id=response.json()["id"]
            ).one()
            self.assertEqual(uploaded.content_type, content_type)
            self.assertTrue(uploaded.stored_name.endswith(extension))

    def test_removes_disk_file_when_database_commit_fails(self):
        with patch.object(self.db, "commit", side_effect=RuntimeError("db failure")):
            with self.assertRaisesRegex(RuntimeError, "db failure"):
                self.upload()
        self.assertEqual(self.db.query(AttendanceFile).count(), 0)
        self.assertEqual(os.listdir(self.temp_dir.name), [])


if __name__ == "__main__":
    unittest.main()
