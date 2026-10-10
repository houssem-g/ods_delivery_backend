"""PlaceIndex: the OSM places cache (`places`), read-only, admins only
(every operation admin-only; the app reads it through
searchPlaces / searchByBbox / geocodeAddress, and refreshOsmIndex writes it).

`id` is the bigint key as text; `quality_score` is answered on the legacy 0-1 scale.
"""

from sqlalchemy import Text, cast, false, true

from app.compat.registry import EntityDef, LegacyField, register
from app.models import Place
from app.services.places import lat_expr, lng_expr, quality_fraction

places = Place.__table__

ENTITY = register(
    EntityDef(
        name="PlaceIndex",
        source=places,
        id_expr=cast(places.c.id, Text),
        id_type="text",
        created_expr=places.c.created_at,
        updated_expr=places.c.updated_at,
        fields={
            "osm_id": LegacyField(places.c.osm_id, "string"),
            "name": LegacyField(places.c.name, "string"),
            "name_norm": LegacyField(places.c.name_norm, "string"),
            "address": LegacyField(places.c.address, "string"),
            "city": LegacyField(places.c.city, "string"),
            "governorate": LegacyField(places.c.governorate, "string"),
            "category": LegacyField(places.c.category, "string"),
            "lat": LegacyField(lat_expr(places.c.location), "number"),
            "lng": LegacyField(lng_expr(places.c.location), "number"),
            "phone": LegacyField(places.c.phone, "string"),
            "source": LegacyField(places.c.source, "string"),
            "opening_hours": LegacyField(places.c.opening_hours, "string"),
            "source_ts": LegacyField(places.c.source_ts, "datetime"),
            "quality_score": LegacyField(quality_fraction(places.c.quality_score), "number"),
        },
        read_policy=lambda user: true() if user.is_admin else false(),
    )
)
