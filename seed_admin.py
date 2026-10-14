"""첫 관리자 계정 생성:  python seed_admin.py
(/web/users 가 관리자 전용이라 첫 관리자는 스크립트로 만듭니다)"""
import bcrypt

from database.db_connection import SessionLocal
from database.orm import init_db
from models.models import User, UserRole

EMAIL = "web@school.kr"
PASSWORD = "admin1234"     # 만든 뒤 꼭 바꾸세요
NAME = "관리자"

init_db()
db = SessionLocal()
if db.query(User).filter(User.email == EMAIL).first():
    print("이미 존재합니다:", EMAIL)
else:
    hashed = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt()).decode()
    db.add(User(email=EMAIL, password_hash=hashed, name=NAME, role=UserRole.ADMIN))
    db.commit()
    print("관리자 생성 완료:", EMAIL, "/", PASSWORD)
db.close()
