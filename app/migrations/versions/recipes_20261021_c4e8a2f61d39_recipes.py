"""recipes (21/10/2026, Recettes first version, visible to the test accounts only)

- ingredients: the shared ingredient dictionary (names fr / ar, buying unit kg | l | piece | botte, weight of
  a piece / bunch for the couffin, aisle, « nearly always at home »); no price column;
- recipes + recipe_lines: imported (scripts/import_recipes.py, importAdminRecipes), draft until published;
  cuisine / country / folded titles for the filters and the name search; a line without a dictionary
  reference (ingredient_id NULL, new_key) keeps its own names and stays without a price;
- recipe_orders: what a recipe order (a normal Achats order) was made of;
- recipe_price_cache: the ingredient prices read from ODS (GET /api/v1/market/recipe-prices), per country.

Additive only: no existing table or row changes. The switch « Recettes publiques »
(app_settings['recipes'].public_allowed) is absent = off. Downgrade drops the five tables.

Revision ID: c4e8a2f61d39
Revises: b7c3e1d9a5f2
Create Date: 2026-10-21 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'c4e8a2f61d39'
down_revision: str | None = 'b7c3e1d9a5f2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPDATED_AT = ("ingredients", "recipes")


def upgrade() -> None:
    op.create_table('ingredients',
    sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('key', sa.Text(), nullable=False),
    sa.Column('name_fr', sa.Text(), nullable=False),
    sa.Column('name_ar', sa.Text(), nullable=False),
    sa.Column('unit', sa.Text(), nullable=False),
    sa.Column('piece_kg', sa.Numeric(precision=8, scale=4), nullable=True),
    sa.Column('density', sa.Numeric(precision=6, scale=3), nullable=True),
    sa.Column('aisle', sa.Text(), nullable=False),
    sa.Column('pantry', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("aisle IN ('marche','boucherie','poissonnerie','cremerie','epicerie','boulangerie')", name=op.f('ck_ingredients_aisle')),
    sa.CheckConstraint("unit IN ('kg','l','piece','botte')", name=op.f('ck_ingredients_unit')),
    sa.CheckConstraint('density IS NULL OR density > 0', name=op.f('ck_ingredients_density')),
    sa.CheckConstraint('piece_kg IS NULL OR piece_kg > 0', name=op.f('ck_ingredients_piece_kg')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ingredients')),
    sa.UniqueConstraint('key', name=op.f('uq_ingredients_key'))
    )
    op.create_table('recipe_price_cache',
    sa.Column('country', sa.Text(), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('tried_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('country', name=op.f('pk_recipe_price_cache'))
    )
    op.create_table('recipes',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('slug', sa.Text(), nullable=False),
    sa.Column('title_fr', sa.Text(), nullable=False),
    sa.Column('title_ar', sa.Text(), nullable=False),
    sa.Column('title_local', sa.Text(), nullable=True),
    sa.Column('cuisine', sa.Text(), nullable=True),
    sa.Column('country', sa.Text(), nullable=True),
    sa.Column('category', sa.Text(), nullable=False),
    sa.Column('search_norm', sa.Text(), server_default='', nullable=False),
    sa.Column('time_min', sa.SmallInteger(), nullable=True),
    sa.Column('difficulty', sa.Text(), nullable=True),
    sa.Column('base_servings', sa.SmallInteger(), server_default='4', nullable=False),
    sa.Column('tags', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False),
    sa.Column('intro_fr', sa.Text(), nullable=True),
    sa.Column('intro_ar', sa.Text(), nullable=True),
    sa.Column('tip_fr', sa.Text(), nullable=True),
    sa.Column('tip_ar', sa.Text(), nullable=True),
    sa.Column('sources', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('steps_fr', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('steps_ar', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('status', sa.Text(), server_default='draft', nullable=False),
    sa.Column('content_hash', sa.Text(), nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('published_by', sa.UUID(), nullable=True),
    sa.Column('imported_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(status = 'published') = (published_at IS NOT NULL)", name=op.f('ck_recipes_published_at')),
    sa.CheckConstraint("status IN ('draft','published')", name=op.f('ck_recipes_status')),
    sa.CheckConstraint('base_servings BETWEEN 1 AND 100', name=op.f('ck_recipes_base_servings')),
    sa.CheckConstraint('time_min IS NULL OR time_min BETWEEN 1 AND 1440', name=op.f('ck_recipes_time_min')),
    sa.ForeignKeyConstraint(['published_by'], ['users.id'], name=op.f('fk_recipes_published_by_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_recipes')),
    sa.UniqueConstraint('slug', name=op.f('uq_recipes_slug'))
    )
    op.create_index('ix_recipes_cuisine', 'recipes', ['cuisine'], unique=False)
    op.create_index('ix_recipes_status', 'recipes', ['status'], unique=False)
    op.create_table('recipe_lines',
    sa.Column('recipe_id', sa.UUID(), nullable=False),
    sa.Column('position', sa.SmallInteger(), nullable=False),
    sa.Column('ingredient_id', sa.Integer(), nullable=True),
    sa.Column('new_key', sa.Text(), nullable=True),
    sa.Column('name_fr', sa.Text(), nullable=False),
    sa.Column('name_ar', sa.Text(), nullable=False),
    sa.Column('aisle', sa.Text(), nullable=True),
    sa.Column('piece_kg', sa.Numeric(precision=8, scale=4), nullable=True),
    sa.Column('pantry', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('qty', sa.Numeric(precision=10, scale=4), nullable=False),
    sa.Column('unit', sa.Text(), nullable=False),
    sa.Column('optional', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('note_fr', sa.Text(), nullable=True),
    sa.Column('note_ar', sa.Text(), nullable=True),
    sa.CheckConstraint("aisle IS NULL OR aisle IN ('marche','boucherie','poissonnerie','cremerie','epicerie','boulangerie')", name=op.f('ck_recipe_lines_aisle')),
    sa.CheckConstraint("unit IN ('kg','l','piece','botte','cas','cac','pincee')", name=op.f('ck_recipe_lines_unit')),
    sa.CheckConstraint('ingredient_id IS NOT NULL OR new_key IS NOT NULL', name=op.f('ck_recipe_lines_ingredient')),
    sa.CheckConstraint('qty > 0 AND qty <= 100', name=op.f('ck_recipe_lines_qty')),
    sa.ForeignKeyConstraint(['ingredient_id'], ['ingredients.id'], name=op.f('fk_recipe_lines_ingredient_id_ingredients'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['recipe_id'], ['recipes.id'], name=op.f('fk_recipe_lines_recipe_id_recipes'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('recipe_id', 'position', name='pk_recipe_lines')
    )
    op.create_index('ix_recipe_lines_ingredient_id', 'recipe_lines', ['ingredient_id'], unique=False)
    op.create_table('recipe_orders',
    sa.Column('order_id', sa.UUID(), nullable=False),
    sa.Column('customer_id', sa.UUID(), nullable=True),
    sa.Column('recipes', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('at_home', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('lines', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('cost', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('test', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['customer_id'], ['users.id'], name=op.f('fk_recipe_orders_customer_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], name=op.f('fk_recipe_orders_order_id_orders'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('order_id', name=op.f('pk_recipe_orders'))
    )
    op.create_index('ix_recipe_orders_customer_id', 'recipe_orders', ['customer_id'], unique=False)
    for table in UPDATED_AT:
        op.execute(
            f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )


def downgrade() -> None:
    for table in UPDATED_AT:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_updated_at ON {table}")
    op.drop_index('ix_recipe_orders_customer_id', table_name='recipe_orders')
    op.drop_table('recipe_orders')
    op.drop_index('ix_recipe_lines_ingredient_id', table_name='recipe_lines')
    op.drop_table('recipe_lines')
    op.drop_index('ix_recipes_status', table_name='recipes')
    op.drop_index('ix_recipes_cuisine', table_name='recipes')
    op.drop_table('recipes')
    op.drop_table('recipe_price_cache')
    op.drop_table('ingredients')
