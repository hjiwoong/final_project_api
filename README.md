## 사람 검출 모델

실제 추론은 `.env`의 `YOLO_MODELS`에 설정된 세 모델을 모두 사용하며, 버스 승객 검출 모델은 `weights/bus_person_k2_more_26s_1105_0855.pt`를 사용합니다. 각 모델의 사람 검출 결과를 합친 뒤 겹치는 박스를 억제해 중복 인원을 줄입니다. 세 모델 중 하나라도 로드할 수 없으면 일부 모델만으로 잘못된 결과를 내지 않고 추론을 실패 처리합니다. 모델 경로는 쉼표로 구분해 변경할 수 있으며, 단일 모델 호환 설정은 `YOLO_MODEL`을 사용하세요. `DETECTOR_MODE=real`에서 실제 추론을 수행하고, 시연용 목업이 필요할 때만 `DETECTOR_MODE=mock`으로 변경하세요. 신규 모델의 `passenger` 클래스와 COCO 모델의 `person` 클래스를 모두 사람 검출로 처리합니다.

## 학부모 자녀 연결 API

모든 학부모 API는 로그인 후 발급받은 액세스 토큰을 Authorization 헤더에 포함해야 합니다.
계정 상세 정보에는 최초 생성 경로(`APP`, `GOOGLE`, `ADMIN`, `UNKNOWN`)를 포함합니다. 이메일 계정에 Google 로그인을 나중에 연결해도 최초 생성 경로는 변경되지 않으며, 기존 계정은 `UNKNOWN`으로 마이그레이션됩니다.

| Method | Path | Description |
|---|---|---|
| `PUT` | `/me/password` | 로그인 사용자의 현재 비밀번호를 검증한 뒤 새 비밀번호로 변경 |

비밀번호 변경 요청은 `{"current_password":"현재 비밀번호","new_password":"새 비밀번호"}` 형식입니다. 새 비밀번호는 가입과 동일하게 4자 이상, 영문 대문자와 특수문자를 포함해야 합니다. 현재 비밀번호가 틀리면 `401`을 반환합니다.

| Method | Path | Description |
|---|---|---|
| `GET` | `/me/child-requests` | 내가 제출한 자녀 연결 요청과 처리 상태(`PENDING`, `APPROVED`, `REJECTED`), 거절 사유 조회 |
| `GET` | `/me/children` | 현재 내 계정에 연결된 자녀 목록 조회 |

| `GET` | `/me/children/{student_id}/attendance?date=YYYY-MM-DD` | 내 자녀의 해당 날짜 결석 여부 조회 (기록이 없으면 `is_absent: false`) |
| `PUT` | `/me/children/{student_id}/attendance` | 내 자녀의 해당 날짜 결석 여부 저장 |

| `GET` | `/me/buses` | 내 자녀에게 배정된 실제 버스 목록 조회 |
| `DELETE` | `/me/children/{student_id}` | 해당 자녀와 내 계정의 연결 해제 (성공 시 `204`, 내 자녀가 아니면 `404`) |

연결 해제는 보호자-자녀 관계만 제거하며, 학생 정보와 다른 보호자의 연결 및 요청 처리 이력은 유지합니다.

## 보호자 통학 위치 API

보호자 계정에 도로명 주소와 WGS84 위도·경도(소수점 7자리)를 저장합니다. 주소만 보내면 서버가 네이버 클라우드 플랫폼 Geocoding API로 좌표를 조회합니다. 지도에서 위치를 보정한 경우에는 주소와 보정한 `latitude`, `longitude`를 함께 보내면 해당 좌표를 사용합니다. 주소와 좌표는 Fernet 인증 암호화로 DB에 암호문으로 저장되고, 본인 또는 관리자 권한 API 응답에서만 복호화됩니다. 서버 환경변수 `LOCATION_ENCRYPTION_KEY`에는 `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` 명령으로 생성한 키를 비밀 저장소에 보관해 설정하세요. 키 분실 시 저장된 위치를 복구할 수 없으므로 암호화된 DB 백업과 별도로 안전한 백업이 필요합니다. 키가 누락되거나 형식이 잘못되면 서버 시작이 중단됩니다. 네이버 API 키는 서버 환경변수 `NAVER_MAPS_CLIENT_ID`, `NAVER_MAPS_CLIENT_SECRET`에 설정하며 클라이언트에 전달하지 않습니다.

| Method | Path | Description |
|---|---|---|
| `GET` | `/me/location` | 로그인한 학부모 본인의 주소와 좌표 |

| `GET` | `/me/location/search?address=...` | 네이버 Geocoding으로 주소 좌표 검색 (네이버 키 미설정 시 `503`) |

| `PUT` | `/me/location` | 본인 주소 저장/수정 및 좌표 지오코딩 또는 직접 보정 |
| `DELETE` | `/me/location` | 본인 주소와 좌표 삭제 |
| `GET` | `/web/users/{user_id}/location` | 관리자 전용 학부모 위치 조회 |

`PUT /me/location` 요청 예시:

```json
{"address": "서울특별시 중구 세종대로 110"}
```

지도에서 보정한 좌표를 함께 전달할 수도 있습니다:

```json
{"address": "서울특별시 중구 세종대로 110", "latitude": 37.5665, "longitude": 126.9780}
```

