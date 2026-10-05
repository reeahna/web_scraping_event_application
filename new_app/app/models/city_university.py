from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.city import City


class CityUniversity(Base):
    """A school in a college town.

    A row per school rather than a list column: a town can have several (State
    College has Penn State; Boston has many), and the public chooser searches
    on them, which a JSON column cannot do portably across SQLite and
    PostgreSQL.
    """

    __tablename__ = "city_universities"
    __table_args__ = (
        # The same school twice in one town is always a mistake, so the
        # database refuses it rather than the form remembering to check.
        UniqueConstraint("city_id", "name", name="uq_city_university_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    city_id: Mapped[int] = mapped_column(
        ForeignKey("cities.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))

    city: Mapped["City"] = relationship(back_populates="universities")
