"""Recettes (21/10/2026, first version, visible to the test accounts only): « je choisis une recette, je dis
pour combien de personnes, je coche ce que j'ai déjà, et un livreur m'apporte le reste ».

- `ingredients`: the shared ingredient dictionary (id = the referential's id, ods-audit
  recettes-delivery-2110/mesure/referentiel_ingredients.csv): names fr / ar, buying unit (kg, l, piece,
  botte), the estimated weight of a piece / bunch (the couffin's weight, never a price), the aisle, and
  whether it is nearly always at home (pre-ticked « j'ai déjà »). No price column, ever.
- `recipes` + `recipe_lines`: imported from recettes.json (idempotent, app/services/recipes_import.py),
  draft until an admin publishes; a changed recipe goes back to draft.
- `recipe_orders`: what a recipe order was made of (the order itself is a normal Achats order).
- `recipe_price_cache`: the ingredient prices read from ODS (app/services/recipe_prices.py), kept so a
  slow or absent ODS never blocks the screen (no cost shown then, never an invented one).
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at, updated_at, uuid_pk

INGREDIENT_UNITS = ("kg", "l", "piece", "botte")
# a recipe line may also say « c. à s. » / « c. à c. » (spoons): for what is at home, never bought as such
LINE_UNITS = ("kg", "l", "piece", "botte", "cas", "cac", "pincee")
AISLES = ("marche", "boucherie", "poissonnerie", "cremerie", "epicerie", "boulangerie")
RECIPE_STATUSES = ("draft", "published")
# what `base_servings` counts: people (a dish), pieces (30 cornes de gazelle), portions (a spice mix)
SERVINGS_UNITS = ("people", "pieces", "portions")


def _in(values: tuple[str, ...]) -> str:
    return ",".join(f"'{v}'" for v in values)


class Ingredient(Base):
    __tablename__ = "ingredients"
    __table_args__ = (
        CheckConstraint(f"unit IN ({_in(INGREDIENT_UNITS)})", name="unit"),
        CheckConstraint(f"aisle IN ({_in(AISLES)})", name="aisle"),
        CheckConstraint("piece_kg IS NULL OR piece_kg > 0", name="piece_kg"),
        CheckConstraint("density IS NULL OR density > 0", name="density"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name_fr: Mapped[str] = mapped_column(Text, nullable=False)
    name_ar: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str] = mapped_column(Text, nullable=False)
    piece_kg: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    density: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    aisle: Mapped[str] = mapped_column(Text, nullable=False)
    pantry: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class Recipe(Base):
    __tablename__ = "recipes"
    __table_args__ = (
        CheckConstraint(f"status IN ({_in(RECIPE_STATUSES)})", name="status"),
        CheckConstraint("base_servings BETWEEN 1 AND 100", name="base_servings"),
        CheckConstraint(f"servings_unit IN ({_in(SERVINGS_UNITS)})", name="servings_unit"),
        CheckConstraint("time_min IS NULL OR time_min BETWEEN 1 AND 1440", name="time_min"),
        CheckConstraint("(status = 'published') = (published_at IS NOT NULL)", name="published_at"),
        Index("ix_recipes_status", "status"),
        Index("ix_recipes_cuisine", "cuisine"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    title_fr: Mapped[str] = mapped_column(Text, nullable=False)
    title_ar: Mapped[str] = mapped_column(Text, nullable=False)
    title_local: Mapped[str | None] = mapped_column(Text)
    # « tunisienne », « marocaine », « libanaise », « française »… and the country, as the files say them
    cuisine: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    # titles fr / ar / local folded (no accent, no case, Arabic letters unified): the name search
    search_norm: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    time_min: Mapped[int | None] = mapped_column(SmallInteger)
    difficulty: Mapped[str | None] = mapped_column(Text)
    base_servings: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="4")
    servings_unit: Mapped[str] = mapped_column(Text, nullable=False, server_default="people")
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    intro_fr: Mapped[str | None] = mapped_column(Text)
    intro_ar: Mapped[str | None] = mapped_column(Text)
    tip_fr: Mapped[str | None] = mapped_column(Text)
    tip_ar: Mapped[str | None] = mapped_column(Text)
    sources: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    steps_fr: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    steps_ar: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class RecipeLine(Base):
    __tablename__ = "recipe_lines"
    __table_args__ = (
        PrimaryKeyConstraint("recipe_id", "position", name="pk_recipe_lines"),
        CheckConstraint(f"unit IN ({_in(LINE_UNITS)})", name="unit"),
        CheckConstraint("qty > 0 AND qty <= 100", name="qty"),
        CheckConstraint("ingredient_id IS NOT NULL OR new_key IS NOT NULL", name="ingredient"),
        CheckConstraint(f"aisle IS NULL OR aisle IN ({_in(AISLES)})", name="aisle"),
        Index("ix_recipe_lines_ingredient_id", "ingredient_id"),
    )

    recipe_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recipes.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # NULL = an ingredient not in the dictionary yet (`ingredient_new` of the files): kept with its own names,
    # unit and aisle, always WITHOUT a price (never blocking the shopping list)
    ingredient_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("ingredients.id", ondelete="RESTRICT")
    )
    new_key: Mapped[str | None] = mapped_column(Text)
    name_fr: Mapped[str] = mapped_column(Text, nullable=False)
    name_ar: Mapped[str] = mapped_column(Text, nullable=False)
    aisle: Mapped[str | None] = mapped_column(Text)
    piece_kg: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    # « placard » of the line: ticked « j'ai déjà » by default (salt, oil, spices…)
    pantry: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    qty: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    unit: Mapped[str] = mapped_column(Text, nullable=False)
    optional: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    note_fr: Mapped[str | None] = mapped_column(Text)
    note_ar: Mapped[str | None] = mapped_column(Text)


class RecipeOrder(Base):
    """The recipes, the « j'ai déjà » and the list a recipe order was made of (counting, support)."""

    __tablename__ = "recipe_orders"
    __table_args__ = (Index("ix_recipe_orders_customer_id", "customer_id"),)

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), primary_key=True
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    recipes: Mapped[list] = mapped_column(JSONB, nullable=False)
    at_home: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    lines: Mapped[list] = mapped_column(JSONB, nullable=False)
    cost: Mapped[dict | None] = mapped_column(JSONB)
    test: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = created_at()


class RecipePriceCache(Base):
    """One row per country: the ingredient prices ODS gave last (payload), and when."""

    __tablename__ = "recipe_price_cache"

    country: Mapped[str] = mapped_column(Text, primary_key=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    tried_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
