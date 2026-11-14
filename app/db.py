from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, Text, create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from . import config


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _engine():
    """Builds the database engine. Postgres uses pg8000 (pure Python), so it also
    installs on Termux/phones. Supabase/Neon urls with sslmode=require just work."""
    url = make_url(config.DATABASE_URL)
    connect_args = {}
    if url.drivername.startswith("postgres"):
        if not url.username or not url.host or url.host == "...":
            raise RuntimeError(
                "DATABASE_URL is incomplete. Paste the real connection string from "
                "Supabase or Neon, or delete the DATABASE_URL line to test with a local file."
            )
        query = dict(url.query)
        sslmode = query.pop("sslmode", None)
        query.pop("channel_binding", None)  # added by Neon, pg8000 does not know it
        url = url.set(drivername="postgresql+pg8000", query=query)
        if sslmode not in (None, "disable"):
            connect_args["ssl_context"] = True
    elif url.drivername.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    return create_engine(url, pool_pre_ping=True, connect_args=connect_args)


engine = _engine()
SessionLocal = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Batch(Base):
    __tablename__ = "batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    subjects: Mapped[List["Subject"]] = relationship(
        cascade="all, delete-orphan", order_by="Subject.id"
    )


class Subject(Base):
    __tablename__ = "subjects"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"))
    name: Mapped[str] = mapped_column(String(200))
    units: Mapped[List["Unit"]] = relationship(
        cascade="all, delete-orphan", order_by="Unit.id"
    )


class Unit(Base):
    __tablename__ = "units"
    id: Mapped[int] = mapped_column(primary_key=True)
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id"))
    name: Mapped[str] = mapped_column(String(200))
    lectures: Mapped[List["Lecture"]] = relationship(
        cascade="all, delete-orphan", order_by="Lecture.id"
    )


class Lecture(Base):
    """One lecture: a video, a PDF, or both. The numbers are message ids
    inside the private Telegram channel where the files are stored."""

    __tablename__ = "lectures"
    id: Mapped[int] = mapped_column(primary_key=True)
    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id"))
    title: Mapped[str] = mapped_column(String(300))
    video_msg_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    pdf_msg_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    thumb_msg_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    # Direct links (https). When set, the app plays/opens them straight away.
    video_url: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True)
    pdf_url: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True)
    free: Mapped[bool] = mapped_column(Boolean, default=False)


class Key(Base):
    __tablename__ = "keys"
    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    device_id: Mapped[str] = mapped_column(String(32))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Setting(Base):
    """Small key/value store. The 'about' key holds the About Developer page as JSON."""

    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Order(Base):
    """One purchase attempt made through the shop bot."""

    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    plan_name: Mapped[str] = mapped_column(String(100))
    days: Mapped[int]
    amount_paise: Mapped[int]
    device_id: Mapped[str] = mapped_column(String(32), default="")
    link_id: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | paid
    payment_id: Mapped[Optional[str]] = mapped_column(String(64), unique=True, nullable=True)
    key: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


def init_db() -> None:
    Base.metadata.create_all(engine)
    order_cols = {c["name"] for c in inspect(engine).get_columns("orders")}
    if "device_id" not in order_cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE orders ADD COLUMN device_id VARCHAR(32) DEFAULT ''"))
    # create_all does not add columns to a table that already exists, so add them here.
    have = {c["name"] for c in inspect(engine).get_columns("lectures")}
    with engine.begin() as conn:
        for col in ("video_url", "pdf_url"):
            if col not in have:
                conn.execute(text(f"ALTER TABLE lectures ADD COLUMN {col} VARCHAR(2000)"))
