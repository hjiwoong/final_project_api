import unittest
from datetime import date, datetime
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.security import (
    get_current_user,
    require_admin,
    require_parent_terms_agreed,
)
from database.db_connection import get_db
from database.orm import Base
from models.models import (
    Attendance,
    AttendanceStatus,
    Bus,
    BusStudent,
    Guardian,
    Notification,
    Student,
    Trip,
    TripSchedule,
    User,
    UserRole,
)
from routers import attendance as attendance_router
from routers import notification, student as student_router
from services import attendance_deadline


class AttendanceWorkflowTests(unittest.TestCase):
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

        self.admin = User(
            email="admin@example.com",
            password_hash="hash",
            name="Admin",
            role=UserRole.ADMIN,
        )
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
        self.db.add_all([self.admin, self.parent, self.other_parent])
        self.db.flush()

        self.student = Student(name="강동원")
        self.other_student = Student(name="김학생")
        self.db.add_all([self.student, self.other_student])
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
        self.bus = Bus(plate_no="12가3456", name="1호차", driver_id=self.admin.id)
        self.db.add(self.bus)
        self.db.flush()
        self.db.add(BusStudent(bus_id=self.bus.id, student_id=self.student.id))
        self.target_date = date(2026, 11, 24)
        self.schedule = TripSchedule(
            bus_id=self.bus.id,
            service_date=self.target_date,
            scheduled_at=datetime(2026, 11, 24, 9, 0),
        )
        self.db.add(self.schedule)
        self.db.commit()

        app = FastAPI()
        app.include_router(student_router.router)
        app.include_router(attendance_router.router)
        app.include_router(notification.router)
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[require_parent_terms_agreed] = lambda: self.parent
        app.dependency_overrides[require_admin] = lambda: self.admin
        app.dependency_overrides[notification.require_terms_agreed] = lambda: self.parent
        self.app = app
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.db.close()
        self.engine.dispose()

    def test_deadline_editability_and_started_trip(self):
        with patch.object(
            attendance_deadline,
            "now_kst_naive",
            return_value=datetime(2026, 11, 24, 8, 29),
        ), patch.object(student_router.notifier, "send_trip_pushes"):
            response = self.client.put(
                f"/me/children/{self.student.id}/attendance",
                json={"date": "2026-11-24", "is_absent": True},
            )
        self.assertEqual(response.status_code, 200)

        with patch.object(
            attendance_deadline,
            "now_kst_naive",
            return_value=datetime(2026, 11, 24, 8, 31),
        ):
            response = self.client.put(
                f"/me/children/{self.student.id}/attendance",
                json={"date": "2026-11-24", "is_absent": False},
            )
        self.assertEqual(response.status_code, 409)

        trip = Trip(
            bus_id=self.bus.id,
            driver_id=self.admin.id,
            started_at=datetime(2026, 11, 24, 8, 20),
        )
        self.db.add(trip)
        self.db.flush()
        self.schedule.trip_id = trip.id
        self.db.commit()
        with patch.object(
            attendance_deadline,
            "now_kst_naive",
            return_value=datetime(2026, 11, 24, 8, 0),
        ):
            response = self.client.put(
                f"/me/children/{self.student.id}/attendance",
                json={"date": "2026-11-24", "is_absent": False},
            )
        self.assertEqual(response.status_code, 409)

    def test_parent_cannot_read_another_guardians_attendance(self):
        response = self.client.get(
            f"/me/children/{self.other_student.id}/attendance",
            params={"date": "2026-11-25"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "내 자녀가 아닙니다")

    def test_range_request_creates_one_admin_notification(self):
        with patch.object(student_router.notifier, "send_trip_pushes"):
            response = self.client.put(
                f"/me/children/{self.student.id}/attendances/range",
                json={
                    "from": "2026-11-27",
                    "to": "2026-11-28",
                    "is_absent": True,
                    "reason_type": "FAMILY",
                    "reason_memo": "범위 사유",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.db.query(Attendance)
            .filter(
                Attendance.student_id == self.student.id,
                Attendance.date.in_([date(2026, 11, 27), date(2026, 11, 28)]),
            )
            .count(),
            2,
        )
        notices = self.db.query(Notification).filter_by(
            event_type="ABSENCE_REQUESTED"
        ).all()
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].body, "강○○ · 11월 27일~11월 28일")

    def test_parent_attendance_views_include_status_and_deadline(self):
        row = Attendance(
            student_id=self.student.id,
            date=self.target_date,
            is_absent=True,
            status=AttendanceStatus.REQUESTED,
        )
        self.db.add(row)
        self.db.commit()
        with patch.object(
            attendance_deadline,
            "now_kst_naive",
            return_value=datetime(2026, 11, 24, 8, 0),
        ):
            single = self.client.get(
                f"/me/children/{self.student.id}/attendance",
                params={"date": self.target_date.isoformat()},
            )
            ranged = self.client.get(
                f"/me/children/{self.student.id}/attendances",
                params={"from": self.target_date.isoformat(), "to": self.target_date.isoformat()},
            )
            children = self.client.get(
                "/me/children", params={"date": self.target_date.isoformat()}
            )
        self.assertEqual(single.status_code, 200)
        self.assertEqual(single.json()["deadline_at"], "2026-11-24T08:30:00+09:00")
        self.assertTrue(single.json()["editable"])
        self.assertEqual(single.json()["status"], "REQUESTED")
        self.assertEqual(ranged.json()[0]["confirmed_at"], None)
        self.assertEqual(children.json()[0]["attendance"]["status"], "REQUESTED")


if __name__ == "__main__":
    unittest.main()
