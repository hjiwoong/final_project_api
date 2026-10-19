import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.security import require_admin
from database.db_connection import get_db
from database.orm import init_db
from services.detector import MODE as DETECTOR_MODE
from routers import (
    alert, boarding_event, bus, bus_student, child_request, guardian,
    notification, social, stats, student, student_admin, trip, user,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()   # 서버 시작 시 MySQL 에 테이블 생성
    yield


app = FastAPI(title="AI 통학버스 미하차 교차검증·안전관리", lifespan=lifespan)

# 관리자 웹(브라우저)에서 호출할 수 있도록 CORS 허용 (개발용)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(user.router)
app.include_router(bus.router, dependencies=[Depends(require_admin)])
app.include_router(student.router)
app.include_router(bus_student.router, dependencies=[Depends(require_admin)])
app.include_router(guardian.router, dependencies=[Depends(require_admin)])
app.include_router(trip.router)
app.include_router(boarding_event.router)
app.include_router(alert.router)
app.include_router(notification.router)
app.include_router(child_request.router)
app.include_router(social.router)
app.include_router(student_admin.router)
app.include_router(stats.router)


@app.get("/health", tags=["시스템"])
def health(db: Session = Depends(get_db)):
    """서버·DB 상태, detector_mode"""
    db.execute(text("SELECT 1"))
    return {"status": "ok", "db": "connected", "detector_mode": DETECTOR_MODE}


# 관리자 웹 화면 (web/ 폴더) - 주소창에 직접 입력: http://127.0.0.1:8000/admin/  (API 문서는 /docs)
# 주의: 이 마운트는 반드시 모든 include_router 보다 뒤에 둘 것.
#       /admin/users, /admin/buses 같은 API 경로가 먼저 매칭되어야 화면과 API 가 겹치지 않는다.
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
app.mount("/admin", StaticFiles(directory=WEB_DIR, html=True), name="admin-web")


def check_db_or_exit():
    """python main.py 실행 시 서버를 켜기 전에 DB 연결을 먼저 확인하고, 실패하면 원인을 짧게 안내"""
    import sys
    from database.db_connection import engine

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as e:  # noqa: BLE001
        orig = getattr(e, "orig", e)
        code = orig.args[0] if getattr(orig, "args", None) and isinstance(orig.args[0], int) else None
        hints = {
            2003: "MySQL 서버에 연결할 수 없습니다. MySQL 이 켜져 있는지, .env 의 DB_HOST / DB_PORT 가 맞는지 확인하세요.",
            1045: "아이디 또는 비밀번호가 틀렸습니다. .env 의 DB_USER / DB_PASSWORD 를 확인하세요.",
            1049: "DB 가 아직 없습니다. MySQL 에서 먼저 만드세요:\n"
                  "       CREATE DATABASE bus_safety DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;",
        }
        u = engine.url
        print("\n[DB 연결 실패] 서버를 시작하지 않았습니다.")
        print(f"  접속 대상: {u.username}@{u.host}:{u.port}/{u.database}")
        print(f"  원인: {orig}")
        print(f"  해결: {hints.get(code, '.env 의 DB_* 값과 MySQL 상태를 확인하세요. (위 원인 문구를 그대로 검색하면 빠릅니다)')}\n")
        sys.exit(1)


if __name__ == "__main__":  # 직접 실행 시 uvicorn 서버 시작, python main.py 로 실행할 때만 동작
    import uvicorn

    check_db_or_exit()
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
