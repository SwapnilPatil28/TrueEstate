from datetime import date
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class AuditCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    listing_url: HttpUrl | None = None
    project_name: str | None = Field(default=None, min_length=2, max_length=160)
    builder: str | None = Field(default=None, min_length=2, max_length=160)
    locality: str | None = Field(default=None, min_length=2, max_length=120)
    city: str | None = Field(default=None, min_length=2, max_length=100)
    state: str = Field(default="Karnataka", min_length=2, max_length=100)
    rera_id: str | None = Field(default=None, max_length=100)
    possession_date: date | None = None
    carpet_area: str | None = Field(default=None, max_length=80)
    price: str | None = Field(default=None, max_length=80)

    @field_validator("listing_url")
    @classmethod
    def allow_only_public_http_urls(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value and value.scheme not in {"http", "https"}:
            raise ValueError("Listing URL must use HTTP or HTTPS.")
        return value

    def validate_input(self) -> None:
        if not self.listing_url and not all((self.project_name, self.builder, self.city)):
            raise ValueError(
                "Enter a listing URL or provide project name, builder and city."
            )


class AuditRetry(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    project_name: str | None = Field(default=None, min_length=2, max_length=160)
    builder: str | None = Field(default=None, min_length=2, max_length=160)
    locality: str | None = Field(default=None, min_length=2, max_length=120)
    city: str | None = Field(default=None, min_length=2, max_length=100)
    state: str | None = Field(default=None, min_length=2, max_length=100)


class CandidateSelection(BaseModel):
    candidate: dict


class WatchCreate(BaseModel):
    audit_id: str
    schedule_cron: str = "0 9 * * *"


class WebhookPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    monitor_id: str | None = None
    id: str | None = None
    type: str | None = None
    result: dict | list | str | None = None
