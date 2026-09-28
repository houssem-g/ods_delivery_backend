"""Read-only views of derived counters (created by the migrations, never by metadata.create_all).

Declared on their own MetaData so Alembic autogenerate does not try to create them as tables.
"""

from sqlalchemy import BigInteger, Column, DateTime, MetaData, Numeric, Table
from sqlalchemy.dialects.postgresql import UUID

views_metadata = MetaData()

# Incidents older than this no longer count (getCustomerReliability / placeOrder / acceptOrderOffer).
INCIDENT_WINDOW_DAYS = 180

courier_stats = Table(
    "courier_stats",
    views_metadata,
    Column("courier_id", UUID(as_uuid=True), primary_key=True),
    Column("total_deliveries", BigInteger),
    Column("gross_fees", Numeric(12, 3)),
    Column("average_rating", Numeric(3, 2)),
    Column("ratings_count", BigInteger),
)

customer_stats = Table(
    "customer_stats",
    views_metadata,
    Column("user_id", UUID(as_uuid=True), primary_key=True),
    Column("total_orders", BigInteger),
    Column("no_response_incidents", BigInteger),
    Column("last_incident_at", DateTime(timezone=True)),
)
