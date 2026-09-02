"""Local development bootstrap: provision one tenant, one owner, one platform owner.

There is no public signup (P0 §4) — tenant provisioning is a platform action —
so this exists to make a fresh database usable locally.

**No password or secret is hard-coded here.** Every credential is supplied by the
operator via CLI arguments or environment variables. Running this without
supplying passwords fails loudly rather than inventing a default.

    python -m app.bootstrap --tenant-slug acme --tenant-name "Acme Dairy" \
        --owner-email owner@example.com --platform-email platform@example.com
    # passwords read from BOOTSTRAP_OWNER_PASSWORD / BOOTSTRAP_PLATFORM_PASSWORD
"""

from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import session_scope
from app.core.security import hash_password
from app.identity.models import AppUser, Role
from app.tenancy.models import DEFAULT_REMINDER_SCHEDULE, Tenant

__all__ = ["provision_tenant", "provision_user", "main"]


def provision_tenant(
    session: Session,
    *,
    slug: str,
    name: str,
    timezone: str = "Asia/Karachi",
    currency: str = "PKR",
    unit_label: str = "unit",
    default_unit_price_minor: int = 0,
    default_quantity: Decimal = Decimal("0"),
) -> Tenant:
    existing = session.execute(select(Tenant).where(Tenant.slug == slug)).scalar_one_or_none()
    if existing is not None:
        return existing
    tenant = Tenant(
        slug=slug,
        name=name,
        timezone=timezone,
        currency=currency,
        unit_label=unit_label,
        default_unit_price_minor=default_unit_price_minor,
        default_quantity=default_quantity,
        reminder_schedule=DEFAULT_REMINDER_SCHEDULE,
    )
    session.add(tenant)
    session.flush()
    return tenant


def provision_user(
    session: Session, *, email: str, password: str, role: str, tenant: Tenant | None
) -> AppUser:
    if not password:
        raise ValueError(f"a password must be supplied for {email}")
    existing = session.execute(
        select(AppUser).where(AppUser.email == email)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    user = AppUser(
        tenant_id=tenant.id if tenant is not None else None,
        email=email,
        password_hash=hash_password(password),
        role=role,
    )
    session.add(user)
    session.flush()
    return user


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Provision local development data.")
    parser.add_argument("--tenant-slug", required=True)
    parser.add_argument("--tenant-name", required=True)
    parser.add_argument("--timezone", default="Asia/Karachi")
    parser.add_argument("--unit-label", default="unit")
    parser.add_argument("--owner-email", required=True)
    parser.add_argument("--platform-email", required=True)
    args = parser.parse_args(argv)

    owner_password = os.environ.get("BOOTSTRAP_OWNER_PASSWORD", "")
    platform_password = os.environ.get("BOOTSTRAP_PLATFORM_PASSWORD", "")
    if not owner_password or not platform_password:
        print(
            "Set BOOTSTRAP_OWNER_PASSWORD and BOOTSTRAP_PLATFORM_PASSWORD in the "
            "environment. This tool never invents a default password.",
            file=sys.stderr,
        )
        return 2

    session = session_scope()
    try:
        tenant = provision_tenant(
            session,
            slug=args.tenant_slug,
            name=args.tenant_name,
            timezone=args.timezone,
            unit_label=args.unit_label,
        )
        provision_user(
            session,
            email=args.owner_email,
            password=owner_password,
            role=Role.OWNER_ADMIN,
            tenant=tenant,
        )
        provision_user(
            session,
            email=args.platform_email,
            password=platform_password,
            role=Role.PLATFORM_OWNER,
            tenant=None,
        )
        session.commit()
        print(f"provisioned tenant {tenant.slug} ({tenant.id})")
    finally:
        session.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
