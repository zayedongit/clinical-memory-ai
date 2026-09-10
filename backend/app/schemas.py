"""Pydantic request/response models (the API contract)."""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator


class MeResponse(BaseModel):
    user_id: str
    clinic_id: str
    role: str
    clinic_name: str | None = None


class ClinicBootstrapRequest(BaseModel):
    clinic_name: str = Field(min_length=1, max_length=200)
    user_name: str = Field(min_length=1, max_length=200)


class ClinicBootstrapResponse(BaseModel):
    clinic_id: str
    user_id: str


class PatientCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    dob: date | None = None
    gender: str | None = Field(default=None, max_length=32)
    phone: str | None = Field(default=None, max_length=32)
    address: str | None = Field(default=None, max_length=500)
    pincode: str | None = Field(default=None, max_length=16)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)

    @field_validator("dob")
    @classmethod
    def _not_in_future(cls, v: date | None) -> date | None:
        # A future date of birth silently produces a negative age, which then
        # flows into the risk model's age features as a valid number.
        if v and v > date.today():
            raise ValueError("Date of birth cannot be in the future")
        return v


class PatientUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    dob: date | None = None
    gender: str | None = Field(default=None, max_length=32)
    phone: str | None = Field(default=None, max_length=32)
    address: str | None = Field(default=None, max_length=500)
    pincode: str | None = Field(default=None, max_length=16)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)


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
    total: int          # matching rows in the clinic, not rows on this page
    limit: int = 50
    offset: int = 0
