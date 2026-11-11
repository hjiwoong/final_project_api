import os

import requests
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from core.security import require_admin, require_parent
from database.db_connection import get_db
from models.models import User, UserRole
from services.location_crypto import (
    decrypt_coordinate_value,
    decrypt_location_value,
    encrypt_location_value,
)

router = APIRouter(tags=["보호자 위치"])


class LocationUpdate(BaseModel):
    address: str = Field(min_length=1, max_length=255)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)

    @model_validator(mode="after")
    def coordinates_must_be_provided_together(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("위도와 경도를 함께 입력해야 합니다")
        if not self.address.strip():
            raise ValueError("주소를 입력하세요")
        return self


def _location_response(user: User) -> dict:
    return {
        "address": (
            decrypt_location_value(user.address_encrypted)
            if user.address_encrypted is not None
            else None
        ),
        "latitude": (
            decrypt_coordinate_value(user.latitude_encrypted)
            if user.latitude_encrypted is not None
            else None
        ),
        "longitude": (
            decrypt_coordinate_value(user.longitude_encrypted)
            if user.longitude_encrypted is not None
            else None
        ),
    }


def _geocode(address: str) -> tuple[float, float]:
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
            params={"query": address},
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

    if not isinstance(payload, dict):
        raise HTTPException(502, "주소 좌표 검색 서비스의 응답 형식이 올바르지 않습니다")
    addresses = payload.get("addresses")
    if not isinstance(addresses, list) or not addresses:
        raise HTTPException(422, "입력한 주소의 좌표를 찾을 수 없습니다")
    try:
        longitude = float(addresses[0]["x"])
        latitude = float(addresses[0]["y"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(502, "주소 좌표 검색 서비스의 응답 형식이 올바르지 않습니다") from exc
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise HTTPException(502, "주소 좌표 검색 서비스가 유효하지 않은 좌표를 반환했습니다")
    return latitude, longitude


@router.get("/me/location")
def get_my_location(me: User = Depends(require_parent)):
    return _location_response(me)


@router.get("/me/location/search")
def search_my_location(
    address: str = Query(..., min_length=1, max_length=255),
    me: User = Depends(require_parent),
):
    normalized_address = address.strip()
    if not normalized_address:
        raise HTTPException(422, "주소를 입력하세요")
    latitude, longitude = _geocode(normalized_address)
    return {
        "address": normalized_address,
        "latitude": latitude,
        "longitude": longitude,
    }


@router.put("/me/location")
def update_my_location(
    body: LocationUpdate,
    me: User = Depends(require_parent),
    db: Session = Depends(get_db),
):
    address = body.address.strip()
    latitude, longitude = (
        (body.latitude, body.longitude)
        if body.latitude is not None and body.longitude is not None
        else _geocode(address)
    )
    me.address_encrypted = encrypt_location_value(address)
    me.latitude_encrypted = encrypt_location_value(f"{latitude:.7f}")
    me.longitude_encrypted = encrypt_location_value(f"{longitude:.7f}")
    db.commit()
    db.refresh(me)
    return _location_response(me)


@router.delete("/me/location", status_code=204)
def delete_my_location(
    me: User = Depends(require_parent),
    db: Session = Depends(get_db),
):
    me.address_encrypted = None
    me.latitude_encrypted = None
    me.longitude_encrypted = None
    db.commit()


@router.get("/web/users/{user_id}/location", dependencies=[Depends(require_admin)])
def get_user_location(user_id: int, db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if user is None or user.role != UserRole.PARENT:
        raise HTTPException(404, "보호자 계정을 찾을 수 없습니다")
    return _location_response(user)
