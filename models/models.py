"""기획안 'DB 테이블' 기준 SQLAlchemy 모델

users / students / guardians / buses / bus_students /
trips / boarding_events / alerts / notifications
+ attendances (학부모 앱 '자녀 출석/결석 설정'용, 기획안 화면 설계에서 추가)
"""
import enum
from datetime import datetime

from sqlalchemy import (
    JSON, Boolean, Column, Date, DateTime, Enum, ForeignKey, Integer, String,
    Text, UniqueConstraint, func,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import relationship

from database.orm import Base

# KPI(업로드→알림 지연)를 ms 단위로 재기 위한 마이크로초 정밀 시각
# (MySQL 기본 DATETIME 은 초 단위라 DATETIME(6) 사용)
DateTimeMs = DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


# ---------- Enum ----------
class UserRole(str, enum.Enum):
    ADMIN = "ADMIN"      # 관리자(기사 포함)
    PARENT = "PARENT"    # 학부모


class TripStatus(str, enum.Enum):
    IN_PROGRESS = "IN_PROGRESS"
    CLOSED = "CLOSED"


class TripResult(str, enum.Enum):
    PENDING = "PENDING"          # 종점 사진 전
    OK = "OK"                    # 정상 (잔류 0)
    MISSED = "MISSED"            # 미하차 의심
    NEEDS_CHECK = "NEEDS_CHECK"  # 확인 필요(신뢰도 낮음)


class EventType(str, enum.Enum):
    BOARDING = "BOARDING"
    DROPOFF = "DROPOFF"
    MISMATCH = "MISMATCH"


class AlertLevel(str, enum.Enum):
    URGENT = "URGENT"    # 긴급
    CHECK = "CHECK"      # 확인 필요


class AlertStatus(str, enum.Enum):
    OPEN = "OPEN"
    ACKED = "ACKED"
    RESOLVED = "RESOLVED"


class ConfirmMethod(str, enum.Enum):
    SCAN = "scan"
    BUTTON = "button"


# ---------- 계정 ----------
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    name = Column(String(100), nullable=False)
    role = Column(Enum(UserRole), nullable=False, default=UserRole.PARENT)
    push_token = Column(String(512), nullable=True)
    created_at = Column(DateTime, default=datetime.now)

    guardians = relationship("Guardian", back_populates="user")
    notifications = relationship("Notification", back_populates="user")


# ---------- 학생 / 보호자 ----------
class Student(Base):
    __tablename__ = "students"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    class_name = Column(String(50), nullable=True)      # 예: 1-2
    consent_at = Column(DateTime, nullable=True)        # 보호자 동의 시각
    created_at = Column(DateTime, default=datetime.now)

    guardians = relationship("Guardian", back_populates="student")
    bus_students = relationship("BusStudent", back_populates="student")
    attendances = relationship("Attendance", back_populates="student")


class Guardian(Base):
    """users ↔ students N:M"""
    __tablename__ = "guardians"
    __table_args__ = (UniqueConstraint("user_id", "student_id", name="uq_guardian"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    student_id = Column(Integer, ForeignKey("students.id", ondelete="CASCADE"), nullable=False)
    relation = Column(String(30), nullable=True)        # 모/부/기타

    user = relationship("User", back_populates="guardians")
    student = relationship("Student", back_populates="guardians")


class Attendance(Base):
    """학부모 앱 출석·결석 설정 (결석이면 탑승 예정 인원에서 제외)"""
    __tablename__ = "attendances"
    __table_args__ = (UniqueConstraint("student_id", "date", name="uq_attendance"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(Integer, ForeignKey("students.id", ondelete="CASCADE"), nullable=False)
    date = Column(Date, nullable=False)
    is_absent = Column(Boolean, nullable=False, default=False)

    student = relationship("Student", back_populates="attendances")


# ---------- 버스 ----------
class Bus(Base):
    __tablename__ = "buses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    plate_no = Column(String(30), unique=True, nullable=False)   # 12가3456
    name = Column(String(50), nullable=False)                    # 1호차
    driver_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    driver = relationship("User")
    bus_students = relationship("BusStudent", back_populates="bus")
    trips = relationship("Trip", back_populates="bus")


class BusStudent(Base):
    """buses ↔ students N:M (버스별 탑승 예정 학생)"""
    __tablename__ = "bus_students"
    __table_args__ = (UniqueConstraint("bus_id", "student_id", name="uq_bus_student"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    bus_id = Column(Integer, ForeignKey("buses.id", ondelete="CASCADE"), nullable=False)
    student_id = Column(Integer, ForeignKey("students.id", ondelete="CASCADE"), nullable=False)

    bus = relationship("Bus", back_populates="bus_students")
    student = relationship("Student", back_populates="bus_students")


# ---------- 운행 ----------
class Trip(Base):
    """한 번의 운행 (승차 1장 + 종점 1장)"""
    __tablename__ = "trips"

    id = Column(Integer, primary_key=True, autoincrement=True)
    bus_id = Column(Integer, ForeignKey("buses.id"), nullable=False)
    driver_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    status = Column(Enum(TripStatus), nullable=False, default=TripStatus.IN_PROGRESS)
    result = Column(Enum(TripResult), nullable=False, default=TripResult.PENDING)

    expected_count = Column(Integer, nullable=True)     # 탑승 예정(결석 제외)
    boarding_count = Column(Integer, nullable=True)     # 승차 수
    remaining_count = Column(Integer, nullable=True)    # 종점 잔류 수
    alighted_count = Column(Integer, nullable=True)     # 하차 수 = 승차 - 잔류
    confirm_method = Column(Enum(ConfirmMethod), nullable=True)
    simulation_mode = Column(Boolean, nullable=False, default=False)

    started_at = Column(DateTime, default=datetime.now)
    ended_at = Column(DateTime, nullable=True)

    bus = relationship("Bus", back_populates="trips")
    driver = relationship("User")
    events = relationship("BoardingEvent", back_populates="trip")
    alerts = relationship("Alert", back_populates="trip")


class BoardingEvent(Base):
    """승차/하차/불일치 사진 로그"""
    __tablename__ = "boarding_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trip_id = Column(Integer, ForeignKey("trips.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type = Column(Enum(EventType), nullable=False)

    image_path = Column(String(512), nullable=True)
    person_count = Column(Integer, nullable=False, default=0)    # YOLO 검출 수
    exclude_count = Column(Integer, nullable=False, default=0)   # 기사·선생님 제외 보정
    detections = Column(JSON, nullable=True)                     # [{conf, bbox}, ...]
    low_conf_count = Column(Integer, nullable=False, default=0)  # 신뢰도 낮은 검출 수

    captured_at = Column(DateTime, nullable=True)
    uploaded_at = Column(DateTimeMs, default=datetime.now)       # KPI: 서버가 사진을 받은 시각
    image_expires_at = Column(DateTime, nullable=True)           # 보관기간 만료 → 410

    trip = relationship("Trip", back_populates="events")
    alerts = relationship("Alert", back_populates="event")


# ---------- 경고 / 알림 ----------
class Alert(Base):
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trip_id = Column(Integer, ForeignKey("trips.id", ondelete="CASCADE"), nullable=False, index=True)
    event_id = Column(Integer, ForeignKey("boarding_events.id", ondelete="SET NULL"), nullable=True)

    level = Column(Enum(AlertLevel), nullable=False, default=AlertLevel.URGENT)
    status = Column(Enum(AlertStatus), nullable=False, default=AlertStatus.OPEN)
    message = Column(String(255), nullable=True)        # 미하차 의심 1명
    llm_summary = Column(Text, nullable=True)           # LLM 요약
    note = Column(Text, nullable=True)                  # 해결 시 조치 메모

    created_at = Column(DateTime, default=datetime.now)
    acked_at = Column(DateTime, nullable=True)
    resolved_at = Column(DateTime, nullable=True)

    trip = relationship("Trip", back_populates="alerts")
    event = relationship("BoardingEvent", back_populates="alerts")
    notifications = relationship("Notification", back_populates="alert")


class Notification(Base):
    """수신자별 알림 발송 기록"""
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    alert_id = Column(Integer, ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    title = Column(String(255), nullable=False)
    body = Column(Text, nullable=True)
    sent_at = Column(DateTimeMs, default=datetime.now)      # KPI: 알림 발송(기록) 시각
    read_at = Column(DateTime, nullable=True)

    alert = relationship("Alert", back_populates="notifications")
    user = relationship("User", back_populates="notifications")
