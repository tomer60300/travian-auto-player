"""SQLAlchemy model for the resource planner's saved setup document.

ONE row per user per account, and deliberately NOT a preset library. The
operator asked for the planner's configuration to be importable and exportable,
not for a shelf of named variants to choose between -- and a store that keeps
several has to answer which one is in force, offer a picker, and decide what
happens when the active one is deleted. None of that was asked for, so the
composite primary key below makes "one saved setup" a fact of the schema rather
than a convention a later handler could drift from. If presets are ever wanted,
that is a new table with a name column, not a nullable one bolted onto this.

The document holds the operator's village names, coordinates and topology, so
`user_id` is half the primary key: there is no way to address a row without
saying whose it is.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class PlannerSetup(Base):
    """The planner's owned-state document as the frontend wrote it.

    Stored as the raw JSON text of the request body rather than a re-serialised
    model, because `plannerSetup.js`'s `buildSetup` omits every field it has no
    answer for: writing a validated model back out would turn "nothing
    declared" into a row of explicit defaults, and the page reads absence as a
    distinct state from zero.
    """

    __tablename__ = "planner_setups"

    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), primary_key=True)
    # `${serverUrl}|${playerName}`, the same key the page scopes its
    # localStorage to. Village ids are per account, so A's levels under B are
    # silently wrong -- the key is what keeps two worlds apart.
    account_key: Mapped[str] = mapped_column(String(256), primary_key=True)
    setup_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        # Never the document body: it is the operator's account topology.
        return f"<PlannerSetup user_id={self.user_id} account_key={self.account_key!r}>"
