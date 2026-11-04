 
## 학부모 자녀 연결 API

모든 학부모 API는 로그인 후 발급받은 액세스 토큰을 Authorization 헤더에 포함해야 합니다.

| Method | Path | Description |
|---|---|---|
| `GET` | `/me/child-requests` | 내가 제출한 자녀 연결 요청과 처리 상태(`PENDING`, `APPROVED`, `REJECTED`), 거절 사유 조회 |
| `GET` | `/me/children` | 현재 내 계정에 연결된 자녀 목록 조회 |
| `GET` | `/me/buses` | 내 자녀에게 배정된 실제 버스 목록 조회 |
| `DELETE` | `/me/children/{student_id}` | 해당 자녀와 내 계정의 연결 해제 (성공 시 `204`, 내 자녀가 아니면 `404`) |

연결 해제는 보호자-자녀 관계만 제거하며, 학생 정보와 다른 보호자의 연결 및 요청 처리 이력은 유지합니다.

## 보호자 통학 위치 API

보호자 계정에 도로명 주소와 WGS84 위도·경도(소수점 7자리)를 저장합니다. 주소만 보내면 서버가 네이버 클라우드 플랫폼 Geocoding API로 좌표를 조회합니다. 지도에서 위치를 보정한 경우에는 주소와 보정한 `latitude`, `longitude`를 함께 보내면 해당 좌표를 사용합니다. 주소와 좌표는 Fernet 인증 암호화로 DB에 암호문으로 저장되고, 본인 또는 관리자 권한 API 응답에서만 복호화됩니다. 서버 환경변수 `LOCATION_ENCRYPTION_KEY`에는 `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` 명령으로 생성한 키를 비밀 저장소에 보관해 설정하세요. 키 분실 시 저장된 위치를 복구할 수 없으므로 암호화된 DB 백업과 별도로 안전한 백업이 필요합니다. 키가 누락되거나 형식이 잘못되면 서버 시작이 중단됩니다. 네이버 API 키는 서버 환경변수 `NAVER_MAPS_CLIENT_ID`, `NAVER_MAPS_CLIENT_SECRET`에 설정하며 클라이언트에 전달하지 않습니다.

| Method | Path | Description |
|---|---|---|
| `GET` | `/me/location` | 로그인한 학부모 본인의 주소와 좌표 |
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

## 미하차 긴급 푸시 알림

미하차 긴급 경고는 운행 기사와 해당 버스에 배정된 학생의 보호자에게 알림 내역을 저장하고 FCM 푸시도 발송합니다. 앱은 로그인 후 FCM 토큰을 등록하며 로그아웃하면 서버에서 토큰을 해제합니다. 서버에는 `firebase-admin`을 설치하고, 앱의 Firebase 프로젝트(현재 Android 설정은 `android/app/google-services.json`에 지정된 프로젝트)와 같은 프로젝트의 서비스 계정 JSON 파일을 사용하세요. 기본 경로는 `D:\secrets\final-project-b66de-firebase-adminsdk-fbsvc-c2cb0dc867.json`이며, `FIREBASE_SERVICE_ACCOUNT_FILE` 또는 `GOOGLE_APPLICATION_CREDENTIALS` 환경변수를 설정하면 해당 경로로 덮어쓸 수 있습니다. 서비스 계정 파일은 저장소에 넣지 말고 비밀 파일/환경 설정으로 관리해야 합니다. FCM 발송이 실패해도 알림 내역 저장은 유지되며 서버 로그에 원인이 기록됩니다.

| Method | Path | Description |
|---|---|---|
| `PUT` | `/me/push-token` | 로그인한 기기의 FCM 토큰 등록 |
| `DELETE` | `/me/push-token` | 로그인한 기기의 FCM 토큰 해제 |

Android 13 이상에서는 앱의 알림 권한을 허용해야 합니다. iOS에서는 Firebase 프로젝트에 APNs 인증 키를 등록하고 앱의 iOS Firebase 설정 파일을 추가해야 푸시를 받을 수 있습니다.

## 계정 삭제 유예 API

학부모가 삭제를 요청하면 30일 동안 삭제를 유예합니다. 이 기간에는 로그인과 기존 액세스 토큰 사용이 차단되며, 비밀번호 또는 연결된 구글 계정으로 재인증해 상태를 확인하거나 삭제 예약을 취소할 수 있습니다. 유예가 끝나면 서버 백그라운드 작업이 계정 및 계정 소유의 보호자 연결, 자녀 요청, 소셜 계정, 알림과 주소 정보를 실제 삭제합니다. 관리자는 이 API로 삭제할 수 없습니다.

| Method | Path | Description |
|---|---|---|
| `POST` | `/me/account-deletion` | 로그인한 학부모의 삭제 예약 (응답에 요청 시각·삭제 예정 시각 포함, `202`) |
| `POST` | `/auth/account-deletion/status` | 이메일과 비밀번호 또는 구글 ID 토큰으로 본인 삭제 유예 상태 확인 |
| `POST` | `/auth/account-deletion/cancel` | 같은 방식으로 재인증 후 유예 기간 내 삭제 예약 취소 및 새 액세스 토큰 발급 |

상태·취소 요청 예시 (비밀번호 계정):

```json
{"email": "parent@example.com", "password": "<password>"}
```

구글 계정은 `password` 대신 검증된 `google_id_token`을 보내면 됩니다. 삭제 예약은 서버의 정기 작업에서 처리되므로 배포 환경에서 최소 한 개의 API 인스턴스를 계속 실행해야 합니다.
