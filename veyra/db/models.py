from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_enc: Mapped[str] = mapped_column(Text)  # Fernet-encrypted
    game_user_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    session_cookies: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    task_configs: Mapped[list["TaskConfig"]] = relationship(back_populates="account")
    attack_logs: Mapped[list["AttackLog"]] = relationship(back_populates="account")
    damage_trackers: Mapped[list["DamageTracker"]] = relationship(back_populates="account")
    session_stats: Mapped[list["SessionStats"]] = relationship(back_populates="account")


class TaskConfig(Base):
    __tablename__ = "task_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    task_type: Mapped[str] = mapped_column(String(50))  # wave_farm|pvp|loot|stamina_farm|dungeon
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    account: Mapped["Account"] = relationship(back_populates="task_configs")


class DamageTracker(Base):
    __tablename__ = "damage_tracker"

    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    monster_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    damage_dealt: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    account: Mapped["Account"] = relationship(back_populates="damage_trackers")


class AttackLog(Base):
    __tablename__ = "attack_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    monster_id: Mapped[str] = mapped_column(String(50))
    monster_name: Mapped[str] = mapped_column(String(200))
    wave: Mapped[int] = mapped_column(Integer)
    damage_dealt: Mapped[int] = mapped_column(Integer)
    stamina_spent: Mapped[int] = mapped_column(Integer)
    result: Mapped[str] = mapped_column(String(20))  # done|stamina|error|dead
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    account: Mapped["Account"] = relationship(back_populates="attack_logs")


class SessionStats(Base):
    __tablename__ = "session_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    task_type: Mapped[str] = mapped_column(String(50))
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    monsters_attacked: Mapped[int] = mapped_column(Integer, default=0)
    total_damage: Mapped[int] = mapped_column(Integer, default=0)
    kills: Mapped[int] = mapped_column(Integer, default=0)
    loot_collected: Mapped[int] = mapped_column(Integer, default=0)
    pvp_wins: Mapped[int] = mapped_column(Integer, default=0)
    pvp_losses: Mapped[int] = mapped_column(Integer, default=0)
    stamina_spent: Mapped[int] = mapped_column(Integer, default=0)

    account: Mapped["Account"] = relationship(back_populates="session_stats")
