from sqlalchemy import inspect, text
from sqlalchemy.orm import DeclarativeBase

from database.db_connection import engine
from services.location_crypto import encrypt_location_value, validate_location_encryption_key


class Base(DeclarativeBase):
    pass


GUARDIAN_COORDINATE_COLUMNS = ("latitude", "longitude", "latitude_encrypted", "longitude_encrypted")


def _move_guardian_coordinates_to_students(connection, user_columns: set[str]) -> None:
    """보호자(users) 좌표 컬럼을 자녀(students) 좌표 컬럼으로 옮기고 users 컬럼을 삭제한다.

    자녀에 좌표가 아직 없을 때만 채우며, 보호자가 여러 명이면 가장 먼저 연결된 보호자의 값을 쓴다.
    복사가 끝난 뒤에만 컬럼을 삭제하므로 재시작해도 안전하다.
    """
    if {"latitude_encrypted", "longitude_encrypted"} <= user_columns:
        address_expr = "u.address_encrypted" if "address_encrypted" in user_columns else "NULL"
        connection.execute(text(f"""
            UPDATE students s
            JOIN (
                SELECT g.student_id, MIN(g.id) AS guardian_id
                FROM guardians g
                JOIN users u ON u.id = g.user_id
                WHERE u.latitude_encrypted IS NOT NULL AND u.longitude_encrypted IS NOT NULL
                GROUP BY g.student_id
            ) first_guardian ON first_guardian.student_id = s.id
            JOIN guardians g ON g.id = first_guardian.guardian_id
            JOIN users u ON u.id = g.user_id
            SET s.latitude_encrypted = u.latitude_encrypted,
                s.longitude_encrypted = u.longitude_encrypted,
                s.address_encrypted = {address_expr}
            WHERE s.latitude_encrypted IS NULL AND s.longitude_encrypted IS NULL
        """))

    # 암호화 이전의 평문 컬럼이 남아 있는 아주 오래된 DB
    if {"latitude", "longitude"} <= user_columns:
        address_select = ", address" if "address" in user_columns else ""
        rows = connection.execute(text(
            f"SELECT id, latitude, longitude{address_select} FROM users "
            "WHERE latitude IS NOT NULL AND longitude IS NOT NULL"
        )).mappings().all()
        for row in rows:
            address = row["address"] if "address" in row else None
            connection.execute(
                text("""
                    UPDATE students SET
                        latitude_encrypted = :latitude,
                        longitude_encrypted = :longitude,
                        address_encrypted = :address
                    WHERE latitude_encrypted IS NULL AND longitude_encrypted IS NULL
                      AND id IN (SELECT student_id FROM guardians WHERE user_id = :user_id)
                """),
                {
                    "user_id": row["id"],
                    "latitude": encrypt_location_value(f"{float(row['latitude']):.7f}"),
                    "longitude": encrypt_location_value(f"{float(row['longitude']):.7f}"),
                    "address": encrypt_location_value(address) if address else None,
                },
            )

    for column in GUARDIAN_COORDINATE_COLUMNS:
        if column in user_columns:
            connection.execute(text(f"ALTER TABLE users DROP COLUMN {column}"))


def _upgrade_attendance_files(connection) -> None:
    import os
    import uuid

    from services.attendance_file_storage import UPLOAD_DIR

    columns = {c["name"] for c in inspect(connection).get_columns("attendance_files")}
    if "original_name" not in columns:
        connection.execute(text(
            "ALTER TABLE attendance_files ADD COLUMN original_name VARCHAR(255) NOT NULL DEFAULT ''"
        ))
        if "file_name" in columns:
            connection.execute(text("UPDATE attendance_files SET original_name = file_name"))
    if "stored_name" not in columns:
        connection.execute(text("ALTER TABLE attendance_files ADD COLUMN stored_name VARCHAR(64) NULL"))
    if "uploaded_by" not in columns:
        connection.execute(text("ALTER TABLE attendance_files ADD COLUMN uploaded_by INT NULL"))
    if "data" in columns:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        rows = connection.execute(text(
            "SELECT id, data, stored_name FROM attendance_files"
        )).mappings().all()
        for row in rows:
            stored = row["stored_name"] or uuid.uuid4().hex
            if row["data"]:
                with open(os.path.join(UPLOAD_DIR, stored), "wb") as fh:
                    fh.write(row["data"])
            connection.execute(
                text("UPDATE attendance_files SET stored_name = :s WHERE id = :id"),
                {"s": stored, "id": row["id"]},
            )
        connection.execute(text("ALTER TABLE attendance_files DROP COLUMN data"))
    if "file_name" in columns:
        connection.execute(text("ALTER TABLE attendance_files DROP COLUMN file_name"))
    connection.execute(text(
        "UPDATE attendance_files SET stored_name = REPLACE(UUID(), '-', '') WHERE stored_name IS NULL"
    ))


