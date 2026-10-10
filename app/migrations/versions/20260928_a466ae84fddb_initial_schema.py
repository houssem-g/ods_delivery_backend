"""initial schema: every table of DB_AUDIT §6.2 + files, push_deliveries and the legacy-field homes

Generated with `alembic revision --autogenerate`, then reviewed by hand:
- enum types created once up front (columns use create_type=False);
- the three circular foreign keys (users→couriers, orders→hot_deals,
  ledger→statements) added after both tables exist;
- `set_updated_at()` trigger on every table with an updated_at column;
- views courier_stats / customer_stats (derived counters);
- extensions are created IF NOT EXISTS and never dropped (shared by the database).

Revision ID: a466ae84fddb
Revises: 
Create Date: 2026-09-28 11:52:09.204372
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geography
from sqlalchemy.dialects import postgresql
revision: str = 'a466ae84fddb'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENUMS = {
    "app_role": ("customer", "courier", "admin"),
    "order_status": (
        "pending", "offers_received", "accepted", "at_shop", "price_confirmation_needed",
        "purchased", "on_the_way", "delivered", "cancelled", "client_no_response",
    ),
    "offer_status": ("pending", "accepted", "rejected", "expired", "withdrawn"),
    "vehicle_type": ("walking", "scooter", "car"),
    "package_size": ("petit", "moyen", "grand"),
    "verification_st": ("pending", "verified", "rejected"),
}

CIRCULAR_FKS = (
    ("fk_users_referred_by_courier_id_couriers", "users", "couriers", ["referred_by_courier_id"], "SET NULL"),
    ("fk_orders_resale_deal_id_hot_deals", "orders", "hot_deals", ["resale_deal_id"], "SET NULL"),
    (
        "fk_courier_ledger_entries_statement_id_courier_statements",
        "courier_ledger_entries", "courier_statements", ["statement_id"], None,
    ),
)

UPDATED_AT_TABLES = (
    "users", "user_addresses", "couriers", "places", "shops", "shop_reviews", "orders", "order_stops",
    "order_offers", "messages", "notifications", "device_tokens", "outbound_messages",
    "no_response_cases", "hot_deals", "app_settings", "files",
)

COURIER_STATS_SQL = """
CREATE VIEW courier_stats AS
SELECT c.id AS courier_id,
       count(o.id) FILTER (WHERE o.status = 'delivered') AS total_deliveries,
       coalesce(sum(o.delivery_fee) FILTER (WHERE o.status = 'delivered'), 0)::numeric(12,3) AS gross_fees,
       r.average_rating,
       coalesce(r.ratings_count, 0) AS ratings_count
FROM couriers c
LEFT JOIN orders o ON o.courier_id = c.id
LEFT JOIN (
    SELECT courier_id, avg(rating)::numeric(3,2) AS average_rating, count(*) AS ratings_count
    FROM order_ratings GROUP BY courier_id
) r ON r.courier_id = c.id
GROUP BY c.id, r.average_rating, r.ratings_count
"""

# Incident rule of getCustomerReliability / placeOrder / acceptOrderOffer: a case
# counts while incident_counted and its date (final_at, else started_at) is
# within the last 180 days.
CUSTOMER_STATS_SQL = """
CREATE VIEW customer_stats AS
SELECT u.id AS user_id,
       (SELECT count(*) FROM orders o WHERE o.customer_id = u.id) AS total_orders,
       (SELECT count(*) FROM no_response_cases n JOIN orders o ON o.id = n.order_id
         WHERE o.customer_id = u.id AND n.incident_counted
           AND coalesce(n.final_at, n.started_at) >= now() - interval '180 days') AS no_response_incidents,
       (SELECT max(coalesce(n.final_at, n.started_at)) FROM no_response_cases n JOIN orders o ON o.id = n.order_id
         WHERE o.customer_id = u.id AND n.incident_counted) AS last_incident_at
