"""Pydantic request/response models (the API contract)."""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


class MeResponse(BaseModel):
    user_id: str
    clinic_id: str
    role: str
    clinic_name: str | None = None


class ClinicBootstrapRequest(BaseModel):
    clinic_name: str = Field(min_length=1)
    user_name: str = Field(min_length=1)


class ClinicBootstrapResponse(BaseModel):
    clinic_id: str
    user_id: str


class PatientCreateRequest(BaseModel):
    name: str = Field(min_length=1)
    dob: date | None = None
    gender: str | None = None
    phone: str | None = None
    address: str | None = None
    pincode: str | None = None
    city: str | None = None
    state: str | None = None


class PatientUpdateRequest(BaseModel):
    name: str | None = None
    dob: date | None = None
    gender: str | None = None
    phone: str | None = None


class PatientResponse(BaseModel):
    id: str
    name: str
    uhid: str | None = None
    dob: date | None = None
    gender: str | None = None
    phone: str | None = None
    address: str | None = None
    pincode: str | None = None
    city: str | None = None
    state: str | None = None
    height_cm: float | None = None
    weight_kg: float | None = None
    created_at: datetime | None = None


class PatientListResponse(BaseModel):
    items: list[PatientResponse]
    total: int
