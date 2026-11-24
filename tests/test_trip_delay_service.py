import unittest
from datetime import datetime, time as datetime_time, timedelta, timezone
from os import environ
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.time as core_time
from database.orm import Base
import routers.trip_schedule as trip_schedule_router
from models.models import (
    Bus, BusStudent, Guardian, Notification, Student, Trip, TripSchedule,
    TripStatus, User, UserRole,
)
from routers.trip_schedule import (
    TripScheduleCreate,
    TripScheduleUpdate,
    create_trip_schedule,
    upsert_trip_schedule,
)
from services import trip_delay_service


class TripDelayServiceTests(unittest.TestCase):
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
        self.now = datetime(2026, 11, 16, 14, 35)
        with self.session_factory() as db:
            self.parent = User(
                email="parent@example.com",
                password_hash="hash",
                name="Parent",
                role=UserRole.PARENT,
            )
            self.admin = User(
                email="driver@example.com",
                password_hash="hash",
                name="Driver",
                role=UserRole.ADMIN,
            )
            self.other_admin = User(
                email="admin@example.com",
                password_hash="hash",
                name="Admin",
                role=UserRole.ADMIN,
            )
            self.student = Student(name="Student")
            db.add_all([self.parent, self.admin, self.other_admin, self.student])
            db.flush()
            self.bus = Bus(
                plate_no="12가3456",
                name="1호차",
                driver_id=self.admin.id,
            )
            db.add(self.bus)
            db.flush()
            db.add_all(
                [
                    Guardian(user_id=self.parent.id, student_id=self.student.id),
                    BusStudent(bus_id=self.bus.id, student_id=self.student.id),
                ]
            )
            self.schedule = TripSchedule(
                bus_id=self.bus.id,
                service_date=self.now.date(),
                scheduled_at=self.now - timedelta(minutes=5),
            )
            db.add(self.schedule)
            db.commit()
            self.schedule_id = self.schedule.id
            self.bus_id = self.bus.id
            self.admin_id = self.admin.id
            self.parent_id = self.parent.id
            self.other_admin_id = self.other_admin.id

    def tearDown(self):
        self.engine.dispose()

    def _check(self, now=None):
        with (
            patch.object(trip_delay_service, "SessionLocal", self.session_factory),
            patch.object(trip_delay_service.notifier, "send_trip_pushes") as send,
        ):
            trip_delay_service.check_delayed_trips(now or self.now)
        return send

    def test_notifies_assigned_parents_and_all_admins_after_five_minutes(self):
        send = self._check()

        with self.session_factory() as db:
            notifications = db.query(Notification).all()
            self.assertEqual(
                {item.user_id for item in notifications},
                {self.parent_id, self.admin_id, self.other_admin_id},
            )
            self.assertTrue(
                all(item.event_type == "NOT_STARTED" for item in notifications)
            )
            self.assertTrue(all(item.category == "CHECK" for item in notifications))
            self.assertTrue(all(item.title == "버스 미출발" for item in notifications))
            self.assertTrue(all(item.body.startswith("1호차 · ") for item in notifications))
            self.assertTrue(all("5분 이상" in item.body for item in notifications))
            self.assertIsNotNone(
                db.get(TripSchedule, self.schedule_id).delayed_notified_at
            )
        send.assert_called_once()

    def test_does_not_notify_before_five_minutes(self):
        with (
            patch.object(trip_delay_service, "SessionLocal", self.session_factory),
            patch.object(trip_delay_service.notifier, "send_trip_pushes") as send,
        ):
            trip_delay_service.check_delayed_trips(
                self.now - timedelta(seconds=1)
            )

        with self.session_factory() as db:
            self.assertEqual(db.query(Notification).count(), 0)
            self.assertIsNone(db.get(TripSchedule, self.schedule_id).delayed_notified_at)
        send.assert_not_called()

    def test_on_time_departure_suppresses_missed_departure_notification(self):
        with self.session_factory() as db:
            departure = Trip(
                bus_id=self.bus_id,
                driver_id=self.admin_id,
                status=TripStatus.IN_PROGRESS,
                started_at=self.schedule.scheduled_at + timedelta(minutes=5),
            )
            db.add(departure)
            db.flush()
            db.commit()

        self._check()

        with self.session_factory() as db:
            self.assertEqual(db.query(Notification).count(), 0)

    def test_late_departure_still_notifies_after_the_five_minute_deadline(self):
        with self.session_factory() as db:
            schedule = db.get(TripSchedule, self.schedule_id)
            departure = Trip(
                bus_id=self.bus_id,
                driver_id=self.admin_id,
                status=TripStatus.IN_PROGRESS,
                started_at=schedule.scheduled_at + timedelta(minutes=6),
            )
            db.add(departure)
            db.flush()
            schedule.trip_id = departure.id
            db.commit()

        self._check(self.now + timedelta(minutes=2))

        with self.session_factory() as db:
            notifications = db.query(Notification).all()
            self.assertEqual(len(notifications), 3)

    def test_schedule_can_be_created_and_updated_until_trip_starts(self):
        scheduled_at = core_time.now_kst_naive() + timedelta(hours=2)
        body = TripScheduleUpdate(
            service_date=scheduled_at.date(),
            start_time=scheduled_at.time().replace(second=0, microsecond=0),
        )
        with self.session_factory() as db:
            created = upsert_trip_schedule(
                self.bus_id,
                body,
                me=self.admin,
                db=db,
            )
        updated_at = core_time.now_kst_naive() + timedelta(hours=3)
        updated = TripScheduleUpdate(
            service_date=updated_at.date(),
            start_time=updated_at.time().replace(second=0, microsecond=0),
        )
        with self.session_factory() as db:
            updated_schedule = upsert_trip_schedule(
                self.bus_id,
                updated,
                me=self.admin,
                db=db,
            )

        self.assertEqual(created["id"], updated_schedule["id"])
        self.assertEqual(updated_schedule["scheduled_at"].hour, updated.start_time.hour)

    def test_schedule_in_the_past_by_kst_is_rejected_with_utc_server_clock(self):
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                utc_now = datetime(2026, 11, 16, 0, 0, tzinfo=timezone.utc)
                return utc_now.astimezone(tz) if tz else utc_now.replace(tzinfo=None)

        body = TripScheduleUpdate(
            service_date=datetime(2026, 11, 16).date(),
            start_time=datetime_time(8, 30),
        )
        with (
            patch.dict(environ, {"TZ": "UTC"}),
            patch.object(core_time, "datetime", FixedDateTime),
            patch.object(trip_schedule_router, "datetime", FixedDateTime),
        ):
            self.assertEqual(FixedDateTime.now().hour, 0)
            self.assertEqual(core_time.now_kst_naive().hour, 9)
            for endpoint, args in (
                (create_trip_schedule, (TripScheduleCreate(
                    bus_id=self.bus_id,
                    service_date=body.service_date,
                    start_time=body.start_time,
                ),)),
                (upsert_trip_schedule, (self.bus_id, body)),
            ):
                with self.subTest(endpoint=endpoint.__name__):
                    with self.session_factory() as db:
                        with self.assertRaises(HTTPException) as raised:
                            endpoint(*args, me=self.admin, db=db)
                    self.assertEqual(raised.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