관리자 전용 API 외에는 다른 사용자의 위치를 반환하지 않습니다. 기존 평문 위치 컬럼은 서버 시작 시 암호문 컬럼으로 이전하고 평문 값을 비웁니다. 위치 데이터가 이미 있는 기존 DB는 이 마이그레이션 전에 `LOCATION_ENCRYPTION_KEY`를 설정해야 합니다.

기사 앱의 탑승 지도는 로그인한 기사가 본인 운행에 대해 `GET /trips/{trip_id}/boarding-map`을 호출합니다. 응답은 해당 버스 배정 학생과 연결된 보호자 위치를 포함하며, 서버에서 복호화한 주소·좌표를 반환합니다. 좌표가 없는 보호자는 지도 위치 목록에서 제외됩니다. 다른 기사의 운행 ID를 지정하면 `404`를 반환합니다. 앱은 각 학생의 `guardian_locations`에서 위도·경도가 모두 있는 항목을 지도 마커로 표시해야 합니다.


운행 시작 직후 취소할 때는 `DELETE /trips/{trip_id}`를 호출합니다. 로그인한 기사의 운행이어야 하며, 사진 처리나 이벤트 기록이 시작된 운행은 취소할 수 없습니다.


## 미하차 긴급 푸시 알림

미하차 긴급 경고는 운행 기사와 해당 버스에 배정된 학생의 보호자에게 알림 내역을 저장하고 FCM 푸시도 발송합니다. 앱은 로그인 후 FCM 토큰을 등록하며 로그아웃하면 서버에서 토큰을 해제합니다. 서버에는 `firebase-admin`을 설치하고, 앱의 Firebase 프로젝트(현재 Android 설정은 `android/app/google-services.json`에 지정된 프로젝트)와 같은 프로젝트의 서비스 계정 JSON 파일을 사용하세요. 기본 경로는 `D:\secrets\final-project-b66de-firebase-adminsdk-fbsvc-c2cb0dc867.json`이며, `FIREBASE_SERVICE_ACCOUNT_FILE` 또는 `GOOGLE_APPLICATION_CREDENTIALS` 환경변수를 설정하면 해당 경로로 덮어쓸 수 있습니다. 서비스 계정 파일은 저장소에 넣지 말고 비밀 파일/환경 설정으로 관리해야 합니다. FCM 발송이 실패해도 알림 내역 저장은 유지되며 서버 로그에 원인이 기록됩니다.


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

구글 계정은 `password` 대신 검증된 `google_id_token`을 보내면 됩니다. 삭제 예약은 서버의 정기 작업에서 처리되므로 배포 환경에서 최소 한 개의 API 인스턴스를 계속 실행해야 합니다.

## AI 연계: LLM 요약 · Agent 경고 판단 · RAG 대응 수칙 · 챗봇 (Gemini 무료 API)

road_hazard 프로젝트의 `services/llm_service.py`, `agent_service.py`, `rag_service.py` 구조(`USE_REAL_AI` 스위치, mock/real 함수, `_normalize`, 실패 시 폴백)를 그대로 옮기고 OpenAI 호출을 Gemini로 바꿨습니다. `GEMINI_API_KEY`가 없으면 세 기능 모두 mock(템플릿·글자 매칭)으로 동작하므로 키 없이도 서버가 정상 동작합니다.

| 기능 | 파일 | 동작 |
|---|---|---|
| AI ② LLM 요약 | `services/llm_service.py` | 경고 생성 시 템플릿 요약을 저장하고, 응답 후 백그라운드에서 Gemini 문장(감지 위치·확인할 곳)을 덧붙여 `alert.llm_summary` 갱신 + SSE 전송. 첫 줄(인원 수)은 서버가 고정해 LLM이 숫자를 바꿀 수 없음 |

| AI ④ Agent 경고 판단 | `services/agent_service.py` | `judge()` 결과에 사진 밝기를 더해 최종 등급·재촬영 요청·기사 안내문 결정. 등급은 올리기만 함(어두운 종점 사진은 '정상' 대신 '확인 필요') |
| AI ③ RAG 대응 수칙 | `services/rag_service.py`, `routers/rag.py`, `knowledge/` (.md·.txt·.pdf) | 법령 PDF·기관 안전 매뉴얼 PDF·대응 수칙·앱 안내 문서를 Gemini 임베딩으로 검색해 근거(파일·쪽 번호)와 함께 답변 |

| 챗봇 | `services/chat_service.py`, `routers/chat.py` | road_hazard 고객센터 챗봇 구조. 같은 지식 문서로 여러 턴 대화, 사용자 상황(진행 중 경고·오늘 결석) 반영, '아이가 안 내렸다' 같은 급한 표현에는 119·즉시 확인 안내를 고정으로 먼저 붙임 |
| 공용 클라이언트 | `services/gemini_client.py` | `google-genai` SDK 호출, JSON 응답 파싱, 임베딩 |

| Method | Path | Description |
|---|---|---|
| `GET` | `/chat/hello` | 채팅 화면 첫 인사 `{reply, suggestions}` (역할별) |
| `POST` | `/chat` | `{"message": "...", "history": [{"role": "user"|"bot", "text": "..."}], "alert_id": 12}` → `{reply, suggestions, sources, mode}`. history 는 앱이 보관해 매번 보냄(서버 저장 안 함). `/rag/ask` 와 분당 한도 공유 |
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

