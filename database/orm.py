from sqlalchemy import inspect, text
from sqlalchemy.orm import DeclarativeBase

from database.db_connection import engine


class Base(DeclarativeBase):
    pass


def init_db():
    """models 를 import 해서 테이블을 MySQL 에 생성 (이미 있으면 건너뜀)"""
    import models.models  # noqa: F401  (테이블 등록용)

    Base.metadata.create_all(bind=engine)
    user_columns = {column["name"] for column in inspect(engine).get_columns("users")}
    missing_columns = {
        "address_encrypted": "VARCHAR(1024)",
        "latitude_encrypted": "VARCHAR(1024)",
        "longitude_encrypted": "VARCHAR(1024)",
        "signup_method": "VARCHAR(20)",
        "deletion_requested_at": "DATETIME",
    }
    for column_name, column_type in missing_columns.items():
        if column_name not in user_columns:
            with engine.begin() as connection:
                connection.execute(
                    text(f"ALTER TABLE users ADD COLUMN {column_name} {column_type}")
                )

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE users SET signup_method = 'ADMIN' "
                "WHERE signup_method IS NULL AND role = 'ADMIN'"
            )
        )
        connection.execute(
            text(
                "UPDATE users SET signup_method = 'LEGACY' "
                "WHERE signup_method IS NULL"
            )
        )
