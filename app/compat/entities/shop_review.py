"""ShopReview: reviews of a shop or a place (base44/entities/ShopReview.jsonc, ShopDetails).

The front keys a review by `shop_osm_id`, which is 'shop:<Shop id>' (map), an OSM id,
or 'place:<name>@<lat>,<lng>' (search lists without an id). The key is stored as sent
(`target_key`, reads filter on it) and resolved when possible to the shop / place
(`shop_id` / `place_id`) so the name follows the catalogue.

Read: everybody signed in. Create: any signed-in user, for himself (`user_id` = the
caller's User id when sent — Base44 rule), rating 1-5, photos = our public uploads,
one review per user and key (409 already_reviewed). `user_name` / `shop_name` are
derived (author / shop), not taken from the body. Delete: the author or an admin.
Update: nobody from the front (Base44: admins; unused) → 403.
"""

import re
import uuid
from typing import Any

from sqlalchemy import Text, func, literal, select, text, true
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.compat.payload import coerce_payload
from app.compat.registry import EntityDef, LegacyField, register
from app.errors import ApiError
from app.models import Place, Shop, ShopReview, User
from app.security.deps import CurrentUser
from app.services.places import point
from app.services.shops import key_from_public_url, public_prefix

reviews = ShopReview.__table__
author = User.__table__.alias("review_author")
shops = Shop.__table__.alias("review_shop")
places = Place.__table__.alias("review_place")

MAX_KEY = 300
MAX_COMMENT = 2000
MAX_PHOTOS = 10
PLACE_KEY_RADIUS_M = 25
_PLACE_KEY = re.compile(r"^place:(.+)@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)$")


def _photo_urls() -> Any:
    """Stored keys → public URLs, in order; [] without photos."""
    photos = func.unnest(reviews.c.photo_keys).table_valued("key", with_ordinality="n").render_derived()
    urls = func.array_agg(
        aggregate_order_by(func.concat(literal(public_prefix(), Text), photos.c.key), photos.c.n)
    )
    return select(func.coalesce(urls, text("'{}'::text[]"))).select_from(photos).scalar_subquery()


FIELDS: dict[str, LegacyField] = {
    "shop_osm_id": LegacyField(reviews.c.target_key, "string"),
    "shop_name": LegacyField(func.coalesce(shops.c.name, places.c.name), "string"),
    "user_id": LegacyField(reviews.c.user_id, "id"),
    "user_name": LegacyField(author.c.full_name, "string"),
    "rating": LegacyField(reviews.c.rating, "integer"),
    "comment": LegacyField(reviews.c.comment, "string"),
    "photo_urls": LegacyField(_photo_urls(), "array"),
}
CREATE_FIELDS = frozenset({"shop_osm_id", "rating", "comment", "photo_urls"})


def _bad(message: str) -> ApiError:
    return ApiError(400, "validation_error", message)


async def _resolve(
    session: AsyncSession, actor: CurrentUser, key: str
) -> tuple[uuid.UUID | None, int | None]:
    """(shop_id, place_id) of a legacy key; (None, None) when it can't be resolved."""
    if key.startswith("shop:"):
        try:
            shop_id = uuid.UUID(key[5:])
        except ValueError as exc:
            raise ApiError(404, "not_found", "Shop not found") from exc
        shop = await session.get(Shop, shop_id)
        visible = shop is not None and (
            shop.review_status == "approved" or shop.proposed_by == actor.id or actor.is_admin
        )
        if not visible:
            raise ApiError(404, "not_found", "Shop not found")
        return shop_id, None
    match = _PLACE_KEY.match(key)
    if match:
        name, lat, lng = match.group(1), float(match.group(2)), float(match.group(3))
        folded = func.lower(func.regexp_replace(func.btrim(Place.name), r"\s+", " ", "g"))
        place_id = (
            await session.execute(
                select(Place.id)
                .where(
                    folded == name,
                    func.ST_DWithin(Place.location, point(lat, lng), PLACE_KEY_RADIUS_M, False),
                )
                .order_by(func.ST_Distance(Place.location, point(lat, lng), False))
                .limit(1)
            )
        ).scalar_one_or_none()
        return None, place_id
    place_id = (await session.execute(select(Place.id).where(Place.osm_id == key))).scalar_one_or_none()
    if place_id is not None:
        return None, place_id
    shop = (await session.execute(select(Shop).where(Shop.osm_id == key))).scalar_one_or_none()
    if shop is not None and (
        shop.review_status == "approved" or shop.proposed_by == actor.id or actor.is_admin
    ):
        return shop.id, None
    return None, None


