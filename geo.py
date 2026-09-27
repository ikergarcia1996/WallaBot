"""Tiny offline geocoding for Spanish provinces / regions.

Used to turn a user-typed place ("País Vasco", "Bizkaia", "Bilbao") into
approximate coordinates so searches can be centered near the user and filtered to
listings they can actually get (shipped, or close enough to pick up). Deterministic
and offline — no external geocoding service needed.
"""

import math
import unicodedata

# Province / region name -> (latitude, longitude) of its capital (approx).
# Keys are looked up accent- and case-insensitively (see _normalize).
PROVINCES = {
    "a coruna": (43.36, -8.41), "coruna": (43.36, -8.41),
    "alava": (42.85, -2.67), "araba": (42.85, -2.67), "vitoria": (42.85, -2.67),
    "albacete": (38.99, -1.86),
    "alicante": (38.35, -0.48), "alacant": (38.35, -0.48),
    "almeria": (36.84, -2.46),
    "asturias": (43.36, -5.84), "oviedo": (43.36, -5.84), "gijon": (43.54, -5.66),
    "avila": (40.66, -4.70),
    "badajoz": (38.88, -6.97),
    "barcelona": (41.39, 2.17),
    "bizkaia": (43.26, -2.93), "vizcaya": (43.26, -2.93), "bilbao": (43.26, -2.93),
    "burgos": (42.34, -3.70),
    "caceres": (39.47, -6.37),
    "cadiz": (36.53, -6.29),
    "cantabria": (43.46, -3.80), "santander": (43.46, -3.80),
    "castellon": (39.99, -0.04), "castello": (39.99, -0.04),
    "ciudad real": (38.99, -3.93),
    "cordoba": (37.89, -4.78),
    "cuenca": (40.07, -2.13),
    "gipuzkoa": (43.32, -1.98), "guipuzcoa": (43.32, -1.98),
    "san sebastian": (43.32, -1.98), "donostia": (43.32, -1.98),
    "girona": (41.98, 2.82), "gerona": (41.98, 2.82),
    "granada": (37.18, -3.60),
    "guadalajara": (40.63, -3.16),
    "huelva": (37.26, -6.95),
    "huesca": (42.13, -0.41),
    "jaen": (37.77, -3.79),
    "la rioja": (42.46, -2.45), "rioja": (42.46, -2.45), "logrono": (42.46, -2.45),
    "las palmas": (28.12, -15.43), "gran canaria": (28.12, -15.43),
    "leon": (42.60, -5.57),
    "lleida": (41.62, 0.62), "lerida": (41.62, 0.62),
    "lugo": (43.01, -7.56),
    "madrid": (40.42, -3.70),
    "malaga": (36.72, -4.42),
    "murcia": (37.99, -1.13),
    "navarra": (42.81, -1.64), "nafarroa": (42.81, -1.64), "pamplona": (42.81, -1.64),
    "ourense": (42.34, -7.86), "orense": (42.34, -7.86),
    "palencia": (42.01, -4.53),
    "baleares": (39.57, 2.65), "illes balears": (39.57, 2.65),
    "mallorca": (39.57, 2.65), "palma": (39.57, 2.65),
    "pontevedra": (42.43, -8.64), "vigo": (42.24, -8.72),
    "salamanca": (40.97, -5.66),
    "tenerife": (28.47, -16.25), "santa cruz de tenerife": (28.47, -16.25),
    "segovia": (40.95, -4.12),
    "sevilla": (37.39, -5.99),
    "soria": (41.76, -2.46),
    "tarragona": (41.12, 1.25),
    "teruel": (40.34, -1.11),
    "toledo": (39.86, -4.02),
    "valencia": (39.47, -0.38),
    "valladolid": (41.65, -4.72),
    "zamora": (41.50, -5.74),
    "zaragoza": (41.65, -0.89),
    "ceuta": (35.89, -5.32),
    "melilla": (35.29, -2.94),
    # Autonomous communities -> a representative city.
    "pais vasco": (43.26, -2.93), "euskadi": (43.26, -2.93),
    "cataluna": (41.39, 2.17), "catalunya": (41.39, 2.17),
    "galicia": (43.36, -8.41),
    "andalucia": (37.39, -5.99),
    "comunidad valenciana": (39.47, -0.38), "valenciana": (39.47, -0.38),
    "castilla y leon": (41.65, -4.72),
    "castilla la mancha": (39.86, -4.02), "castilla-la mancha": (39.86, -4.02),
    "canarias": (28.12, -15.43),
    "aragon": (41.65, -0.89),
    "extremadura": (38.88, -6.97),
    "murcia region": (37.99, -1.13),
    "cantabria region": (43.46, -3.80),
}


