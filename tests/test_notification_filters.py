import unittest
from datetime import datetime
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.security import require_terms_agreed
from database.db_connection import get_db
from database.orm import Base
from models.models import (
    Alert, AlertLevel, BoardingEvent, Bus, BusStudent, EventType, Guardian,
    Notification, Student, Trip, User, UserRole,
)
from routers import notification
from services import notifier


class NotificationFilterTests(unittest.TestCase):
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
        self.session_factory = sessionmaker(bind=self.engine)
        self.db = self.session_factory()
        self.user = User(
            email="admin@example.com",
            password_hash="hash",
            name="Admin",
            role=UserRole.ADMIN,
        )
        self.db.add(self.user)
        self.db.flush()
        self.db.add_all(
            [
                Notification(
                    user_id=self.user.id,
                    category="CHECK",
                    event_type="NOT_STARTED",
                    title="버스 미출발",
                    body="1호차 예정 운행이 지났습니다.",
                ),
                Notification(
                    user_id=self.user.id,
                    category="CHECK",
                    event_type="NOT_STARTED",
                    title="버스 미출발",
                    body="2호차 예정 운행이 지났습니다.",
                    read_at=datetime.now(),
                ),
                Notification(
                    user_id=self.user.id,
                    category="URGENT",
                    event_type="MISSED",
                    title="안전 경고",
                    body="학생 승차 기록을 확인하세요.",
                ),
            ]
        )
        self.db.commit()
        app = FastAPI()
        app.include_router(notification.router)
        app.dependency_overrides[require_terms_agreed] = lambda: self.user
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.db.close()
        self.engine.dispose()

    def test_filters_notifications_by_category_and_event_type(self):
        response = self.client.get(
            "/me/notifications",
            params={"category": "CHECK", "event_type": "NOT_STARTED", "limit": 10},
        )
        self.assertEqual(response.status_code, 200)
        result = response.json()

        self.assertEqual(result["total"], 2)
        self.assertEqual(
            {item["event_type"] for item in result["items"]},
            {"NOT_STARTED"},
        )
        self.assertEqual(result["unread_count"], 2)

    def test_invalid_category_is_rejected(self):
        for category in ("OTHER", "", "check"):
            with self.subTest(category=category):
                response = self.client.get(
                    "/me/notifications", params={"category": category}
                )
                self.assertEqual(response.status_code, 422)

    def test_read_all_only_updates_current_users_unread_notifications(self):
        other = User(
            email="other@example.com", password_hash="hash",
            name="Other", role=UserRole.ADMIN,
        )
        self.db.add(other)
        self.db.flush()
        other_notification = Notification(
            user_id=other.id, category="CHECK", event_type="NEEDS_CHECK",
            title="사진 확인 필요",
        )
        self.db.add(other_notification)
        self.db.commit()
        filtered = self.client.get(
            "/me/notifications", params={"category": "CHECK", "unread_only": True}
        ).json()
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["unread_count"], 2)
        original_read = self.db.query(Notification).filter(
            Notification.user_id == self.user.id,
            Notification.read_at.is_not(None),
        ).one().read_at
        now = datetime(2026, 11, 19, 20, 20, 17, 123456)
        with patch.object(notification, "now_kst_naive", return_value=now):
            response = self.client.post("/me/notifications/read-all")
        self.assertEqual(response.json(), {"updated": 2})
        self.db.expire_all()
        rows = self.db.query(Notification).filter_by(user_id=self.user.id).all()
        self.assertEqual(sum(row.read_at == now for row in rows), 2)
        self.assertEqual(sum(row.read_at == original_read for row in rows), 1)
        self.assertIsNone(self.db.get(Notification, other_notification.id).read_at)
        self.assertEqual(self.client.post("/me/notifications/read-all").json(), {"updated": 0})

    def test_timestamps_order_and_bus_name(self):
        bus = Bus(plate_no="12가3456", name="1호차", driver_id=self.user.id)
        self.db.add(bus)
        self.db.flush()
        trip = Trip(bus_id=bus.id, driver_id=self.user.id)
        self.db.add(trip)
        self.db.flush()
        alert = Alert(trip_id=trip.id, level=AlertLevel.CHECK)
        self.db.add(alert)
        self.db.flush()
        timestamp = datetime(2030, 11, 19, 20, 20, 17, 123456)
        older_id = Notification(
            user_id=self.user.id, alert_id=alert.id, category="CHECK",
            event_type="NEEDS_CHECK", title="사진 확인 필요",
            sent_at=timestamp, read_at=timestamp,
        )
        newer_id = Notification(
            user_id=self.user.id, trip_id=trip.id, category="TRIP",
            event_type="STARTED", title="운행 시작", sent_at=timestamp,
        )
        latest_id_but_older_time = Notification(
            user_id=self.user.id, category="TRIP", event_type="ENDED",
            title="운행 종료", sent_at=datetime(2029, 1, 1),
        )
        self.db.add_all([older_id, newer_id, latest_id_but_older_time])
        self.db.commit()
        response = self.client.get("/me/notifications")
        self.assertEqual(response.status_code, 200)
        items = response.json()["items"]
        self.assertEqual([item["id"] for item in items[:2]], [newer_id.id, older_id.id])
        self.assertEqual(items[0]["sent_at"], "2030-11-19T20:20:17+09:00")
        self.assertEqual(items[1]["read_at"], "2030-11-19T20:20:17+09:00")
        self.assertIsNone(items[0]["read_at"])
        self.assertEqual([item["bus_name"] for item in items[:2]], ["1호차", "1호차"])
        self.assertTrue(all(item["bus_name"] is None for item in items[2:]))
        self.assertEqual(items[2]["id"], latest_id_but_older_time.id)
        page = self.client.get("/me/notifications", params={"limit": 1, "offset": 1})
        self.assertEqual(page.json()["items"][0]["id"], older_id.id)

    def test_alert_notifications_have_category_event_and_short_titles(self):
        bus = Bus(plate_no="12가3456", name="1호차", driver_id=self.user.id)
        self.db.add(bus)
        self.db.flush()
        for level, remaining, low_conf, message, event_type in (
            (AlertLevel.CHECK, 1, 1, "재촬영 필요", "NEEDS_CHECK"),
            (
                AlertLevel.URGENT,
                3,
                1,
                "미하차 의심 2명 (추가 확인 필요 1명)",
                "MISSED",
            ),
        ):
            with self.subTest(level=level):
                trip = Trip(bus_id=bus.id, driver_id=self.user.id)
                self.db.add(trip)
                self.db.flush()
                event = BoardingEvent(
                    trip_id=trip.id, event_type=EventType.MISMATCH,
                    person_count=remaining, low_conf_count=low_conf,
                )
                self.db.add(event)
                self.db.flush()
                alert = notifier.raise_alert(
                    self.db, trip, event, level, message, 5, remaining,
                )
                self.db.flush()
                saved = self.db.query(Notification).filter_by(alert_id=alert.id).one()
                self.assertEqual((saved.category, saved.event_type), (level.value, event_type))
                self.assertEqual(saved.title, message)
                self.assertTrue(saved.body.startswith("1호차 · "))
                self.assertIn(message, saved.body)
                self.assertEqual(saved.trip_id, trip.id)

    def test_trip_start_and_end_notifications_use_requested_text(self):
        parent = User(
            email="parent@example.com", password_hash="hash",
            name="Parent", role=UserRole.PARENT,
        )
        student = Student(name="Student")
        bus = Bus(plate_no="12가3456", name="1호차", driver_id=self.user.id)
        self.db.add_all([parent, student, bus])
        self.db.flush()
        self.db.add_all([
            Guardian(user_id=parent.id, student_id=student.id),
            BusStudent(bus_id=bus.id, student_id=student.id),
        ])
        trip = Trip(bus_id=bus.id, driver_id=self.user.id)
        self.db.add(trip)
        self.db.flush()
        for event_type, title, body in (
            ("STARTED", "운행 시작", "1호차 · 운행이 시작되었습니다."),
            ("ENDED", "운행 종료", "1호차 · 운행이 종료되었습니다."),
        ):
            with self.subTest(event_type=event_type):
                ids = notifier.create_trip_notifications(self.db, trip, event_type)
                self.assertEqual(len(ids), 1)
                saved = self.db.get(Notification, ids[0])
                self.assertEqual((saved.category, saved.event_type), ("TRIP", event_type))
                self.assertEqual((saved.title, saved.body), (title, body))
                self.assertEqual(notifier.create_trip_notifications(self.db, trip, event_type), [])

    def test_individual_read_uses_kst_clock(self):
        row = self.db.query(Notification).filter_by(
            user_id=self.user.id, read_at=None
        ).first()
        now = datetime(2026, 11, 19, 20, 20, 17)
        with patch.object(notification, "now_kst_naive", return_value=now):
            response = self.client.post(f"/me/notifications/{row.id}/read")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(row.read_at, now)


if __name__ == "__main__":
    unittest.main()
