# Données de départ — prévision de gains des livreurs (06/10/2026)

La production ODS Delivery n'a pas encore d'historique : ces données servent d'**a priori prudents**,
corrigés ensuite par chaque vraie commande (approche bayésienne).

| Fichier | Contenu | Source | Limites |
|---|---|---|---|
| `weather_sousse_hourly.csv` | Pluie (mm) et température, heure par heure, 01/01/2024 → 04/10/2026, Sousse | Open-Meteo Archive API (libre, CC BY 4.0) | Grille ~10 km ; prévisions à récupérer de la même façon au moment du calcul |
| `calendar_tn.json` (moved to app/services/data/ so it ships in the image) | Jours fériés 2027, Ramadan / Aïd 2027 (estimés) | publicholidays.africa, La Presse, Tuniscope, AllAfrica | Dates religieuses à confirmer ; **examens de l'Université de Sousse : à compléter** |
| `ins_rgph2024_sousse.pdf` | Population du gouvernorat : 762 281 habitants (RGPH 2024) | INS | Pas de détail par délégation ni par quartier dans la publication |
| `places_sousse_urbain.json` | 445 lieux (restaurants, supermarchés, pharmacies, cafés…) dans Sousse ville | Export OSM de l'ancien PlaceIndex (sauvegarde du 28/09) | Couverture OSM incomplète (seulement 12 cafés) ; à rafraîchir via Overpass (saturé le 06/10) |

Données à venir (les plus importantes) :
- réponses aux questions d'inscription (livreurs, invitations, clients) ;
- caisse ODS du café de Houssem à Sousse dès son ouverture (heures de pointe réelles) ;
- chaque commande ODS Delivery.

Règle : aucune donnée inventée. Ce qui manque reste « À COMPLÉTER ».