# Official display names for the 50 provinces + Ceuta + Melilla, used to
# populate the "Provincia" autocomplete in the UI. Every name here must resolve
# through geocode() below (they map onto keys in PROVINCES).
PROVINCE_NAMES = [
    "A Coruña", "Álava", "Albacete", "Alicante", "Almería", "Asturias", "Ávila",
    "Badajoz", "Barcelona", "Bizkaia", "Burgos", "Cáceres", "Cádiz", "Cantabria",
    "Castellón", "Ciudad Real", "Córdoba", "Cuenca", "Gipuzkoa", "Girona",
    "Granada", "Guadalajara", "Huelva", "Huesca", "Illes Balears", "Jaén",
    "La Rioja", "Las Palmas", "León", "Lleida", "Lugo", "Madrid", "Málaga",
    "Murcia", "Navarra", "Ourense", "Palencia", "Pontevedra", "Salamanca",
    "Santa Cruz de Tenerife", "Segovia", "Sevilla", "Soria", "Tarragona",
    "Teruel", "Toledo", "Valencia", "Valladolid", "Zamora", "Zaragoza",
    "Ceuta", "Melilla",
]

# ISO 3166-2:ES autonomous-community codes -> a place name geocode() resolves.
# Used to turn IP-geolocation results (which only pinpoint the community, not
# the exact province) into a location we can center a search on.
REGION_CODE_TO_PLACE = {
    "AN": "Andalucía", "AR": "Aragón", "AS": "Asturias", "CB": "Cantabria",
    "CE": "Ceuta", "CL": "Castilla y León", "CM": "Castilla-La Mancha",
    "CN": "Canarias", "CT": "Cataluña", "EX": "Extremadura", "GA": "Galicia",
    "IB": "Illes Balears", "MC": "Murcia", "MD": "Madrid", "ML": "Melilla",
    "NC": "Navarra", "PV": "País Vasco", "RI": "La Rioja", "VC": "Comunidad Valenciana",
}


def list_provinces():
    """Return the canonical list of Spanish provinces for the UI autocomplete.

    Returns:
        list[str]: The 50 provinces plus Ceuta and Melilla, alphabetically sorted.
    """
    return sorted(PROVINCE_NAMES, key=_normalize)


def place_for_region_code(code):
    """Map an ISO 3166-2:ES autonomous-community code to a geocodable place name.

    Used for IP-based geolocation hints, which typically only resolve to the
    community level (e.g. "Basque Country"), not the exact province.

    Args:
        code (str): A 2-letter community code, e.g. "PV" for País Vasco.

    Returns:
        str | None: A place name geocode() understands, or None if unrecognised.
    """
    return REGION_CODE_TO_PLACE.get((code or "").strip().upper())


def _normalize(text):
    """Lowercase and strip accents so lookups are forgiving.

    Args:
        text (str): Raw place name.

    Returns:
        str: Normalized (lowercase, no accents, trimmed) name.
    """
    text = unicodedata.normalize("NFKD", text.lower().strip())
    return "".join(c for c in text if not unicodedata.combining(c))


def geocode(place):
    """Resolve a Spanish place name to approximate coordinates.

    Tries an exact match first, then a substring match in either direction (so
    "soy de bilbao" or "provincia de Bizkaia" still resolve).

    Args:
        place (str): A province, region or city name.

    Returns:
        tuple[float, float] | None: (latitude, longitude), or None if unknown.
    """
    if not place:
        return None
    norm = _normalize(place)
    if norm in PROVINCES:
        return PROVINCES[norm]
    for name, coords in PROVINCES.items():
        if name in norm or norm in name:
            return coords
    return None


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance between two points, in kilometres.

    Args:
        lat1, lon1, lat2, lon2 (float): Coordinates in decimal degrees.

    Returns:
        float: Distance in km.
    """
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
