from pydantic import BaseModel, ConfigDict


class WebsiteBase(BaseModel):
    name: str
    base_url: str
    city_id: int | None = None
    requires_js: bool = False
    is_active: bool = True


class WebsiteCreate(WebsiteBase):
    pass


class WebsiteRead(WebsiteBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
