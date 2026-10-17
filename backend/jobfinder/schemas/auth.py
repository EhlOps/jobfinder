"""Sign-in, password-link and current-user bodies."""
from pydantic import BaseModel, EmailStr, Field


class LoginBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class LinkRequest(BaseModel):
    email: EmailStr


class ConfirmBody(BaseModel):
    token: str = Field(min_length=1, max_length=2048)
    password: str = Field(min_length=10, max_length=256)
    timezone: str | None = Field(default=None, max_length=64)  # from the browser; falls back to UTC


class UserOut(BaseModel):
    id: int
    email: str
    timezone: str
    digest_hour: int
    digest_enabled: bool
    is_admin: bool

    model_config = {"from_attributes": True}
