from sqlalchemy import inspect, text
from sqlalchemy.orm import DeclarativeBase

from database.db_connection import engine
from services.location_crypto import encrypt_location_value, validate_location_encryption_key


class Base(DeclarativeBase):
    pass


def init_db():
    """models 를 import 해서 테이블을 MySQL 에 생성 (이미 있으면 건너뜀)"""
    import models.models  # noqa: F401  (테이블 등록용)

    validate_location_encryption_key()
    Base.metadata.create_all(bind=engine)
    encrypted_location_columns = {
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
        existing = {column["name"] for column in inspector.get_columns("users")}
        legacy_location_columns = [
            column for column in ("address", "latitude", "longitude") if column in existing
        ]
        for column, column_type in {**encrypted_location_columns, **other_user_columns}.items():
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

        if legacy_location_columns:
            selected = ", ".join(["id", *legacy_location_columns])
            rows = connection.execute(text(f"SELECT {selected} FROM users")).mappings().all()
            encrypted_columns = {
                "address": "address_encrypted",
                "latitude": "latitude_encrypted",
                "longitude": "longitude_encrypted",
            }
            assignments = ", ".join(
                f"{encrypted_columns[column]} = :{encrypted_columns[column]}"
                for column in legacy_location_columns
            )
            clear_legacy = ", ".join(f"{column} = NULL" for column in legacy_location_columns)
            for row in rows:
                values = {"id": row["id"]}
                has_location = False
                for column in legacy_location_columns:
                    value = row[column]
                    encrypted_column = encrypted_columns[column]
                    values[encrypted_column] = (
                        encrypt_location_value(value) if value is not None else None
                    )
                    has_location = has_location or value is not None
                if has_location:
                    connection.execute(
                        text(
                            f"UPDATE users SET {assignments}, {clear_legacy} WHERE id = :id"
                        ),
                        values,
                    )
                else:
                    connection.execute(
                        text(f"UPDATE users SET {clear_legacy} WHERE id = :id"),
                        {"id": row["id"]},
                    )

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
