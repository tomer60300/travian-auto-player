"""SQLite database models and async session management for the Travian Web UI."""

import os
from datetime import datetime
from pathlib import Path

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# ---------------------------------------------------------------------------
# Engine & session factory
# ---------------------------------------------------------------------------

# Configurable via TRAVIAN_DB_PATH env var; defaults to ~/.travian/travian_web.db
_DEFAULT_DB_DIR = Path.home() / ".travian"
_DB_PATH = os.environ.get("TRAVIAN_DB_PATH", str(_DEFAULT_DB_DIR / "travian_web.db"))
# The directory the database actually lives in. Created here (not just the
# default dir) so a custom TRAVIAN_DB_PATH boots on a fresh machine; also the
# anchor for machine-local secrets that must travel WITH the database.
DB_DIR = Path(_DB_PATH).parent
DB_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_URL = f"sqlite+aiosqlite:///{_DB_PATH}"

engine = create_async_engine(DATABASE_URL, echo=False)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    credentials: Mapped[list["TravianCredential"]] = relationship(
        "TravianCredential", back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} username={self.username!r}>"


class TravianCredential(Base):
    __tablename__ = "travian_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    server_url: Mapped[str] = mapped_column(String(256), nullable=False)
    travian_username: Mapped[str] = mapped_column(String(128), nullable=False)
    encrypted_password: Mapped[str] = mapped_column(String(512), nullable=False)
    label: Mapped[str | None] = mapped_column(String(128), nullable=True, default=None)
    last_connected: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship("User", back_populates="credentials")

    def __repr__(self) -> str:
        return (
            f"<TravianCredential id={self.id} server={self.server_url!r} "
            f"user={self.travian_username!r}>"
        )


class ReconCredential(Base):
    """Credentials for the background ("recon") account.

    Deliberately not tied to a user: ReconAccountManager is a process-global
    singleton shared by every session, so a single row is the honest
    representation of what the runtime actually holds. The password is Fernet
    encrypted at rest, same as TravianCredential.
    """

    __tablename__ = "recon_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    travian_username: Mapped[str] = mapped_column(String(128), nullable=False)
    encrypted_password: Mapped[str] = mapped_column(String(512), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<ReconCredential id={self.id} user={self.travian_username!r}>"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Columns added after the first release. create_all() creates missing TABLES
# but never ALTERs existing ones, so upgrading a live travian_web.db needs
# these backfilled or every query naming them fails with 'no such column'.
_COLUMN_BACKFILLS: dict[str, dict[str, str]] = {
    "travian_credentials": {
        "label": "VARCHAR(128)",
        "last_connected": "DATETIME",
    },
}


async def init_db() -> None:
    """Create all tables if they don't already exist, and backfill columns."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for table, columns in _COLUMN_BACKFILLS.items():
            result = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
            existing = {row[1] for row in result.fetchall()}
            for name, ddl in columns.items():
                if existing and name not in existing:
                    await conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


async def get_db():
    """FastAPI dependency that yields an async database session."""
    async with async_session_factory() as session:
        try:
            yield session
        finally:
            await session.close()
