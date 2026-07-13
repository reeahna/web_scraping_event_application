from datetime import date, datetime, time

from pydantic import BaseModel, ConfigDict


class EventBase(BaseModel):
    title: str
    normalized_title: str | None = None
    canonical_url: str
    source: str
    external_source_id: str | None = None
    fingerprint: str | None = None
    description: str | None = None
    source_category: str | None = None
    timezone: str | None = None
    origin: str = "extracted"
    start_date: date | None = None
    end_date: date | None = None
    start_time: time | None = None
    end_time: time | None = None
    venue: str | None = None
    address: str | None = None
    image_url: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    scraped_at: datetime | None = None
    is_active: bool = True
    city_id: int | None = None
    website_id: int | None = None
    category_id: int | None = None
    review_status: str = "needs_review"
    duplicate_status: str = "not_reviewed"
    category_source: str = "uncategorized"


class EventCreate(EventBase):
    pass


class EventRead(EventBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
