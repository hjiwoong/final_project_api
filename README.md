# AI 통학버스 미하차 교차검증 · 안전관리 서비스 — API 서버

통학버스 기사가 운행 시작(승차)과 종점(하차) 시점에 차량 내부 사진을 찍어 올리면, 서버가 YOLO로 인원을 세고 승차 인원과 비교해 미하차(차 안에 남은 아이) 의심을 판정합니다. 의심되면 기사·관리자·보호자에게 앱 알림과 FCM 푸시를 보내고, 기사가 차량 내부를 직접 확인한 뒤 사유를 남겨야 운행을 종료할 수 있습니다.

- 앱(기사·관리자 / 학부모): [hjiwoong/final_project_app](https://github.com/hjiwoong/final_project_app) (Flutter)
- 관리자 웹: 이 서버의 `/admin/` (정적 파일 `web/`)
- API 문서: 서버 실행 후 `/docs` (Swagger)

> AI 판정은 기사의 하차 확인 의무를 대신하지 않는 보조 수단입니다. 판정 결과와 관계없이 기사가 차량 내부를 직접 확인해야 운행이 종료됩니다.

## 주요 기능

| 사용자 | 기능 |
|---|---|
| 기사 (앱) | 운행 시작, 탑승 지도, 승차·종점 사진 촬영 → 인원 판정, 재촬영, 안전 확인 후 운행 종료, 출결 확인 |
| 학부모 (앱) | 회원가입 약관 동의, 자녀 연결 요청, 결석 설정, 버스 운행 상태, 미하차·운행 시작·종료·미출발 알림, 경고 상세·AI 대응 수칙, 챗봇, 자녀 위치 |
| 관리자 (웹) | 대시보드(알림 지연 포함), 운행·경고 실시간 확인과 처리, 자녀 연결 승인, 학생·보호자·버스·배정·출발 예정 시각 관리 |

## 결과 요약

| 항목 | 목표 | 결과 |
|---|---|---|
| 인원 카운트 정확도 (±1명 이내 사진 비율) | 80% 이상 | 98.5% (Test 66장, 정확히 일치 83.3%) |
| 미하차 감지 정확도 (사람이 남은 사진에서 경고가 나는 비율) | 90% 이상 | 90.7% (43장 중 39장) |
| 서버 기능 테스트 | - | 44개 항목 통과 |

학습·평가 데이터는 아동 사진이나 인터넷 이미지 대신 팀원이 시연 차량에서 직접 촬영한 사진 216장입니다(성인 팀원, 비공개 보관). 놓친 사례는 누운 자세와 아주 어두운 사진이었습니다.

## 팀

| 이름 | 담당 |
|---|---|
| 전지웅 | 서버·DB, 모델 학습, AI 연계 |
| 김태환 | 관리자 웹, 문서 |
| 김민지 | AI 데이터, 학부모 앱 |
| 김지수 | AI 데이터, 관리자(기사) 앱 |

## 기술 스택

| 구분 | 사용 기술 |
|---|---|
| 서버 | Python, FastAPI, Uvicorn, APScheduler |
| DB | MySQL (`bus_safety`), SQLAlchemy 2, PyMySQL |
| 인증 | JWT(HS256, 기본 720분), bcrypt, Google 로그인 |
| AI | Ultralytics YOLO (`ultralytics==8.4.182`), Google Gemini (요약·RAG·챗봇) |
| 알림 | Firebase Cloud Messaging(`firebase-admin`), SSE(`/stream/events`) |
| 위치 | 네이버 클라우드 Geocoding·지역 검색 API, Fernet 암호화 |

## 폴더 구조

```
main.py               앱 생성, 라우터 등록, 스케줄러(미출발 알림·사진 삭제·계정 삭제), /health, /admin 마운트
core/                 JWT·비밀번호(security.py), 시간대(time.py)
database/             DB 연결(.env 읽기), 테이블 생성·마이그레이션
models/models.py      ORM 모델 (17개 테이블)
routers/              API 라우터 (user, bus, student, trip, boarding_event, alert, notification, rag, chat 등)
services/             인원 감지(detector), 판정(judge), Agent, LLM·RAG·챗봇, 푸시(notifier), 사진 저장·자동 삭제
knowledge/            RAG 지식 문서 (법령·매뉴얼 PDF, 대응 수칙 .md)
weights/              YOLO 가중치 (.pt)
web/                  관리자 웹 (HTML/JS)
seed_admin.py         첫 관리자 계정 생성 스크립트
```

## 실행 방법

1. Python 가상환경을 만들고 패키지를 설치합니다.
   ```
   pip install -r requirements.txt
   ```
   `ultralytics`는 학습·평가에 쓴 버전과 맞추기 위해 `8.4.182`로 고정했습니다.
2. MySQL에 `bus_safety` 데이터베이스를 만듭니다. 테이블은 서버 시작 시 자동 생성됩니다.
   ```sql
   CREATE DATABASE bus_safety DEFAULT CHARACTER SET utf8mb4;
   ```
3. `.env.example`을 복사해 `.env`를 만들고 값을 채웁니다 (아래 환경변수 표 참고). `.env`와 Firebase 서비스 계정 JSON은 저장소에 올리지 않습니다.
4. 첫 관리자 계정을 만듭니다. 스크립트에 적힌 기본 비밀번호는 만든 뒤 바로 바꾸세요.
   ```
   python seed_admin.py
   ```
5. 서버를 실행합니다.
   ```
   python main.py
   ```
   `0.0.0.0:8000`에서 실행됩니다. 앱은 같은 네트워크에서 서버 PC의 IP(`http://<서버IP>:8000`)로 접속합니다. 시연 환경은 HTTP이며, 실제 운영 시에는 HTTPS(리버스 프록시) 적용이 필요합니다.
6. `GET /health`로 DB·인원 감지 모드·푸시·AI 설정 상태를 확인합니다. 관리자 웹은 `http://<서버IP>:8000/admin/`에서 엽니다.
7. 앱([final_project_app](https://github.com/hjiwoong/final_project_app))은 서버 주소를 넣어 실행합니다.
   ```
   flutter pub get
   flutter run --dart-define=API_BASE_URL=http://<서버IP>:8000
   ```

## 환경변수 (`.env`)

| 구분 | 변수 | 설명 (기본값) |
|---|---|---|
| DB | `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` | MySQL 접속 정보 (`127.0.0.1`, `3306`, `root`, 빈 값, `bus_safety`). `DATABASE_URL`을 쓰면 이 값보다 우선 |
| 인증 | `JWT_SECRET`, `JWT_EXPIRE_MINUTES` | 토큰 서명 키(반드시 변경), 만료 시간(`720`) |
| 인증 | `GOOGLE_CLIENT_ID` | Google 로그인 클라이언트 ID (쉼표로 여러 개) |
| 위치 | `LOCATION_ENCRYPTION_KEY` | 자녀 주소·좌표 암호화 키 (Fernet). 없으면 서버 시작 중단 |
| 위치 | `NAVER_MAPS_CLIENT_ID`, `NAVER_MAPS_CLIENT_SECRET`, `NAVER_SEARCH_CLIENT_ID`, `NAVER_SEARCH_CLIENT_SECRET` | 네이버 Geocoding·지역 검색 API 키 |
| 인원 감지 | `DETECTOR_MODE` | `real` 실제 추론 / `mock` 시연용 가짜 결과 (`real`) |
| 인원 감지 | `YOLO_MODEL` / `YOLO_MODELS` | 사용할 모델 경로. 서비스는 `YOLO_MODEL`에 k5 하나 (아래 '사람 검출 모델' 참고) |
| 인원 감지 | `CONF_THRESHOLD`, `LOW_CONF_THRESHOLD`, `IOU_THRESHOLD`, `IMG_SIZE`, `DETECTOR_TIMEOUT_SEC` | 사람 판정 신뢰도(`0.25`), 신뢰도 낮음 기준(`0.5`), 박스 병합 IoU(`0.5`), 입력 크기(`640`), 추론 제한 시간(`12`초) |
| 사진 | `UPLOAD_DIR`, `IMAGE_RETENTION_DAYS` | 사진 저장 폴더(`uploads`), 보관 기간(`14`일) |
| 푸시 | `FIREBASE_SERVICE_ACCOUNT_FILE` 또는 `GOOGLE_APPLICATION_CREDENTIALS`, `FCM_ANDROID_CHANNEL_ID` | Firebase 서비스 계정 JSON 경로, Android 알림 채널(`safety_alerts`) |
| AI | `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_EMBED_MODEL`, `LLM_ENABLED`, `LLM_SEND_IMAGE`, `RAG_ENABLED`, `CHAT_ENABLED`, `AGENT_LLM_GUIDANCE`, `AGENT_ESCALATE_DARK`, `DARK_BRIGHTNESS_THRESHOLD` | 'AI 연계' 절 참고 |
| 기타 | `RELOAD` | `1`이면 코드 변경 시 자동 재시작 (개발용) |

## 사람 검출 모델

서비스는 팀원이 시연 차량에서 직접 촬영한 사진으로 YOLO26s를 전이학습한 `weights/bus_person_k5_final_26s_1109_0926.pt`(k5, 최종 모델) **하나로** 사람을 셉니다. 같은 Test 66장 기준 인원 정확 일치율 0.833, 재현율 0.895로 이전 모델(k1~k4) 중 미하차 경고 누락이 가장 적었습니다. 자세한 수치는 노션의 AI 성능 평가 보고서를 참고하세요.

예전 모델(`bus_person_1029_0718.pt`)·COCO 모델(`yolo11s.pt`)과의 3개 모델 앙상블도 검토했지만, 함께 묶을 모델의 성능이 낮고 검출을 합치는 방식이라 창밖 행인 같은 오검출이 늘 수 있어 쓰지 않습니다. 앙상블 기능 자체는 실험용으로 남겨 두었습니다.

| 설정 | 동작 |
|---|---|
| `YOLO_MODEL=weights/bus_person_k5_final_26s_1109_0926.pt` | **서비스 설정.** 모델 하나로 추론 |
| `YOLO_MODELS=a.pt,b.pt,c.pt` | 쉼표로 적은 모델을 모두 불러 앙상블 추론 (실험용). 각 모델의 사람 박스를 합친 뒤 겹치는 박스를 억제해 중복 인원을 줄임. 있으면 `YOLO_MODEL`보다 우선 |
| 둘 다 비움 | `weights/`의 k5 모델 하나 사용 |

설정한 모델을 하나라도 불러오지 못하면 잘못된 결과를 내지 않도록 추론을 실패(`503`) 처리합니다. 신규 모델의 `passenger` 클래스와 COCO 모델의 `person` 클래스를 모두 사람으로 셉니다. 시연용 목업이 필요할 때만 `DETECTOR_MODE=mock`으로 바꾸세요.

## 사진 보관과 자동 삭제

업로드된 승하차 사진은 `UPLOAD_DIR/trip_{운행ID}/`에 저장되며 `IMAGE_RETENTION_DAYS`(기본 14일)가 지나면 자동 삭제됩니다. 서버가 켜질 때와 이후 1시간마다 `services/image_retention.py`가 만료된 사진 파일과 `event_images` 행을 지우고 이벤트의 `image_path`를 비웁니다. 인원 수·판정 결과·경고 기록은 남습니다. 보관 기간이 기록되지 않은 예전 이벤트는 업로드 시각 기준으로 처리합니다.

## 백그라운드 작업 (APScheduler)

| 작업 | 주기 | 내용 |
|---|---|---|
| 미출발 알림 | 1분 | 출발 예정 시각 5분 후에도 운행이 시작되지 않으면 `NOT_STARTED` 알림 1회 |
| 사진 자동 삭제 | 1시간 | 보관 기간이 지난 사진 삭제 |
| 계정 삭제 | 1시간 | 30일 유예가 끝난 학부모 계정 삭제 |

스케줄러는 서버 프로세스 안에서 돌기 때문에 최소 한 개의 API 인스턴스가 계속 실행 중이어야 합니다.

## 회원가입 약관 동의

| Method | Path | Description |
|---|---|---|
| `GET` | `/terms` | 필수 약관 목록(`type`, `title`, `version`, `required`). 인증 불필요 |
| `GET` | `/me/agreements` | 내 약관 동의 이력 조회 |
| `POST` | `/me/agreements` | 기존 학부모가 현재 약관에 동의 (일부만 제출 가능, 응답의 `pending_terms`에 남은 약관 표시) |

인증 없이 `GET /terms`를 호출하면 필수 약관의 `type`, `title`, `version`, `required` 목록을 반환합니다. 일반 회원가입(`POST /auth/signup`)과 신규 Google 가입(`POST /auth/google`)에는 응답의 세 약관에 대한 동의를 다음 형식으로 `agreements`에 포함해야 합니다.

```json
{"agreements": [
  {"term_type": "COOKIE", "term_version": "2026-11-18"},
  {"term_type": "TERMS", "term_version": "2026-11-18"},
  {"term_type": "PRIVACY", "term_version": "2026-11-18"}
]}
```

약관이 누락되거나 버전이 현재 버전과 다르면 가입이 거부됩니다. 앱은 가입 직전에 현재 버전을 조회해야 하며, 동의를 보내지 않는 기존 앱의 신규 가입은 더 이상 동작하지 않습니다. 기존 Google 계정 로그인과 기존 계정의 Google 연결, 관리자 계정 생성에는 동의가 필요하지 않습니다. 로그인한 사용자는 `GET /me/agreements`에서 자신의 동의 이력을 조회할 수 있으며, 동의 시각은 서버가 KST 벽시각으로 기록합니다.

기존 학부모도 로그인 후 `POST /me/agreements`에 위와 같은 `agreements` 목록을 보내 현재 약관에 동의할 수 있습니다. 일부만 제출해도 되며, 응답의 `pending_terms`에 남은 필수 약관이 표시됩니다. 이미 기록된 동일 버전의 동의 시각은 유지하고, 누락된 버전만 저장합니다. 오래된 버전은 422로 거부합니다. 로그인 토큰이 필요하며 다른 계정 대신 동의할 수 없습니다.

로그인·가입·Google 로그인·계정 복구 응답과 `GET /me`의 `pending_terms`에서 미동의 필수 약관을 확인할 수 있습니다. 미동의 학부모가 자녀·버스·위치·알림·채팅 등의 보호된 API를 호출하면 `403`과 `detail.code: "TERMS_REQUIRED"` 및 `detail.pending_terms`가 반환됩니다. 로그인, 약관 조회·동의, 계정 삭제 요청, 푸시 토큰 등록·해제는 동의 전에도 이용할 수 있습니다. 관리자는 동의 검사의 대상이 아닙니다. 이전에 배포된 앱은 이 403을 처리하지 못할 수 있으므로 앱 업데이트를 준비한 뒤 서버를 배포하세요.

`terms_agreements` 테이블은 서버 시작 시 자동 생성됩니다. 서버를 시작하기 전에 새 테이블만 생성하려면 DB 접속 환경을 설정한 뒤 다음을 실행하세요.

```powershell
python -c "from database.orm import Base; from database.db_connection import engine; from models.models import TermsAgreement; Base.metadata.create_all(bind=engine, tables=[TermsAgreement.__table__])"
```

## 학부모 자녀 연결 API

모든 학부모 API는 로그인 후 발급받은 액세스 토큰을 `Authorization: Bearer <토큰>` 헤더에 포함해야 합니다.
계정 상세 정보에는 최초 생성 경로(`APP`, `GOOGLE`, `ADMIN`, `UNKNOWN`)를 포함합니다. 이메일 계정에 Google 로그인을 나중에 연결해도 최초 생성 경로는 변경되지 않으며, 기존 계정은 `UNKNOWN`으로 마이그레이션됩니다.

| Method | Path | Description |
|---|---|---|
| `PUT` | `/me/password` | 로그인 사용자의 현재 비밀번호를 검증한 뒤 새 비밀번호로 변경 |
| `GET` | `/me/child-requests` | 내가 제출한 자녀 연결 요청과 처리 상태(`PENDING`, `APPROVED`, `REJECTED`), 거절 사유 조회 |
| `GET` | `/me/children` | 현재 내 계정에 연결된 자녀 목록 조회 |
| `DELETE` | `/me/children/{student_id}` | 해당 자녀와 내 계정의 연결 해제 (성공 시 `204`, 내 자녀가 아니면 `404`) |
| `GET` | `/me/children/{student_id}/attendance?date=YYYY-MM-DD` | 내 자녀의 해당 날짜 결석 여부 조회 (기록이 없으면 `is_absent: false`) |
| `PUT` | `/me/children/{student_id}/attendance` | 내 자녀의 해당 날짜 결석 신청·취소 |
| `GET` | `/me/children/{student_id}/attendances?from=YYYY-MM-DD&to=YYYY-MM-DD` | 내 자녀의 날짜 범위 출결 조회 |
| `PUT` | `/me/children/{student_id}/attendances/range` | 내 자녀의 날짜 범위 결석 신청·취소 (`from`, `to`, `is_absent`) |
| `GET` | `/web/attendances?date=YYYY-MM-DD&status=REQUESTED` | 관리자 결석 신청 조회 및 대기 건수 |
| `POST` | `/web/attendances/{id}/confirm` | 관리자 결석 신청 확인 |
| `POST` | `/web/attendances/{id}/reject` | 관리자 결석 신청 반려 (`reason`: 1~200자) |
| `GET` | `/me/buses` | 내 자녀에게 배정된 실제 버스 목록 조회 |

기존 DB에는 `migrations/attendance_v3.sql`을 적용해야 합니다. 결석 신청은 운행 예정 시각 30분 전까지 수정할 수 있으며, `ATTENDANCE_DEADLINE_MINUTES` 환경 변수로 마감 분을 설정할 수 있습니다.

비밀번호 변경 요청은 `{"current_password":"현재 비밀번호","new_password":"새 비밀번호"}` 형식입니다. 새 비밀번호는 가입과 동일하게 4자 이상, 영문 대문자와 특수문자를 포함해야 합니다. 현재 비밀번호가 틀리면 `401`을 반환합니다.

연결 해제는 보호자-자녀 관계만 제거하며, 학생 정보와 다른 보호자의 연결 및 요청 처리 이력은 유지합니다.

## 출발 예정 시각 및 미출발 알림

기사 앱에서는 **배정된 버스** 카드에 오늘 출발 예정 시각을 표시하고, 관리자는 웹의 **버스·배정** 화면 또는 관리자 앱에서 운행 날짜별 출발 예정 시각을 등록·수정할 수 있습니다. 보호자 홈의 버스 정보에도 예정 시각이 표시됩니다. 운행을 시작할 때 시간표 ID를 함께 전송하며, 서버가 예정 시각부터 5분이 지난 시점에도 출발이 확인되지 않으면 해당 버스의 보호자와 전체 관리자(기사)에게 `NOT_STARTED` 운행 알림과 푸시를 한 번 발송합니다. 도착(종점) 지연 알림은 두지 않으며, 보호자는 운행 시작·종료 알림으로 운행 상황을 확인합니다.

| Method | Path | Description |
|---|---|---|
| `GET` | `/trip-schedules?service_date=YYYY-MM-DD` | 날짜별 출발 예정 시각 조회 |
| `PUT` | `/trip-schedules/{bus_id}` | 출발 예정 시각 생성·수정 (`service_date`, `start_time`). 운행 시작 후에는 수정 불가 |

## 운행 취소와 미하차 확인 종료

운행 시작 직후 취소할 때는 `DELETE /trips/{trip_id}`를 호출합니다. 로그인한 기사의 운행이어야 하며, 사진 처리나 이벤트 기록이 시작된 운행은 취소할 수 없습니다.

미하차 의심(`MISSED`) 운행을 종료할 때는 `POST /trips/{trip_id}/confirm`에 차량 내부 안전 확인(`safety_checked: true`, 시뮬레이션 운행은 `simulation_mode: true`)과 함께 확인 사유 `note`(앱은 `reason`으로 전송해도 동일하게 처리)를 보내야 합니다. 열린 경고는 이 사유와 함께 해결 처리되며, `note`가 없으면 `409`를 반환합니다.

## 자녀 통학 위치 API

자녀(students)에 도로명 주소와 WGS84 위도·경도(소수점 7자리)를 저장합니다. 주소만 보내면 서버가 네이버 클라우드 플랫폼 Geocoding API로 좌표를 조회하고, 네이버 검색 API의 지역 검색 결과도 주소 후보에 함께 포함합니다. Geocoding API에서 좌표를 찾지 못하거나 사용할 수 없을 때는 검색 API 결과 중 입력한 도로명/지번 주소와 정확히 일치하는 결과를 좌표 조회에 사용할 수 있습니다. 지도에서 위치를 보정한 경우에는 주소와 보정한 `latitude`, `longitude`를 함께 보내면 해당 좌표를 사용합니다.

주소와 좌표는 Fernet 인증 암호화로 DB에 암호문으로 저장되고, 본인 또는 관리자 권한 API 응답에서만 복호화됩니다. 서버 환경변수 `LOCATION_ENCRYPTION_KEY`에는 아래 명령으로 생성한 키를 비밀 저장소에 보관해 설정하세요. 키 분실 시 저장된 위치를 복구할 수 없으므로 암호화된 DB 백업과 별도로 안전한 백업이 필요합니다. 키가 누락되거나 형식이 잘못되면 서버 시작이 중단됩니다.

```
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

네이버 API 키는 모두 서버 환경변수로 관리하고 클라이언트에 전달하지 않습니다.

| Method | Path | Description |
|---|---|---|
| `GET` | `/me/location/search?address=` | 주소 자동완성 후보 목록(최대 5개, 2자 이상). 항목: `address`, `road_address`, `jibun_address`, `latitude`, `longitude` |
| `GET` | `/me/children/{student_id}/location` | 내 자녀의 주소와 좌표 |
| `PUT` | `/me/children/{student_id}/location` | 자녀 주소 저장/수정 및 좌표 지오코딩 또는 직접 보정 |
| `DELETE` | `/me/children/{student_id}/location` | 자녀 주소와 좌표 삭제 |
| `GET` | `/web/students/{student_id}/location` | 관리자 전용 자녀 위치 조회 |

`PUT /me/children/{student_id}/location` 요청 예시:

```json
{"address": "서울특별시 중구 세종대로 110"}
```

지도에서 보정한 좌표를 함께 전달할 수도 있습니다:

```json
{"address": "서울특별시 중구 세종대로 110", "latitude": 37.5665, "longitude": 126.9780}
```

주소만 전달할 때 좌표를 찾지 못하면 `422`를 반환하므로 도로명 주소를 입력하세요. 건물명만으로 검색되지 않는 경우 지도에서 선택한 정확한 주소와 위도·경도를 함께 전달할 수 있습니다. 좌표는 위도와 경도를 모두 보내야 합니다.

관리자 전용 API 외에는 다른 사용자의 위치를 반환하지 않습니다. 서버 시작 시 `students`에 `address_encrypted`, `latitude_encrypted`, `longitude_encrypted` 컬럼을 추가하고, 위치가 없는 자녀에게 연결된 첫 보호자(`guardians.id` 최소)의 기존 `users` 암호문 좌표·주소를 복사한 뒤 `users`의 `latitude`, `longitude`, `latitude_encrypted`, `longitude_encrypted` 컬럼을 삭제합니다. 삭제는 되돌릴 수 없으므로 배포 전에 DB를 백업하세요. `users.address_encrypted`는 유지하지만 더 이상 사용하지 않습니다.

기사 앱의 탑승 지도는 로그인한 기사가 본인 운행에 대해 `GET /trips/{trip_id}/boarding-map`을 호출합니다. 응답은 해당 버스 배정 학생과 연결된 자녀 위치를 포함하며, 서버에서 복호화한 주소·좌표를 반환합니다. 위치가 없는 학생은 `location`이 `null`입니다. 다른 기사의 운행 ID를 지정하면 `404`를 반환합니다. 앱은 각 학생의 `location`에 위도·경도가 모두 있을 때 지도 마커로 표시해야 합니다.

## 미하차 긴급 푸시 알림

미하차 긴급 경고는 운행 기사와 해당 버스에 배정된 학생의 보호자에게 알림 내역을 저장하고 FCM 푸시도 발송합니다. 앱은 로그인 후 FCM 토큰을 등록하며 로그아웃하면 서버에서 토큰을 해제합니다.

서버에는 `firebase-admin`을 설치하고, 앱의 Firebase 프로젝트(Android 설정은 앱의 `android/app/google-services.json`에 지정된 프로젝트)와 같은 프로젝트의 서비스 계정 JSON 파일을 사용하세요. 경로는 `FIREBASE_SERVICE_ACCOUNT_FILE` 또는 `GOOGLE_APPLICATION_CREDENTIALS` 환경변수로 지정합니다(지정하지 않으면 `services/notifier.py`에 적힌 개발 PC 경로를 사용). 서비스 계정 파일은 저장소에 넣지 말고 비밀 파일/환경 설정으로 관리해야 합니다. FCM 발송이 실패해도 알림 내역 저장은 유지되며 서버 로그에 원인이 기록됩니다.

토큰은 기기별 `push_tokens` 테이블에 저장하며, 기존 `users.push_token` 값도 전송 대상으로 유지합니다. 한 계정의 여러 기기에서 알림을 받을 수 있고 같은 기기의 토큰이 다른 계정에 등록되면 해당 계정으로 이동합니다. 로그아웃은 `DELETE /me/push-token`에 `{"push_token":"<현재 기기의 FCM 토큰>"}`을 보내 해당 기기만 해제합니다. 앱에서 토큰을 가져오지 못하면 다른 기기 알림을 보호하기 위해 서버 해제를 건너뜁니다. 서버의 `FCM_ANDROID_CHANNEL_ID`와 앱의 `safety_alerts` 알림 채널 ID가 같아야 합니다.

| Method | Path | Description |
|---|---|---|
| `PUT` | `/me/push-token` | 로그인한 기기의 FCM 토큰 등록 |
| `DELETE` | `/me/push-token` | 로그인한 기기의 FCM 토큰 해제 |

Android 13 이상에서는 앱의 알림 권한을 허용해야 합니다. iOS에서는 Firebase 프로젝트에 APNs 인증 키를 등록하고 앱의 iOS Firebase 설정 파일을 추가해야 푸시를 받을 수 있습니다.

## 계정 삭제 유예 API

학부모가 현재 비밀번호 또는 연결된 Google 계정으로 재인증해 삭제를 요청하면 30일 동안 삭제를 유예한 뒤, 서버 백그라운드 작업이 계정 및 계정 소유의 보호자 연결, 자녀 요청, 소셜 계정, 알림과 주소 정보를 실제 삭제합니다. 요청 직후 앱에서 로그아웃됩니다. 유예 중 로그인하면 삭제 예정일까지 남은 일수를 확인할 수 있으며, 비밀번호 또는 연결된 Google 계정으로 재인증해 삭제를 취소하거나 로그아웃할 수 있습니다. 유예 기간에는 일반 API 접근이 차단됩니다. 관리자는 이 API로 삭제할 수 없습니다.

| Method | Path | Description |
|---|---|---|
| `POST` | `/me/account-deletion` | `current_password` 또는 연결된 Google 계정의 `google_id_token`으로 재인증 후 학부모 계정 삭제 예약 (응답에 요청 시각·삭제 예정 시각 포함, `202`) |
| `POST` | `/auth/account-deletion/status` | 이메일과 비밀번호 또는 Google ID 토큰으로 삭제 유예 상태와 남은 일수 확인 |
| `POST` | `/auth/account-deletion/cancel` | 같은 방식으로 재인증 후 유예 기간 내 삭제 예약 취소 및 새 액세스 토큰 발급 |

삭제 예약 요청 예시:

```json
{"current_password": "<password>"}
```

상태·취소 요청 예시 (비밀번호 계정):

```json
{"email": "parent@example.com", "password": "<password>"}
```

구글 계정은 `password` 대신 검증된 `google_id_token`을 보내면 됩니다.

## AI 연계: LLM 요약 · Agent 경고 판단 · RAG 대응 수칙 · 챗봇 (Gemini 무료 API)

road_hazard 프로젝트의 `services/llm_service.py`, `agent_service.py`, `rag_service.py` 구조(`USE_REAL_AI` 스위치, mock/real 함수, `_normalize`, 실패 시 폴백)를 옮기고 OpenAI 호출을 Gemini로 바꿨습니다. `GEMINI_API_KEY`가 없으면 세 기능 모두 mock(템플릿·글자 매칭)으로 동작하므로 키 없이도 서버가 정상 동작합니다.

| 기능 | 파일 | 동작 |
|---|---|---|
| AI ② LLM 요약 | `services/llm_service.py` | 경고 생성 시 템플릿 요약을 저장하고, 응답 후 백그라운드에서 Gemini 문장(감지 위치·확인할 곳)을 덧붙여 `alert.llm_summary` 갱신 + SSE 전송. 첫 줄(인원 수)은 서버가 고정해 LLM이 숫자를 바꿀 수 없음 |
| AI ③ RAG 대응 수칙 | `services/rag_service.py`, `routers/rag.py`, `knowledge/` (.md·.txt·.pdf) | 법령 PDF·기관 안전 매뉴얼 PDF·대응 수칙·앱 안내 문서를 Gemini 임베딩으로 검색해 근거(파일·쪽 번호)와 함께 답변 |
| AI ④ Agent 경고 판단 | `services/agent_service.py` | `judge()` 결과에 사진 밝기를 더해 최종 등급·재촬영 요청·기사 안내문 결정. 등급은 올리기만 함(어두운 종점 사진은 '정상' 대신 '확인 필요') |
| 챗봇 | `services/chat_service.py`, `routers/chat.py` | 같은 지식 문서로 여러 턴 대화, 사용자 상황(진행 중 경고·오늘 결석) 반영, '아이가 안 내렸다' 같은 급한 표현에는 119·즉시 확인 안내를 고정으로 먼저 붙임 |
| 공용 클라이언트 | `services/gemini_client.py` | `google-genai` SDK 호출, JSON 응답 파싱, 임베딩 |

| Method | Path | Description |
|---|---|---|
| `GET` | `/chat/hello` | 채팅 화면 첫 인사 `{reply, suggestions}` (역할별) |
| `POST` | `/chat` | `{"message": "...", "history": [{"role": "user"\|"bot", "text": "..."}], "alert_id": 12}` → `{reply, suggestions, sources, mode}`. history는 앱이 보관해 매번 보냄(서버 저장 안 함). `/rag/ask`와 분당 한도 공유 |
| `POST` | `/rag/ask` | `{"question": "...", "alert_id": 12}` → `{"answer", "sources", "mode"}`. 관리자·학부모 사용(학부모는 내 아이 버스 긴급 경고만). `question` 없이 `alert_id`만 보내면 경고 등급에 맞는 대응 수칙을 답함. 사용자당 분당 10회 |

`POST /trips/{id}/dropoff-photo` 응답에 `guidance`(기사 행동 안내), `retake_recommended`, `agent_reasons`가, `POST /trips/{id}/boarding-photo` 응답에 `photo_dark`, `guidance`가 추가되었습니다. `GET /health`의 `ai` 항목에서 AI 설정 상태를 확인할 수 있습니다.

`.env` 설정:

```
GEMINI_API_KEY=발급받은_키           # Google AI Studio. 저장소에 올리지 말 것
GEMINI_MODEL=gemini-3.5-flash-lite   # 무료 등급 모델
GEMINI_EMBED_MODEL=gemini-embedding-2
LLM_ENABLED=true
LLM_SEND_IMAGE=false                 # true면 종점 사진도 Gemini에 보냄 (무료 등급은 학습에 쓰일 수 있어 실제 아동 사진에는 false)
RAG_ENABLED=true
CHAT_ENABLED=true                    # false면 챗봇이 문서 발췌로만 답함
AGENT_LLM_GUIDANCE=false             # true면 기사 안내문을 Gemini가 대응 수칙 근거로 작성
AGENT_ESCALATE_DARK=true
DARK_BRIGHTNESS_THRESHOLD=60
```

### 지식 문서 (`knowledge/`)

| 형식 | 자르는 단위 | 출처 표시 |
|---|---|---|
| `.md` / `.txt` | `## 소제목` (첫 줄 `# 제목`, `출처:` 줄) | 파일 이름 · 출처 줄 |
| 법령 `.pdf` | `제○조(…)` 조문 단위 (자동 인식) | `도로교통법.pdf p.12` |
| 매뉴얼 `.pdf` | `❑`·`❍` 제목 단위 (예: `운전자 매뉴얼 · 하차 시`), 제목 기호가 없으면 쪽 단위. 그림만 있는 쪽은 건너뜀 | `기관_안전매뉴얼.pdf p.3` |

- 하위 폴더도 읽습니다. `_`로 시작하는 파일과 `README`는 읽지 않습니다.
- 법령 PDF: 국가법령정보센터(law.go.kr)에서 법령을 열고 **저장 → PDF**. 전체 법령은 조각이 많아 처음 임베딩이 오래 걸리므로, 필요하면 해당 조문만 선택해 저장하세요.
- `.hwp` 매뉴얼은 한글에서 **PDF로 저장**해서 넣습니다. 스캔 이미지 PDF는 글자를 읽지 못하므로 OCR 후 넣어야 합니다.
- 문서를 추가·수정하면 다음 검색 때 **바뀐 조각만** 새로 임베딩해 `rag_index.json`에 더합니다 (무료 한도 초과(429) 시 자동 재시도).
- `02_미하차_대응수칙.md`는 공식 매뉴얼 근거와 서비스 운영 규칙을 항목마다 `[근거: …]`로 구분합니다. 기관 자체 매뉴얼이 있으면 그 PDF를 넣고, 충돌하는 항목은 기관 매뉴얼에 맞춰 고치세요.