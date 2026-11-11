import os
import re

import requests
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from core.security import require_admin, require_parent
from database.db_connection import get_db
from models.models import Guardian, Student, User
from services.location_crypto import (
    decrypt_coordinate_value,
    decrypt_location_value,
    encrypt_location_value,
)

router = APIRouter(tags=["자녀 위치"])

SEARCH_CANDIDATE_LIMIT = 5


class LocationUpdate(BaseModel):
    address: str = Field(min_length=1, max_length=255)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)

    @model_validator(mode="after")
    def validate_body(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("위도와 경도를 함께 입력해야 합니다")
        if not self.address.strip():
            raise ValueError("주소를 입력하세요")
        return self


def student_location(student: Student) -> dict | None:
    """자녀의 주소·좌표 복호화 결과. 등록된 값이 없으면 None."""
    if (
        student.address_encrypted is None
        and student.latitude_encrypted is None
        and student.longitude_encrypted is None
    ):
        return None
    return {
        "address": (
            decrypt_location_value(student.address_encrypted)
            if student.address_encrypted is not None
            else None
        ),
        "latitude": (
            decrypt_coordinate_value(student.latitude_encrypted)
            if student.latitude_encrypted is not None
            else None
        ),
        "longitude": (
            decrypt_coordinate_value(student.longitude_encrypted)
            if student.longitude_encrypted is not None
            else None
        ),
    }


def _location_response(student: Student) -> dict:
    return student_location(student) or {
        "address": None,
        "latitude": None,
        "longitude": None,
    }


def _geocode_candidates(query: str, count: int) -> list[dict]:
    client_id = os.getenv("NAVER_MAPS_CLIENT_ID")
    client_secret = os.getenv("NAVER_MAPS_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise HTTPException(
            503,
            "주소 검색 서비스가 설정되지 않았습니다 (NAVER_MAPS_CLIENT_ID/SECRET 확인)",
        )
    try:
        response = requests.get(
            "https://maps.apigw.ntruss.com/map-geocode/v2/geocode",
            params={"query": query, "count": count},
            headers={
                "X-NCP-APIGW-API-KEY-ID": client_id,
                "X-NCP-APIGW-API-KEY": client_secret,
            },
            timeout=5,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise HTTPException(502, "주소 좌표 검색 서비스에 연결할 수 없습니다") from exc
    except ValueError as exc:
        raise HTTPException(502, "주소 좌표 검색 서비스의 응답을 읽을 수 없습니다") from exc

    addresses = payload.get("addresses") if isinstance(payload, dict) else None
    if not isinstance(addresses, list):
        raise HTTPException(502, "주소 좌표 검색 서비스의 응답 형식이 올바르지 않습니다")

    candidates = []
    for item in addresses:
        try:
            longitude = float(item["x"])
            latitude = float(item["y"])
        except (KeyError, TypeError, ValueError):
            continue
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            continue
        road_address = item.get("roadAddress") or None
        jibun_address = item.get("jibunAddress") or None
        address = road_address or jibun_address
        if not address:
            continue
        candidates.append({
            "address": address,
            "road_address": road_address,
            "jibun_address": jibun_address,
            "latitude": latitude,
            "longitude": longitude,
        })
    return candidates


def _place_candidates(query: str, count: int) -> list[dict]:
    """장소명(예: 광운대학교) 검색. 네이버 개발자센터 '지역 검색' API 사용, 키가 없으면 건너뜀."""
    client_id = os.getenv("NAVER_SEARCH_CLIENT_ID")
    client_secret = os.getenv("NAVER_SEARCH_CLIENT_SECRET")
    if not client_id or not client_secret:
        return []
    try:
        response = requests.get(
            "https://openapi.naver.com/v1/search/local.json",
            params={"query": query, "display": count, "sort": "random"},
            headers={
                "X-Naver-Client-Id": client_id,
                "X-Naver-Client-Secret": client_secret,
            },
            timeout=5,
        )
        response.raise_for_status()
        items = response.json().get("items", [])
    except (requests.RequestException, ValueError, AttributeError):
        return []

    results = []
    for item in items:
        name = re.sub(r"<[^>]+>", "", item.get("title") or "").strip()
        road_address = item.get("roadAddress") or None
        jibun_address = item.get("address") or None
        address = road_address or jibun_address
        if not name or not address:
            continue
        try:
            # mapx/mapy 는 WGS84 좌표 x 10^7 정수 문자열
            longitude = int(item["mapx"]) / 1e7
            latitude = int(item["mapy"]) / 1e7
        except (KeyError, TypeError, ValueError):
            continue
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            continue
        results.append({
            "name": name,
            "address": address,
            "road_address": road_address,
            "jibun_address": jibun_address,
            "latitude": latitude,
            "longitude": longitude,
        })
    return results


def _geocode(address: str) -> tuple[float, float]:
    candidates = _geocode_candidates(address, 1)
    if not candidates:
        raise HTTPException(
            422,
            "입력한 주소의 좌표를 찾을 수 없습니다. 건물명만 입력하지 말고 "
            "도로명 또는 지번 주소(예: 서울특별시 중구 세종대로 110)를 입력하거나 "
            "지도에서 위치를 선택해 주세요",
        )
    return candidates[0]["latitude"], candidates[0]["longitude"]


def _get_my_child(db: Session, me: User, student_id: int) -> Student:
    student = db.get(Student, student_id)
    if student is None:
        raise HTTPException(404, "학생을 찾을 수 없습니다")
    is_mine = (
        db.query(Guardian.id)
        .filter(Guardian.user_id == me.id, Guardian.student_id == student_id)
        .first()
    )
    if not is_mine:
        raise HTTPException(403, "내 자녀가 아닙니다")
    return student


@router.get("/me/location/search")
def search_address(
    address: str = Query(min_length=2, max_length=255),
    me: User = Depends(require_parent),
):
    """입력 중인 주소의 후보 목록 (자동완성용). 결과가 없으면 빈 목록."""
    query = address.strip()
    if len(query) < 2:
        return []
    places = _place_candidates(query, SEARCH_CANDIDATE_LIMIT)
    try:
        addresses = _geocode_candidates(query, SEARCH_CANDIDATE_LIMIT)
    except HTTPException:
        if not places:
            raise
        addresses = []
    results, seen = [], set()
    for item in [*places, *addresses]:
        key = (round(item["latitude"], 4), round(item["longitude"], 4))
        if key in seen:
            continue
        seen.add(key)
        results.append(item)
    return results[:SEARCH_CANDIDATE_LIMIT * 2]


@router.get("/me/children/{student_id}/location")
def get_child_location(
    student_id: int,
    me: User = Depends(require_parent),
    db: Session = Depends(get_db),
):
    return _location_response(_get_my_child(db, me, student_id))


@router.put("/me/children/{student_id}/location")
def update_child_location(
    student_id: int,
    body: LocationUpdate,
    me: User = Depends(require_parent),
    db: Session = Depends(get_db),
):
    student = _get_my_child(db, me, student_id)
    address = body.address.strip()
    latitude, longitude = (
        (body.latitude, body.longitude)
        if body.latitude is not None and body.longitude is not None
        else _geocode(address)
    )
    student.address_encrypted = encrypt_location_value(address)
    student.latitude_encrypted = encrypt_location_value(f"{latitude:.7f}")
    student.longitude_encrypted = encrypt_location_value(f"{longitude:.7f}")
    db.commit()
    db.refresh(student)
    return _location_response(student)


@router.delete("/me/children/{student_id}/location", status_code=204)
def delete_child_location(
    student_id: int,
    me: User = Depends(require_parent),
    db: Session = Depends(get_db),
):
    student = _get_my_child(db, me, student_id)
    student.address_encrypted = None
    student.latitude_encrypted = None
    student.longitude_encrypted = None
    db.commit()


@router.get("/web/students/{student_id}/location", dependencies=[Depends(require_admin)])
def get_student_location(student_id: int, db: Session = Depends(get_db)):
    student = db.get(Student, student_id)
    if student is None:
        raise HTTPException(404, "학생을 찾을 수 없습니다")
    return _location_response(student)
