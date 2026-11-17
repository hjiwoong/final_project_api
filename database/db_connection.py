import os
from urllib.parse import quote_plus

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

load_dotenv()

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = os.getenv("DB_PORT", "3306")
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = quote_plus(os.getenv("DB_PASSWORD", ""))
DB_NAME = os.getenv("DB_NAME", "bus_safety")

# DATABASE_URL 이 있으면 우선 사용 (테스트용 sqlite 등)
DATABASE_URL = os.getenv("DATABASE_URL") or (
    f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}?charset=utf8mb4"
)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=1800,
    pool_timeout=30,
    echo=False,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db():
    """FastAPI Depends 용 세션"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
