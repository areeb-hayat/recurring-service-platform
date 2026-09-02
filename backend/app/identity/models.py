"""Users and sessions.

SEC-7: the customer is not a login principal — there is no credential column on
``customer`` and no route that could issue a session for one.

SEC-11: passwords are Argon2id hashes; refresh tokens are stored only as a
SHA-256 hash and are revocable.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, uuid_nullable, uuid_pk, utc_timestamp
from app.core.ids import new_id

__all__ = ["AppUser", "UserSession", "Role", "ALL_ROLES"]


class Role:
    OWNER_ADMIN = "OWNER_ADMIN"
    PLATFORM_OWNER = "PLATFORM_OWNER"
    OPERATOR = "OPERATOR"  # SEC-8: reserved, granted nothing, unused in V1


ALL_ROLES = (Role.OWNER_ADMIN, Role.PLATFORM_OWNER, Role.OPERATOR)


class AppUser(Base):
    __tablename__ = "app_user"

    id: Mapped[uuid_pk] = mapped_column(default=new_id)
    # NULL exactly for platform scope (P0 §3.1). Enforced by scope_matches_role below.
    tenant_id: Mapped[uuid_nullable] = mapped_column(ForeignKey("tenant.id"))
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="ACTIVE")
    created_at: Mapped[utc_timestamp] = mapped_column(server_default=func.now())
    updated_at: Mapped[utc_timestamp] = mapped_column(server_default=func.now())

    __table_args__ = (
        # Composite-FK target for user_session.
        UniqueConstraint("tenant_id", "id", name="uq_app_user_tenant_id_id"),
        CheckConstraint(f"role IN {ALL_ROLES}", name="role_valid"),
        CheckConstraint("status IN ('ACTIVE','DISABLED')", name="status_valid"),
        # The structural half of SEC-6: a principal is tenant-scoped or
        # platform-scoped, never both, and the database refuses the alternative.
        CheckConstraint(
            "(role = 'PLATFORM_OWNER' AND tenant_id IS NULL) "
            "OR (role <> 'PLATFORM_OWNER' AND tenant_id IS NOT NULL)",
            name="scope_matches_role",
        ),
    )

    @property
    def scope(self) -> str:
        return "PLATFORM" if self.tenant_id is None else "TENANT"


class UserSession(Base):
    """One refresh token. The plaintext never touches the database."""

    __tablename__ = "user_session"

    id: Mapped[uuid_pk] = mapped_column(default=new_id)
    tenant_id: Mapped[uuid_nullable] = mapped_column(ForeignKey("tenant.id"))
    user_id: Mapped[uuid_pk] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("app_user.id"), primary_key=False, nullable=False
    )
    refresh_token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    issued_at: Mapped[utc_timestamp] = mapped_column(server_default=func.now())
    expires_at: Mapped[utc_timestamp]
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    device_label: Mapped[str | None] = mapped_column(String(120), nullable=True)

    __table_args__ = (
        # Composite FK keeps a tenant session pinned to a user of the same tenant.
        # For platform users tenant_id is NULL and MATCH SIMPLE skips the check,
        # so the plain user_id FK above still guarantees the user exists.
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["app_user.tenant_id", "app_user.id"],
            name="fk_user_session_tenant_id_user_id",
        ),
    )