def init_db():
    """models 를 import 해서 테이블을 MySQL 에 생성 (이미 있으면 건너뜀)"""
    import models.models  # noqa: F401  (테이블 등록용)

    validate_location_encryption_key()
    Base.metadata.create_all(bind=engine)
    student_location_columns = {
        "address_encrypted": "VARCHAR(1024) NULL",
        "latitude_encrypted": "VARCHAR(255) NULL",
        "longitude_encrypted": "VARCHAR(255) NULL",
    }
    other_user_columns = {
        "deletion_requested_at": "DATETIME NULL",
        "signup_method": "VARCHAR(20) NOT NULL DEFAULT 'UNKNOWN'",
    }
    with engine.begin() as connection:
        inspector = inspect(connection)
        student_columns = {column["name"] for column in inspector.get_columns("students")}
        for column, column_type in student_location_columns.items():
            if column not in student_columns:
                connection.execute(text(f"ALTER TABLE students ADD COLUMN {column} {column_type}"))

        existing = {column["name"] for column in inspector.get_columns("users")}
        for column, column_type in other_user_columns.items():
            if column not in existing:
                connection.execute(text(f"ALTER TABLE users ADD COLUMN {column} {column_type}"))
                existing.add(column)
                if column == "signup_method":
                    # 컬럼을 처음 만들 때만: 기존 관리자 계정은 ADMIN, 나머지는 기본값 UNKNOWN
                    connection.execute(text(
                        "UPDATE users SET signup_method = 'ADMIN' WHERE role = 'ADMIN'"
                    ))

        # admin-app-new 브랜치로 이미 실행된 DB 정리:
        # 그 버전은 signup_method 를 NULL 허용으로 만들고 일반 계정에 'LEGACY' 를 넣었다.
        # 'LEGACY' 는 허용 값(APP/GOOGLE/ADMIN/UNKNOWN)이 아니므로 UNKNOWN 으로 바꾼다.
        connection.execute(text(
            "UPDATE users SET signup_method = 'UNKNOWN' "
            "WHERE signup_method IS NULL OR signup_method = 'LEGACY'"
        ))

        _move_guardian_coordinates_to_students(connection, existing)

        indexes = {index["name"] for index in inspector.get_indexes("users")}
        if "ix_users_deletion_requested_at" not in indexes:
            connection.execute(text(
                "CREATE INDEX ix_users_deletion_requested_at ON users (deletion_requested_at)"
            ))
        # ---------- 결석 사유·확인 상태 컬럼 (migrations/attendance_v3.sql 과 같은 결과) ----------
        attendance_existing = {c["name"] for c in inspect(connection).get_columns("attendances")}
        for column, column_type in {
            "reason_type": "ENUM('SICK', 'FAMILY', 'FIELD_TRIP', 'OTHER') NULL",
            "reason_memo": "TEXT NULL",
            "status": "ENUM('REQUESTED', 'CONFIRMED', 'REJECTED') NULL",
            "confirmed_by": "INT NULL",
            "confirmed_at": "DATETIME NULL",
            "reject_reason": "VARCHAR(200) NULL",
        }.items():
            if column not in attendance_existing:
                connection.execute(text(f"ALTER TABLE attendances ADD COLUMN {column} {column_type}"))
        if "status" not in attendance_existing:
            connection.execute(text(
                "UPDATE attendances SET status = 'REQUESTED' WHERE is_absent = 1 AND status IS NULL"
            ))

        # ---------- 결석 첨부 표 (migrations/attendance_files_v1.sql 구조) ----------
        # 이전 main 패치는 file_name·data(파일 내용을 DB 에) 구조였다 → 파일을 디스크로 옮기고 칸 이름을 맞춘다
        _upgrade_attendance_files(connection)

        # ---------- 운행 알림용 notifications 컬럼 추가 ----------
        notification_columns = {
            "trip_id": "INT NULL",
            "category": "VARCHAR(20) NOT NULL DEFAULT 'URGENT'",
            "event_type": "VARCHAR(30) NULL",
            "attendance_id": "INT NULL",
        }

        notification_existing = {
            column["name"]
            for column in inspect(connection).get_columns("notifications")
        }

        for column, column_type in notification_columns.items():
            if column not in notification_existing:
                connection.execute(
                    text(
                        f"ALTER TABLE notifications "
                        f"ADD COLUMN {column} {column_type}"
                    )
                )

        # 일반 운행 알림은 alert_id 없이 저장 가능
        alert_id_column = next(
            column
            for column in inspect(connection).get_columns("notifications")
            if column["name"] == "alert_id"
        )

        if not alert_id_column["nullable"]:
            connection.execute(
                text(
                    "ALTER TABLE notifications "
                    "MODIFY COLUMN alert_id INT NULL"
                )
            )

        # 기존 경고 알림의 카테고리 보정
        connection.execute(
            text(
                "UPDATE notifications "
                "SET category = 'URGENT' "
                "WHERE alert_id IS NOT NULL "
                "AND (category IS NULL OR category = '')"
            )
        )
        # '확인 필요' 경고 알림이 URGENT 로 저장돼 앱 '확인 필요' 탭이 비어 있던 것 보정
        connection.execute(
            text(
                "UPDATE notifications SET category = 'CHECK' "
                "WHERE category = 'URGENT' AND alert_id IN "
                "(SELECT id FROM alerts WHERE level = 'CHECK')"
            )
        )
