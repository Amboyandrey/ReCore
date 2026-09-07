"""Request and response shapes for the auth endpoints."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class SignupRequest(BaseModel):
    """What creating an account requires: an email and a password of adequate length."""

    email: EmailStr
    password: str = Field(min_length=12, max_length=256)


class LoginRequest(BaseModel):
    """Credentials submitted at sign-in."""

    email: EmailStr
    password: str


class UserOut(BaseModel):
    """The public shape of a user — the password hash never leaves the service layer."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    is_superuser: bool
    created_at: datetime


class LoginResponse(BaseModel):
    """What login returns: the user, plus the CSRF token the client must echo on mutations."""

    user: UserOut
    csrf_token: str
