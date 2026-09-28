"""Declarative base, shared column helpers and PostgreSQL enum types (audit §6.2)."""

import uuid
from datetime import datetime

from geoalchemy2 import Geography
from sqlalchemy import DateTime, Enum, MetaData, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


APP_ROLES = ("customer", "courier", "admin")
ORDER_STATUSES = (
    "pending",
    "offers_received",
    "accepted",
    "at_shop",
    "price_confirmation_needed",
    "purchased",
    "on_the_way",
    "delivered",
    "cancelled",
    "client_no_response",
)
OFFER_STATUSES = ("pending", "accepted", "rejected", "expired", "withdrawn")
VEHICLE_TYPES = ("walking", "scooter", "car")
PACKAGE_SIZES = ("petit", "moyen", "grand")
VERIFICATION_STATUSES = ("pending", "verified", "rejected")

# create_type=False: the migration creates/drops the types explicitly.
app_role = Enum(*APP_ROLES, name="app_role", create_type=False)
order_status = Enum(*ORDER_STATUSES, name="order_status", create_type=False)
offer_status = Enum(*OFFER_STATUSES, name="offer_status", create_type=False)
vehicle_type = Enum(*VEHICLE_TYPES, name="vehicle_type", create_type=False)
package_size = Enum(*PACKAGE_SIZES, name="package_size", create_type=False)
verification_st = Enum(*VERIFICATION_STATUSES, name="verification_st", create_type=False)


def Point() -> Geography:
    return Geography(geometry_type="POINT", srid=4326, spatial_index=False)


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))


def created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def updated_at() -> Mapped[datetime]:
    """Maintained by the `set_updated_at` trigger (see the initial migration)."""
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def legacy_id() -> Mapped[str | None]:
    return mapped_column(Text, unique=True)


E164_CHECK = r"~ '^\+[1-9][0-9]{7,14}$'"
