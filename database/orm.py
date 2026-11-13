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
        # ---------- 운행 알림용 notifications 컬럼 추가 ----------
        notification_columns = {
            "trip_id": "INT NULL",
            "category": "VARCHAR(20) NOT NULL DEFAULT 'URGENT'",
            "event_type": "VARCHAR(30) NULL",
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