def _photo_keys(value: Any) -> list[str]:
    if value is None:
        return []
    if len(value) > MAX_PHOTOS:
        raise _bad(f"photo_urls: at most {MAX_PHOTOS} photos")
    keys = []
    for url in value:
        key = key_from_public_url(url)
        if key is None:
            raise _bad("photo_urls: only photos uploaded to this app are accepted")
        keys.append(key)
    return keys


async def create(session: AsyncSession, actor: CurrentUser, data: dict[str, Any]) -> str:
    if "user_id" in data and str(data["user_id"]) != str(actor.id):
        raise ApiError(403, "permission_denied", "Permission denied for create operation on ShopReview")
    values = coerce_payload(ENTITY, data, CREATE_FIELDS)
    key = (values.get("shop_osm_id") or "").strip()
    if not key or len(key) > MAX_KEY:
        raise _bad(f"shop_osm_id: required (at most {MAX_KEY} characters)")
    rating = values.get("rating")
    if rating not in (1, 2, 3, 4, 5):
        raise _bad("rating: expected 1 to 5")
    comment = (values.get("comment") or "").strip()[:MAX_COMMENT] or None
    photo_keys = _photo_keys(values.get("photo_urls"))
    duplicate = await session.execute(
        select(ShopReview.id).where(ShopReview.user_id == actor.id, ShopReview.target_key == key)
    )
    if duplicate.first() is not None:
        raise ApiError(409, "already_reviewed", "You already reviewed this shop")
    shop_id, place_id = await _resolve(session, actor, key)
    review = ShopReview(
        target_key=key,
        shop_id=shop_id,
        place_id=place_id,
        user_id=actor.id,
        rating=rating,
        comment=comment,
        photo_keys=photo_keys,
    )
    try:
        async with session.begin_nested():
            session.add(review)
            await session.flush()
    except IntegrityError as exc:  # the same shop / place reviewed through another key
        raise ApiError(409, "already_reviewed", "You already reviewed this shop") from exc
    return str(review.id)


async def delete(session: AsyncSession, actor: CurrentUser, doc_id: str) -> list[uuid.UUID]:
    try:
        review_id = uuid.UUID(doc_id)
    except ValueError as exc:
        raise ApiError(404, "not_found", "ShopReview not found") from exc
    review = await session.get(ShopReview, review_id, with_for_update=True)
    if review is None:
        raise ApiError(404, "not_found", "ShopReview not found")
    if review.user_id != actor.id and not actor.is_admin:
        raise ApiError(403, "permission_denied", "Permission denied for delete operation on ShopReview")
    await session.delete(review)
    await session.flush()
    return [review.user_id, actor.id]


ENTITY = register(
    EntityDef(
        name="ShopReview",
        source=reviews.join(author, author.c.id == reviews.c.user_id)
        .outerjoin(shops, shops.c.id == reviews.c.shop_id)
        .outerjoin(places, places.c.id == reviews.c.place_id),
        id_expr=reviews.c.id,
        id_type="uuid",
        created_expr=reviews.c.created_at,
        updated_expr=reviews.c.updated_at,
        fields=FIELDS,
        read_policy=lambda _user: true(),
        create=create,
        delete=delete,
    )
)