FROM users u
"""


def upgrade() -> None:
    for ext in ("citext", "postgis", "pg_trgm", "pgcrypto"):
        op.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")
    for name, values in ENUMS.items():
        postgresql.ENUM(*values, name=name).create(op.get_bind())
    op.execute(
        """
        CREATE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.updated_at := now();
            RETURN NEW;
        END $$
        """
    )
    op.create_table('places',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('osm_id', sa.Text(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('name_norm', sa.Text(), nullable=False),
    sa.Column('category', sa.Text(), nullable=False),
    sa.Column('address', sa.Text(), nullable=True),
    sa.Column('city', sa.Text(), nullable=True),
    sa.Column('governorate', sa.Text(), nullable=True),
    sa.Column('phone', sa.Text(), nullable=True),
    sa.Column('opening_hours', sa.Text(), nullable=True),
    sa.Column('location', Geography(geometry_type='POINT', srid=4326, dimension=2, spatial_index=False, from_text='ST_GeogFromText', name='geography', nullable=False), nullable=False),
    sa.Column('source', sa.Text(), server_default='osm', nullable=False),
    sa.Column('source_ts', sa.DateTime(timezone=True), nullable=True),
    sa.Column('quality_score', sa.SmallInteger(), nullable=True),
    sa.Column('refreshed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('old_import_id', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_places')),
    sa.UniqueConstraint('old_import_id', name=op.f('uq_places_old_import_id')),
    sa.UniqueConstraint('osm_id', name=op.f('uq_places_osm_id'))
    )
    op.create_index('places_cat', 'places', ['category'], unique=False)
    op.create_index('places_geo', 'places', ['location'], unique=False, postgresql_using='gist', postgresql_ops={})
    op.create_index('places_name', 'places', ['name_norm'], unique=False, postgresql_using='gin', postgresql_ops={'name_norm': 'gin_trgm_ops'})
    op.create_table('users',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('email', postgresql.CITEXT(), nullable=False),
    sa.Column('email_verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('password_hash', sa.Text(), nullable=True),
    sa.Column('google_sub', sa.Text(), nullable=True),
    sa.Column('full_name', sa.Text(), server_default='', nullable=False),
    sa.Column('phone_e164', sa.Text(), nullable=True),
    sa.Column('role', postgresql.ENUM(name='app_role', create_type=False), server_default='customer', nullable=False),
    sa.Column('language', sa.Text(), server_default='ar', nullable=False),
    sa.Column('profile_created_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('notify_order_status', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('notify_new_orders', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('notify_incoming_orders', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('notify_chat', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('push_enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('whatsapp_opt_in_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('terms_accepted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('terms_version', sa.Text(), nullable=True),
    sa.Column('is_blacklisted', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('referred_by_courier_id', sa.UUID(), nullable=True),
    sa.Column('referred_by_code', sa.Text(), nullable=True),
    sa.Column('referred_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('old_import_id', sa.Text(), nullable=True),
    sa.Column('old_import_profile_id', sa.Text(), nullable=True),
    sa.Column('disabled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("language IN ('ar','fr')", name=op.f('ck_users_language')),
    sa.CheckConstraint("phone_e164 ~ '^\\+[1-9][0-9]{7,14}$'", name=op.f('ck_users_phone_e164')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('email', name=op.f('uq_users_email')),
    sa.UniqueConstraint('google_sub', name=op.f('uq_users_google_sub')),
    sa.UniqueConstraint('old_import_id', name=op.f('uq_users_old_import_id')),
    sa.UniqueConstraint('old_import_profile_id', name=op.f('uq_users_old_import_profile_id'))
    )
    op.create_index('ix_users_referred_by_courier_id', 'users', ['referred_by_courier_id'], unique=False)
    op.create_index('ix_users_role', 'users', ['role'], unique=False)
    op.create_table('app_settings',
    sa.Column('key', sa.Text(), nullable=False),
    sa.Column('value', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('updated_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], name=op.f('fk_app_settings_updated_by_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('key', name=op.f('pk_app_settings'))
    )
    op.create_table('audit_log',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('actor_user_id', sa.UUID(), nullable=True),
    sa.Column('action', sa.Text(), nullable=False),
    sa.Column('entity', sa.Text(), nullable=False),
    sa.Column('entity_id', sa.Text(), nullable=False),
    sa.Column('before', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('after', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('ip', postgresql.INET(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], name=op.f('fk_audit_log_actor_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_log'))
    )
    op.create_index('ix_audit_log_entity_entity_id_created_at', 'audit_log', ['entity', 'entity_id', 'created_at'], unique=False)
    op.create_table('couriers',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('display_name', sa.Text(), nullable=False),
    sa.Column('phone_e164', sa.Text(), nullable=False),
    sa.Column('id_document_number', sa.Text(), nullable=False),
    sa.Column('id_document_key', sa.Text(), nullable=True),
    sa.Column('vehicle', postgresql.ENUM(name='vehicle_type', create_type=False), nullable=False),
    sa.Column('max_package', postgresql.ENUM(name='package_size', create_type=False), server_default='petit', nullable=False),
    sa.Column('price_per_km', sa.Numeric(precision=10, scale=3), nullable=False),
    sa.Column('min_fee', sa.Numeric(precision=10, scale=3), nullable=True),
    sa.Column('notification_radius_km', sa.Numeric(precision=5, scale=1), server_default='10', nullable=False),
    sa.Column('service_governorate', sa.Text(), nullable=True),
    sa.Column('service_city', sa.Text(), nullable=True),
    sa.Column('service_country', sa.String(length=2), server_default='TN', nullable=True),
    sa.Column('service_start', sa.Time(), nullable=True),
    sa.Column('service_end', sa.Time(), nullable=True),
    sa.Column('verification', postgresql.ENUM(name='verification_st', create_type=False), server_default='pending', nullable=False),
    sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('verified_by', sa.UUID(), nullable=True),
    sa.Column('rejection_reason', sa.Text(), nullable=True),
    sa.Column('referral_code', sa.Text(), nullable=True),
    sa.Column('late_cancellations', sa.Integer(), server_default='0', nullable=False),
    sa.Column('is_online', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('last_location', Geography(geometry_type='POINT', srid=4326, dimension=2, spatial_index=False, from_text='ST_GeogFromText', name='geography'), nullable=True),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('old_import_id', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("phone_e164 ~ '^\\+[1-9][0-9]{7,14}$'", name=op.f('ck_couriers_phone_e164')),
    sa.CheckConstraint('min_fee >= 0 AND min_fee <= 200', name=op.f('ck_couriers_min_fee')),
    sa.CheckConstraint('notification_radius_km BETWEEN 0 AND 100', name=op.f('ck_couriers_notification_radius_km')),
    sa.CheckConstraint('price_per_km >= 0 AND price_per_km <= 50', name=op.f('ck_couriers_price_per_km')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_couriers_user_id_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['verified_by'], ['users.id'], name=op.f('fk_couriers_verified_by_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_couriers')),
    sa.UniqueConstraint('old_import_id', name=op.f('uq_couriers_old_import_id')),
    sa.UniqueConstraint('referral_code', name=op.f('uq_couriers_referral_code')),
    sa.UniqueConstraint('user_id', name=op.f('uq_couriers_user_id'))
    )
    op.create_index('couriers_dispatch', 'couriers', ['last_location'], unique=False, postgresql_using='gist', postgresql_where=sa.text("is_online AND verification = 'verified'"), postgresql_ops={})
    op.create_table('device_tokens',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('token', sa.Text(), nullable=False),
    sa.Column('platform', sa.Text(), nullable=False),
    sa.Column('app_version', sa.Text(), nullable=True),
    sa.Column('device_model', sa.Text(), nullable=True),
    sa.Column('locale', sa.Text(), nullable=True),
    sa.Column('failure_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('old_import_id', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("platform IN ('web','android','ios')", name=op.f('ck_device_tokens_platform')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_device_tokens_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_device_tokens')),
    sa.UniqueConstraint('old_import_id', name=op.f('uq_device_tokens_old_import_id')),
    sa.UniqueConstraint('token', name=op.f('uq_device_tokens_token'))
    )
    op.create_index('ix_device_tokens_active_user', 'device_tokens', ['user_id'], unique=False, postgresql_where=sa.text('is_active'))
    op.create_table('email_codes',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('purpose', sa.Text(), nullable=False),
    sa.Column('code_hash', sa.Text(), nullable=False),
    sa.Column('link_hash', sa.Text(), nullable=True),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("purpose IN ('verify','reset','migrate')", name=op.f('ck_email_codes_purpose')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_email_codes_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_email_codes')),
    sa.UniqueConstraint('link_hash', name=op.f('uq_email_codes_link_hash'))
    )
    op.create_index('ix_email_codes_open', 'email_codes', ['user_id', 'purpose'], unique=False, postgresql_where=sa.text('used_at IS NULL'))
    op.create_table('files',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('key', sa.Text(), nullable=False),
    sa.Column('owner_id', sa.UUID(), nullable=True),
    sa.Column('visibility', sa.Text(), nullable=False),
    sa.Column('purpose', sa.Text(), server_default='generic', nullable=False),
    sa.Column('content_type', sa.Text(), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('original_name', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("visibility IN ('public','private')", name=op.f('ck_files_visibility')),
    sa.CheckConstraint('size_bytes >= 0', name=op.f('ck_files_size_bytes')),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], name=op.f('fk_files_owner_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_files')),
    sa.UniqueConstraint('key', name=op.f('uq_files_key'))
    )
    op.create_index('ix_files_owner_id', 'files', ['owner_id'], unique=False)
    op.create_table('refresh_tokens',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('token_hash', sa.Text(), nullable=False),
    sa.Column('family', sa.UUID(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('rotated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_refresh_tokens_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_refresh_tokens')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_refresh_tokens_token_hash'))
    )
    op.create_index('ix_refresh_tokens_family', 'refresh_tokens', ['family'], unique=False)
    op.create_index(op.f('ix_refresh_tokens_user_id'), 'refresh_tokens', ['user_id'], unique=False)
    op.create_table('shops',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('place_id', sa.BigInteger(), nullable=True),
    sa.Column('osm_id', sa.Text(), nullable=True),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('address', sa.Text(), nullable=True),
    sa.Column('governorate', sa.Text(), nullable=True),
    sa.Column('city', sa.Text(), nullable=True),
    sa.Column('phone', sa.Text(), nullable=True),
    sa.Column('categories', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False),
    sa.Column('opening_hours', sa.Text(), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('photo_key', sa.Text(), nullable=True),
    sa.Column('location', Geography(geometry_type='POINT', srid=4326, dimension=2, spatial_index=False, from_text='ST_GeogFromText', name='geography', nullable=False), nullable=False),
    sa.Column('review_status', sa.Text(), server_default='approved', nullable=False),
    sa.Column('proposed_by', sa.UUID(), nullable=True),
    sa.Column('proposed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('reviewed_by', sa.UUID(), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('old_import_id', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("review_status IN ('pending','approved','rejected')", name=op.f('ck_shops_review_status')),
    sa.ForeignKeyConstraint(['place_id'], ['places.id'], name=op.f('fk_shops_place_id_places'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['proposed_by'], ['users.id'], name=op.f('fk_shops_proposed_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['reviewed_by'], ['users.id'], name=op.f('fk_shops_reviewed_by_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_shops')),
    sa.UniqueConstraint('old_import_id', name=op.f('uq_shops_old_import_id')),
    sa.UniqueConstraint('osm_id', name=op.f('uq_shops_osm_id'))
    )
    op.create_index('ix_shops_proposed_by', 'shops', ['proposed_by'], unique=False)
    op.create_index('ix_shops_review_status', 'shops', ['review_status'], unique=False)
    op.create_index('shops_geo', 'shops', ['location'], unique=False, postgresql_using='gist', postgresql_ops={})
    op.create_table('user_addresses',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('label', sa.Text(), nullable=True),
    sa.Column('address', sa.Text(), server_default='', nullable=False),
    sa.Column('details', sa.Text(), nullable=True),
    sa.Column('governorate', sa.Text(), nullable=True),
    sa.Column('city', sa.Text(), nullable=True),
    sa.Column('country', sa.String(length=2), server_default='TN', nullable=True),
    sa.Column('location', Geography(geometry_type='POINT', srid=4326, dimension=2, spatial_index=False, from_text='ST_GeogFromText', name='geography'), nullable=True),
    sa.Column('is_default', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_addresses_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_user_addresses'))
    )
    op.create_index('one_default_address', 'user_addresses', ['user_id'], unique=True, postgresql_where=sa.text('is_default'))
    op.create_table('courier_statements',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('courier_id', sa.UUID(), nullable=False),
    sa.Column('period_start', sa.Date(), nullable=False),
    sa.Column('period_end', sa.Date(), nullable=False),
    sa.Column('total_due', sa.Numeric(precision=10, scale=3), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('open','sent','paid','void')", name=op.f('ck_courier_statements_status')),
    sa.ForeignKeyConstraint(['courier_id'], ['couriers.id'], name=op.f('fk_courier_statements_courier_id_couriers')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_courier_statements')),
    sa.UniqueConstraint('courier_id', 'period_start', name=op.f('uq_courier_statements_courier_id_period_start'))
    )
    op.create_table('orders',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('customer_id', sa.UUID(), nullable=False),
    sa.Column('courier_id', sa.UUID(), nullable=True),
    sa.Column('preferred_courier_id', sa.UUID(), nullable=True),
    sa.Column('status', postgresql.ENUM(name='order_status', create_type=False), server_default='pending', nullable=False),
    sa.Column('items_text', sa.Text(), nullable=False),
    sa.Column('quantity', sa.Integer(), server_default='1', nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('alternatives', sa.Text(), nullable=True),
    sa.Column('package', postgresql.ENUM(name='package_size', create_type=False), server_default='petit', nullable=False),
    sa.Column('estimated_price', sa.Numeric(precision=10, scale=3), nullable=True),
    sa.Column('contact_name', sa.Text(), nullable=False),
    sa.Column('contact_phone_e164', sa.Text(), nullable=True),
    sa.Column('delivery_address', sa.Text(), nullable=False),
    sa.Column('delivery_details', sa.Text(), nullable=True),
    sa.Column('delivery_governorate', sa.Text(), nullable=True),
    sa.Column('delivery_city', sa.Text(), nullable=True),
    sa.Column('delivery_location', Geography(geometry_type='POINT', srid=4326, dimension=2, spatial_index=False, from_text='ST_GeogFromText', name='geography'), nullable=True),
    sa.Column('scheduled_for', sa.DateTime(timezone=True), nullable=True),
    sa.Column('distance_km', sa.Numeric(precision=6, scale=2), nullable=True),
    sa.Column('eta_minutes', sa.Integer(), nullable=True),
    sa.Column('purchase_amount', sa.Numeric(precision=10, scale=3), nullable=True),
    sa.Column('delivery_fee', sa.Numeric(precision=10, scale=3), nullable=True),
    sa.Column('payment_method', sa.Text(), server_default='cash', nullable=False),
    sa.Column('price_confirmed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('current_stop_seq', sa.SmallInteger(), server_default='0', nullable=False),
    sa.Column('cancelled_by', sa.Text(), nullable=True),
    sa.Column('cancel_reason', sa.Text(), nullable=True),
    sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_dispatched_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resale_deal_id', sa.UUID(), nullable=True),
    sa.Column('old_import_id', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("cancelled_by IN ('customer','courier','admin','system')", name=op.f('ck_orders_cancelled_by')),
    sa.CheckConstraint("contact_phone_e164 ~ '^\\+[1-9][0-9]{7,14}$'", name=op.f('ck_orders_contact_phone_e164')),
    sa.CheckConstraint("payment_method IN ('cash')", name=op.f('ck_orders_payment_method')),
    sa.CheckConstraint("status <> 'cancelled' OR cancelled_at IS NOT NULL", name=op.f('ck_orders_cancelled_at')),
    sa.CheckConstraint("status <> 'delivered' OR delivered_at IS NOT NULL", name=op.f('ck_orders_delivered_at')),
    sa.CheckConstraint("status NOT IN ('accepted','at_shop','price_confirmation_needed','purchased','on_the_way','delivered','client_no_response') OR courier_id IS NOT NULL", name=op.f('ck_orders_courier_when_assigned')),
    sa.CheckConstraint('delivery_fee >= 0 AND delivery_fee <= 200', name=op.f('ck_orders_delivery_fee')),
    sa.CheckConstraint('estimated_price >= 0', name=op.f('ck_orders_estimated_price')),
    sa.CheckConstraint('length(items_text) BETWEEN 1 AND 2000', name=op.f('ck_orders_items_text')),
    sa.CheckConstraint('purchase_amount >= 0 AND purchase_amount <= 2000', name=op.f('ck_orders_purchase_amount')),
    sa.CheckConstraint('quantity BETWEEN 1 AND 100', name=op.f('ck_orders_quantity')),
    sa.ForeignKeyConstraint(['courier_id'], ['couriers.id'], name=op.f('fk_orders_courier_id_couriers'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['customer_id'], ['users.id'], name=op.f('fk_orders_customer_id_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['preferred_courier_id'], ['couriers.id'], name=op.f('fk_orders_preferred_courier_id_couriers'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_orders')),
    sa.UniqueConstraint('old_import_id', name=op.f('uq_orders_old_import_id'))
    )
    op.create_index('ix_orders_preferred_courier_id', 'orders', ['preferred_courier_id'], unique=False)
    op.create_index('orders_active', 'orders', ['status'], unique=False, postgresql_where=sa.text("status NOT IN ('delivered','cancelled')"))
    op.create_index('orders_courier', 'orders', ['courier_id', 'status', sa.literal_column('created_at DESC')], unique=False, postgresql_where=sa.text('courier_id IS NOT NULL'))
    op.create_index('orders_customer', 'orders', ['customer_id', sa.literal_column('created_at DESC')], unique=False)
    op.create_index('orders_open_geo', 'orders', ['delivery_location'], unique=False, postgresql_using='gist', postgresql_where=sa.text("status IN ('pending','offers_received')"), postgresql_ops={})
    op.create_table('shop_menu_items',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('shop_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('price', sa.Numeric(precision=10, scale=3), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('photo_key', sa.Text(), nullable=True),
    sa.Column('position', sa.Integer(), server_default='0', nullable=False),
    sa.CheckConstraint('price >= 0', name=op.f('ck_shop_menu_items_price')),
    sa.ForeignKeyConstraint(['shop_id'], ['shops.id'], name=op.f('fk_shop_menu_items_shop_id_shops'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_shop_menu_items'))
    )
    op.create_index(op.f('ix_shop_menu_items_shop_id'), 'shop_menu_items', ['shop_id'], unique=False)
    op.create_table('shop_reviews',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('shop_id', sa.UUID(), nullable=True),
    sa.Column('place_id', sa.BigInteger(), nullable=True),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('rating', sa.SmallInteger(), nullable=False),
    sa.Column('comment', sa.Text(), nullable=True),
    sa.Column('photo_keys', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False),
    sa.Column('old_import_id', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('(shop_id IS NULL) <> (place_id IS NULL)', name=op.f('ck_shop_reviews_one_target')),
    sa.CheckConstraint('rating BETWEEN 1 AND 5', name=op.f('ck_shop_reviews_rating')),
    sa.ForeignKeyConstraint(['place_id'], ['places.id'], name=op.f('fk_shop_reviews_place_id_places'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['shop_id'], ['shops.id'], name=op.f('fk_shop_reviews_shop_id_shops'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_shop_reviews_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_shop_reviews')),
    sa.UniqueConstraint('old_import_id', name=op.f('uq_shop_reviews_old_import_id'))
    )
    op.create_index('ix_shop_reviews_place_id', 'shop_reviews', ['place_id'], unique=False)
    op.create_index('ix_shop_reviews_shop_id', 'shop_reviews', ['shop_id'], unique=False)
    op.create_index('one_review_per_user_place', 'shop_reviews', ['user_id', 'place_id'], unique=True, postgresql_where=sa.text('place_id IS NOT NULL'))
    op.create_index('one_review_per_user_shop', 'shop_reviews', ['user_id', 'shop_id'], unique=True, postgresql_where=sa.text('shop_id IS NOT NULL'))
    op.create_table('courier_ledger_entries',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('courier_id', sa.UUID(), nullable=False),
    sa.Column('order_id', sa.UUID(), nullable=True),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=10, scale=3), nullable=False),
    sa.Column('statement_id', sa.BigInteger(), nullable=True),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('commission_due','commission_waived_launch','commission_waived_quota','payment_received','adjustment')", name=op.f('ck_courier_ledger_entries_kind')),
    sa.ForeignKeyConstraint(['courier_id'], ['couriers.id'], name=op.f('fk_courier_ledger_entries_courier_id_couriers'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_courier_ledger_entries_created_by_users')),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], name=op.f('fk_courier_ledger_entries_order_id_orders'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_courier_ledger_entries')),
    sa.UniqueConstraint('order_id', 'kind', name=op.f('uq_courier_ledger_entries_order_id_kind'))
    )
    op.create_index('ix_courier_ledger_entries_courier_id_created_at', 'courier_ledger_entries', ['courier_id', 'created_at'], unique=False)
    op.create_table('hot_deals',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('original_order_id', sa.UUID(), nullable=False),
    sa.Column('courier_id', sa.UUID(), nullable=False),
    sa.Column('items_text', sa.Text(), nullable=False),
    sa.Column('shop_name', sa.Text(), nullable=True),
    sa.Column('shop_address', sa.Text(), nullable=True),
    sa.Column('purchase_amount', sa.Numeric(precision=10, scale=3), nullable=False),
    sa.Column('discount_percentage', sa.Numeric(precision=5, scale=2), nullable=False),
    sa.Column('price', sa.Numeric(precision=10, scale=3), nullable=False),
    sa.Column('include_delivery', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('delivery_fee', sa.Numeric(precision=10, scale=3), nullable=True),
    sa.Column('photo_key', sa.Text(), nullable=True),
    sa.Column('pickup_location', Geography(geometry_type='POINT', srid=4326, dimension=2, spatial_index=False, from_text='ST_GeogFromText', name='geography'), nullable=True),
    sa.Column('status', sa.Text(), server_default='available', nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('buyer_id', sa.UUID(), nullable=True),
    sa.Column('reserved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('buyer_order_id', sa.UUID(), nullable=True),
    sa.Column('old_import_id', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status <> 'sold' OR buyer_id IS NOT NULL", name=op.f('ck_hot_deals_sold_has_buyer')),
    sa.CheckConstraint("status IN ('available','sold','expired')", name=op.f('ck_hot_deals_status')),
    sa.CheckConstraint('discount_percentage BETWEEN 0 AND 100', name=op.f('ck_hot_deals_discount_percentage')),
    sa.CheckConstraint('price >= 0', name=op.f('ck_hot_deals_price')),
    sa.CheckConstraint('purchase_amount >= 0', name=op.f('ck_hot_deals_purchase_amount')),
    sa.ForeignKeyConstraint(['buyer_id'], ['users.id'], name=op.f('fk_hot_deals_buyer_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['buyer_order_id'], ['orders.id'], name=op.f('fk_hot_deals_buyer_order_id_orders'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['courier_id'], ['couriers.id'], name=op.f('fk_hot_deals_courier_id_couriers'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['original_order_id'], ['orders.id'], name=op.f('fk_hot_deals_original_order_id_orders'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_hot_deals')),
    sa.UniqueConstraint('old_import_id', name=op.f('uq_hot_deals_old_import_id'))
    )
    op.create_index('hot_deals_available', 'hot_deals', ['pickup_location'], unique=False, postgresql_using='gist', postgresql_where=sa.text("status = 'available'"), postgresql_ops={})
    op.create_index('ix_hot_deals_original_order_id', 'hot_deals', ['original_order_id'], unique=False)
    op.create_table('messages',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('order_id', sa.UUID(), nullable=False),
    sa.Column('sender_id', sa.UUID(), nullable=True),
    sa.Column('recipient_id', sa.UUID(), nullable=True),
    sa.Column('sender_role', sa.Text(), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('is_template', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('read_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('old_import_id', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("sender_role IN ('customer','courier')", name=op.f('ck_messages_sender_role')),
    sa.CheckConstraint('length(body) BETWEEN 1 AND 1000', name=op.f('ck_messages_body')),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], name=op.f('fk_messages_order_id_orders'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['recipient_id'], ['users.id'], name=op.f('fk_messages_recipient_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['sender_id'], ['users.id'], name=op.f('fk_messages_sender_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_messages')),
    sa.UniqueConstraint('old_import_id', name=op.f('uq_messages_old_import_id'))
    )
    op.create_index('ix_messages_order_id_created_at', 'messages', ['order_id', 'created_at'], unique=False)
    op.create_index('messages_unread', 'messages', ['recipient_id'], unique=False, postgresql_where=sa.text('read_at IS NULL'))
    op.create_table('no_response_cases',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('order_id', sa.UUID(), nullable=False),
    sa.Column('courier_id', sa.UUID(), nullable=True),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('purchase_amount', sa.Numeric(precision=10, scale=3), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('deadline_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('final_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resolution', sa.Text(), nullable=True),
    sa.Column('incident_counted', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('customer_answered_late', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('channels', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('messaging_status', sa.Text(), nullable=True),
    sa.Column('old_import_id', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("resolution IN ('customer_confirmed','courier_reached','resold','cancelled_kept','returned_to_shop','auto_closed','courier_cancelled_other','delivered','order_cancelled')", name=op.f('ck_no_response_cases_resolution')),
    sa.CheckConstraint("status IN ('waiting','expired','resolved')", name=op.f('ck_no_response_cases_status')),
    sa.ForeignKeyConstraint(['courier_id'], ['couriers.id'], name=op.f('fk_no_response_cases_courier_id_couriers'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], name=op.f('fk_no_response_cases_order_id_orders'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_no_response_cases')),
    sa.UniqueConstraint('old_import_id', name=op.f('uq_no_response_cases_old_import_id'))
    )
    op.create_index('due_cases', 'no_response_cases', ['deadline_at'], unique=False, postgresql_where=sa.text("status = 'waiting'"))
    op.create_index('ix_no_response_cases_order_id', 'no_response_cases', ['order_id'], unique=False)
    op.create_index('one_open_case_per_order', 'no_response_cases', ['order_id'], unique=True, postgresql_where=sa.text("status = 'waiting'"))
    op.create_table('notifications',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('order_id', sa.UUID(), nullable=True),
    sa.Column('type', sa.Text(), nullable=False),
    sa.Column('title_ar', sa.Text(), nullable=True),
    sa.Column('title_fr', sa.Text(), nullable=True),
    sa.Column('body_ar', sa.Text(), nullable=True),
    sa.Column('body_fr', sa.Text(), nullable=True),
    sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('read_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('old_import_id', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("type IN ('order_accepted','at_shop','purchased','on_the_way','delivered','order_cancelled','delivery_cancelled','delivery_delayed','eta_update','new_order','new_offer','new_message','emergency_contact','customer_responded','customer_no_response_final','order_confirmed','order_preparing','hot_deal_reserved','issue_reported','account_verified','account_rejected')", name=op.f('ck_notifications_type')),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], name=op.f('fk_notifications_order_id_orders'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_notifications_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_notifications')),
    sa.UniqueConstraint('old_import_id', name=op.f('uq_notifications_old_import_id'))
    )
    op.create_index('ix_notifications_order_id', 'notifications', ['order_id'], unique=False)
    op.create_index('ix_notifications_user_id_created_at', 'notifications', ['user_id', sa.literal_column('created_at DESC')], unique=False)
    op.create_index('notifications_unread', 'notifications', ['user_id'], unique=False, postgresql_where=sa.text('read_at IS NULL'))
    op.create_table('order_issues',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('order_id', sa.UUID(), nullable=False),
    sa.Column('reporter_id', sa.UUID(), nullable=True),
    sa.Column('issue_type', sa.Text(), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('photo_key', sa.Text(), nullable=True),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resolved_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], name=op.f('fk_order_issues_order_id_orders'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['reporter_id'], ['users.id'], name=op.f('fk_order_issues_reporter_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['resolved_by'], ['users.id'], name=op.f('fk_order_issues_resolved_by_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_order_issues'))
    )
    op.create_index('ix_order_issues_order_id', 'order_issues', ['order_id'], unique=False)
    op.create_table('order_offers',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('order_id', sa.UUID(), nullable=False),
    sa.Column('courier_id', sa.UUID(), nullable=False),
    sa.Column('proposed_fee', sa.Numeric(precision=10, scale=3), nullable=False),
    sa.Column('eta_minutes', sa.Integer(), nullable=True),
    sa.Column('distance_km', sa.Numeric(precision=6, scale=2), nullable=True),
    sa.Column('message', sa.Text(), nullable=True),
    sa.Column('courier_rating_snapshot', sa.Numeric(precision=3, scale=2), nullable=True),
    sa.Column('status', postgresql.ENUM(name='offer_status', create_type=False), server_default='pending', nullable=False),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('old_import_id', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('eta_minutes BETWEEN 0 AND 600', name=op.f('ck_order_offers_eta_minutes')),
    sa.CheckConstraint('length(message) <= 500', name=op.f('ck_order_offers_message')),
    sa.CheckConstraint('proposed_fee > 0 AND proposed_fee <= 200', name=op.f('ck_order_offers_proposed_fee')),
    sa.ForeignKeyConstraint(['courier_id'], ['couriers.id'], name=op.f('fk_order_offers_courier_id_couriers'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], name=op.f('fk_order_offers_order_id_orders'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_order_offers')),
    sa.UniqueConstraint('old_import_id', name=op.f('uq_order_offers_old_import_id'))
    )
    op.create_index('ix_order_offers_courier_status_created', 'order_offers', ['courier_id', 'status', sa.literal_column('created_at DESC')], unique=False)
    op.create_index('ix_order_offers_order_id', 'order_offers', ['order_id'], unique=False)
    op.create_index('one_accepted_offer', 'order_offers', ['order_id'], unique=True, postgresql_where=sa.text("status = 'accepted'"))
    op.create_index('one_live_offer_per_courier', 'order_offers', ['order_id', 'courier_id'], unique=True, postgresql_where=sa.text("status = 'pending'"))
    op.create_table('order_ratings',
    sa.Column('order_id', sa.UUID(), nullable=False),
    sa.Column('courier_id', sa.UUID(), nullable=False),
    sa.Column('rater_id', sa.UUID(), nullable=True),
    sa.Column('rating', sa.SmallInteger(), nullable=False),
    sa.Column('comment', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('rating BETWEEN 1 AND 5', name=op.f('ck_order_ratings_rating')),
    sa.ForeignKeyConstraint(['courier_id'], ['couriers.id'], name=op.f('fk_order_ratings_courier_id_couriers'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], name=op.f('fk_order_ratings_order_id_orders'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['rater_id'], ['users.id'], name=op.f('fk_order_ratings_rater_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('order_id', name=op.f('pk_order_ratings'))
    )
    op.create_index('ix_order_ratings_courier_id', 'order_ratings', ['courier_id'], unique=False)
    op.create_table('order_status_events',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('order_id', sa.UUID(), nullable=False),
    sa.Column('from_status', postgresql.ENUM(name='order_status', create_type=False), nullable=True),
    sa.Column('to_status', postgresql.ENUM(name='order_status', create_type=False), nullable=False),
    sa.Column('actor_user_id', sa.UUID(), nullable=True),
    sa.Column('source', sa.Text(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('cancelled_by', sa.Text(), nullable=True),
    sa.Column('location', Geography(geometry_type='POINT', srid=4326, dimension=2, spatial_index=False, from_text='ST_GeogFromText', name='geography'), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], name=op.f('fk_order_status_events_actor_user_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], name=op.f('fk_order_status_events_order_id_orders'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_order_status_events'))
    )
    op.create_index('ix_order_status_events_order_id_created_at', 'order_status_events', ['order_id', 'created_at'], unique=False)
    op.create_table('order_stops',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('order_id', sa.UUID(), nullable=False),
    sa.Column('seq', sa.SmallInteger(), nullable=False),
    sa.Column('shop_id', sa.UUID(), nullable=True),
    sa.Column('place_id', sa.BigInteger(), nullable=True),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('address', sa.Text(), nullable=True),
    sa.Column('phone', sa.Text(), nullable=True),
    sa.Column('governorate', sa.Text(), nullable=True),
    sa.Column('city', sa.Text(), nullable=True),
    sa.Column('location', Geography(geometry_type='POINT', srid=4326, dimension=2, spatial_index=False, from_text='ST_GeogFromText', name='geography'), nullable=True),
    sa.Column('items', sa.Text(), nullable=True),
    sa.Column('status', sa.Text(), server_default='pending', nullable=False),
    sa.Column('purchase_amount', sa.Numeric(precision=10, scale=3), nullable=True),
    sa.Column('receipt_key', sa.Text(), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('pending','en_route','at_shop','purchased','skipped')", name=op.f('ck_order_stops_status')),
    sa.CheckConstraint('purchase_amount >= 0 AND purchase_amount <= 2000', name=op.f('ck_order_stops_purchase_amount')),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], name=op.f('fk_order_stops_order_id_orders'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['place_id'], ['places.id'], name=op.f('fk_order_stops_place_id_places'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['shop_id'], ['shops.id'], name=op.f('fk_order_stops_shop_id_shops'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_order_stops')),
    sa.UniqueConstraint('order_id', 'seq', name=op.f('uq_order_stops_order_id_seq'))
    )
    op.create_table('order_tracking',
    sa.Column('order_id', sa.UUID(), nullable=False),
    sa.Column('courier_id', sa.UUID(), nullable=False),
    sa.Column('location', Geography(geometry_type='POINT', srid=4326, dimension=2, spatial_index=False, from_text='ST_GeogFromText', name='geography', nullable=False), nullable=False),
    sa.Column('recorded_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['courier_id'], ['couriers.id'], name=op.f('fk_order_tracking_courier_id_couriers')),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], name=op.f('fk_order_tracking_order_id_orders'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('order_id', name=op.f('pk_order_tracking'))
    )
    op.create_table('outbound_messages',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('channel', sa.Text(), nullable=False),
    sa.Column('purpose', sa.Text(), nullable=False),
    sa.Column('template_name', sa.Text(), nullable=True),
    sa.Column('lang', sa.Text(), nullable=True),
    sa.Column('params', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False),
    sa.Column('to_e164', sa.Text(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=True),
    sa.Column('order_id', sa.UUID(), nullable=True),
    sa.Column('notification_id', sa.UUID(), nullable=True),
    sa.Column('idempotency_key', sa.Text(), nullable=True),
    sa.Column('critical', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('provider', sa.Text(), nullable=True),
    sa.Column('provider_message_id', sa.Text(), nullable=True),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('error_code', sa.Text(), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('read_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('failed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('fallback_deadline_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('fallback_status', sa.Text(), nullable=True),
    sa.Column('parent_id', sa.UUID(), nullable=True),
    sa.Column('old_import_id', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("channel IN ('whatsapp','sms')", name=op.f('ck_outbound_messages_channel')),
    sa.ForeignKeyConstraint(['notification_id'], ['notifications.id'], name=op.f('fk_outbound_messages_notification_id_notifications'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], name=op.f('fk_outbound_messages_order_id_orders'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['parent_id'], ['outbound_messages.id'], name=op.f('fk_outbound_messages_parent_id_outbound_messages')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_outbound_messages_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_outbound_messages')),
    sa.UniqueConstraint('idempotency_key', name=op.f('uq_outbound_messages_idempotency_key')),
    sa.UniqueConstraint('old_import_id', name=op.f('uq_outbound_messages_old_import_id'))
    )
    op.create_index('ix_outbound_messages_due', 'outbound_messages', ['next_attempt_at'], unique=False, postgresql_where=sa.text("status IN ('retry_pending','queued')"))
    op.create_index('ix_outbound_messages_order_id', 'outbound_messages', ['order_id'], unique=False)
    op.create_index('ix_outbound_messages_provider_message_id', 'outbound_messages', ['provider_message_id'], unique=False)
    op.create_index('ix_outbound_messages_to_e164_created_at', 'outbound_messages', ['to_e164', 'created_at'], unique=False)
    op.create_table('push_deliveries',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=True),
    sa.Column('device_token_id', sa.UUID(), nullable=True),
    sa.Column('notification_id', sa.UUID(), nullable=True),
    sa.Column('provider', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("provider IN ('fcm','log')", name=op.f('ck_push_deliveries_provider')),
    sa.CheckConstraint("status IN ('sent','failed','invalid_token')", name=op.f('ck_push_deliveries_status')),
    sa.ForeignKeyConstraint(['device_token_id'], ['device_tokens.id'], name=op.f('fk_push_deliveries_device_token_id_device_tokens'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['notification_id'], ['notifications.id'], name=op.f('fk_push_deliveries_notification_id_notifications'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_push_deliveries_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_push_deliveries'))
    )
    op.create_index('ix_push_deliveries_user_id_created_at', 'push_deliveries', ['user_id', 'created_at'], unique=False)

    for name, source, referent, cols, ondelete in CIRCULAR_FKS:
        op.create_foreign_key(name, source, referent, cols, ["id"], ondelete=ondelete)
    for table in UPDATED_AT_TABLES:
        op.execute(
            f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )
    op.execute(COURIER_STATS_SQL)
    op.execute(CUSTOMER_STATS_SQL)


def downgrade() -> None:
    op.execute("DROP VIEW customer_stats")
    op.execute("DROP VIEW courier_stats")
    for name, source, _referent, _cols, _ondelete in CIRCULAR_FKS:
        op.drop_constraint(name, source, type_="foreignkey")
    op.drop_index('ix_push_deliveries_user_id_created_at', table_name='push_deliveries')
    op.drop_table('push_deliveries')
    op.drop_index('ix_outbound_messages_to_e164_created_at', table_name='outbound_messages')
    op.drop_index('ix_outbound_messages_provider_message_id', table_name='outbound_messages')
    op.drop_index('ix_outbound_messages_order_id', table_name='outbound_messages')
    op.drop_index('ix_outbound_messages_due', table_name='outbound_messages', postgresql_where=sa.text("status IN ('retry_pending','queued')"))
    op.drop_table('outbound_messages')
    op.drop_table('order_tracking')
    op.drop_table('order_stops')
    op.drop_index('ix_order_status_events_order_id_created_at', table_name='order_status_events')
    op.drop_table('order_status_events')
    op.drop_index('ix_order_ratings_courier_id', table_name='order_ratings')
    op.drop_table('order_ratings')
    op.drop_index('one_live_offer_per_courier', table_name='order_offers', postgresql_where=sa.text("status = 'pending'"))
    op.drop_index('one_accepted_offer', table_name='order_offers', postgresql_where=sa.text("status = 'accepted'"))
    op.drop_index('ix_order_offers_order_id', table_name='order_offers')
    op.drop_index('ix_order_offers_courier_status_created', table_name='order_offers')
    op.drop_table('order_offers')
    op.drop_index('ix_order_issues_order_id', table_name='order_issues')
    op.drop_table('order_issues')
    op.drop_index('notifications_unread', table_name='notifications', postgresql_where=sa.text('read_at IS NULL'))
    op.drop_index('ix_notifications_user_id_created_at', table_name='notifications')
    op.drop_index('ix_notifications_order_id', table_name='notifications')
    op.drop_table('notifications')
    op.drop_index('one_open_case_per_order', table_name='no_response_cases', postgresql_where=sa.text("status = 'waiting'"))
    op.drop_index('ix_no_response_cases_order_id', table_name='no_response_cases')
    op.drop_index('due_cases', table_name='no_response_cases', postgresql_where=sa.text("status = 'waiting'"))
    op.drop_table('no_response_cases')
    op.drop_index('messages_unread', table_name='messages', postgresql_where=sa.text('read_at IS NULL'))
    op.drop_index('ix_messages_order_id_created_at', table_name='messages')
    op.drop_table('messages')
    op.drop_index('ix_hot_deals_original_order_id', table_name='hot_deals')
    op.drop_index('hot_deals_available', table_name='hot_deals', postgresql_using='gist', postgresql_where=sa.text("status = 'available'"))
    op.drop_table('hot_deals')
    op.drop_index('ix_courier_ledger_entries_courier_id_created_at', table_name='courier_ledger_entries')
    op.drop_table('courier_ledger_entries')
    op.drop_index('one_review_per_user_shop', table_name='shop_reviews', postgresql_where=sa.text('shop_id IS NOT NULL'))
    op.drop_index('one_review_per_user_place', table_name='shop_reviews', postgresql_where=sa.text('place_id IS NOT NULL'))
    op.drop_index('ix_shop_reviews_shop_id', table_name='shop_reviews')
    op.drop_index('ix_shop_reviews_place_id', table_name='shop_reviews')
    op.drop_table('shop_reviews')
    op.drop_index(op.f('ix_shop_menu_items_shop_id'), table_name='shop_menu_items')
    op.drop_table('shop_menu_items')
    op.drop_index('orders_open_geo', table_name='orders', postgresql_using='gist', postgresql_where=sa.text("status IN ('pending','offers_received')"))
    op.drop_index('orders_customer', table_name='orders')
    op.drop_index('orders_courier', table_name='orders', postgresql_where=sa.text('courier_id IS NOT NULL'))
    op.drop_index('orders_active', table_name='orders', postgresql_where=sa.text("status NOT IN ('delivered','cancelled')"))
    op.drop_index('ix_orders_preferred_courier_id', table_name='orders')
    op.drop_table('orders')
    op.drop_table('courier_statements')
    op.drop_index('one_default_address', table_name='user_addresses', postgresql_where=sa.text('is_default'))
    op.drop_table('user_addresses')
    op.drop_index('shops_geo', table_name='shops', postgresql_using='gist')
    op.drop_index('ix_shops_review_status', table_name='shops')
    op.drop_index('ix_shops_proposed_by', table_name='shops')
    op.drop_table('shops')
    op.drop_index(op.f('ix_refresh_tokens_user_id'), table_name='refresh_tokens')
    op.drop_index('ix_refresh_tokens_family', table_name='refresh_tokens')
    op.drop_table('refresh_tokens')
    op.drop_index('ix_files_owner_id', table_name='files')
    op.drop_table('files')
    op.drop_index('ix_email_codes_open', table_name='email_codes', postgresql_where=sa.text('used_at IS NULL'))
    op.drop_table('email_codes')
    op.drop_index('ix_device_tokens_active_user', table_name='device_tokens', postgresql_where=sa.text('is_active'))
    op.drop_table('device_tokens')
    op.drop_index('couriers_dispatch', table_name='couriers', postgresql_using='gist', postgresql_where=sa.text("is_online AND verification = 'verified'"))
    op.drop_table('couriers')
    op.drop_index('ix_audit_log_entity_entity_id_created_at', table_name='audit_log')
    op.drop_table('audit_log')
    op.drop_table('app_settings')
    op.drop_index('ix_users_role', table_name='users')
    op.drop_index('ix_users_referred_by_courier_id', table_name='users')
    op.drop_table('users')
    op.drop_index('places_name', table_name='places', postgresql_using='gin', postgresql_ops={'name_norm': 'gin_trgm_ops'})
    op.drop_index('places_geo', table_name='places', postgresql_using='gist')
    op.drop_index('places_cat', table_name='places')
    op.drop_table('places')
    op.execute("DROP FUNCTION set_updated_at()")
    for name in reversed(ENUMS):
        postgresql.ENUM(name=name).drop(op.get_bind())
