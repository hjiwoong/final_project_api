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
