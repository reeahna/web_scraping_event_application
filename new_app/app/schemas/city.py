from pydantic import BaseModel, ConfigDict


class CityBase(BaseModel):
    name: str
    slug: str
    state_or_region: str | None = None
    country: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    is_active: bool = True


class CityCreate(CityBase):
    pass


class CityRead(CityBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
