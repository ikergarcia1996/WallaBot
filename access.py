"""Wallapop site-access layer.

This module is the deterministic, cheap data layer described in the architecture:
it only fetches data from Wallapop, with no LLM involved. It exposes two plain
Python functions that the reasoning layer will later register as Claude tools:

    - search_wallapop(query, category_id, max_price, ...)
    - get_listing_details(item_id)

Two fetch modes are used, in order of preference:

    1. Direct API calls with httpx (fast, no browser). Wallapop's internal
       endpoints return JSON directly.
    2. Playwright fallback: the *same* JSON endpoints are fetched from inside a
       real browser context that carries the saved login session. This dodges
       bot-detection blocks and reuses the exact same parsing code.

The saved session (cookies) comes from login.py and lives in storage_state.json.
"""

import concurrent.futures
import datetime
import json
import os

import httpx

import geo

# --- Configuration -----------------------------------------------------------

API_BASE = "https://api.wallapop.com/api/v3"
# Human-facing listing page, built from an item's "web_slug".
ITEM_PAGE_BASE = "https://es.wallapop.com/item"

# Search is location-aware. Default to central Madrid; override per call if needed.
DEFAULT_LATITUDE = 40.4168
DEFAULT_LONGITUDE = -3.7038

# A listing is "reachable" for a user if the seller ships it OR it's within this
# many km of the user (close enough to pick up in person).
REACH_KM = 150

# Where login.py writes the saved browser session.
STORAGE_STATE_PATH = os.path.join(os.path.dirname(__file__), "storage_state.json")

# Headers that make our requests look like a normal browser hitting the API.
BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "X-DeviceOS": "0",
}


# --- Low-level fetching ------------------------------------------------------

def _fetch_json(url, params=None):
    """GET a Wallapop JSON endpoint, using httpx first and Playwright as fallback.

    Args:
        url (str): Full endpoint URL.
        params (dict | None): Query-string parameters.

    Returns:
        dict: The parsed JSON response.

    Raises:
        RuntimeError: If both httpx and the Playwright fallback fail.
    """
    # Mode 1: plain httpx request (fast, no browser).
    # Note: these public endpoints work best ANONYMOUSLY. Sending the saved
    # session cookies actually makes the API return HTTP 400 (it then expects a
    # matching bearer token). So we deliberately don't attach cookies here; the
    # login session is used by the Playwright fallback below via storage_state.
    try:
        resp = httpx.get(url, params=params, headers=BASE_HEADERS, timeout=20)
        if resp.status_code == 200:
            return resp.json()
        print(f"[access] httpx got HTTP {resp.status_code}, trying Playwright fallback...")
    except Exception as exc:  # network error, JSON error, etc.
        print(f"[access] httpx failed ({exc}), trying Playwright fallback...")

    # Mode 2: fetch the same JSON from inside a real browser session.
    return _fetch_json_playwright(url, params)


def _fetch_json_playwright(url, params=None):
    """Fetch a JSON endpoint through a real browser context (Playwright).

    Reuses the saved login session so the request looks like a logged-in user,
    which helps when the plain httpx call gets blocked.

    Args:
        url (str): Full endpoint URL.
        params (dict | None): Query-string parameters.

    Returns:
        dict: The parsed JSON response.

    Raises:
        RuntimeError: If the request does not return HTTP 200.
    """
    from playwright.sync_api import sync_playwright

    # Only pass storage_state if we actually have a saved session.
    state = STORAGE_STATE_PATH if os.path.exists(STORAGE_STATE_PATH) else None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=state, extra_http_headers=BASE_HEADERS)
        response = context.request.get(url, params=params or {})
        status = response.status
        body = response.text()
        browser.close()

    if status != 200:
        raise RuntimeError(f"Playwright fetch failed: HTTP {status} for {url}")
    return json.loads(body)


# --- Parsing helpers ---------------------------------------------------------

def _item_url(item):
    """Build the human-facing listing URL for a search result item.

    Args:
        item (dict): A raw item object from the search endpoint.

    Returns:
        str: The es.wallapop.com listing page URL (empty if no slug present).
    """
    slug = item.get("web_slug")
    return f"{ITEM_PAGE_BASE}/{slug}" if slug else ""


def _format_timestamp(ms_or_s):
    """Convert a Wallapop timestamp to a readable ISO date string.

    Wallapop uses milliseconds in some endpoints and seconds in others; this
    normalizes both.

    Args:
        ms_or_s (int | None): A unix timestamp in seconds or milliseconds.

    Returns:
        str | None: An ISO date-time string (UTC), or None if no timestamp.
    """
    if not ms_or_s:
        return None
    # Anything past ~year 33658 in seconds is really milliseconds.
    seconds = ms_or_s / 1000 if ms_or_s > 1e11 else ms_or_s
    return datetime.datetime.utcfromtimestamp(seconds).isoformat() + "Z"


def _first_image(item):
    """Return the best available image URL for a search result item.

    Args:
        item (dict): A raw item object from the search endpoint.

    Returns:
        str | None: A large image URL, or None if the listing has no images.
    """
    images = item.get("images") or []
    if not images:
        return None
    urls = images[0].get("urls", {})
    return urls.get("big") or urls.get("medium") or urls.get("small")


def _summarize_item(item):
    """Reduce a raw search item to the fields the reasoning layer needs.

    Args:
        item (dict): A raw item object from the search endpoint.

    Returns:
        dict: A compact summary (id, title, price, url, description, location, ...).
    """
    price = item.get("price", {})
    location = item.get("location", {})
    return {
        "id": item.get("id"),
        "title": item.get("title", ""),
        "price": price.get("amount"),
        "currency": price.get("currency"),
        "url": _item_url(item),
        "thumbnail": _first_image(item),
        # Full description is already included in search results; keep it, the
        # reasoning layer benefits from having specs without a second request.
        "description": item.get("description", ""),
        "category_id": item.get("category_id"),
        "reserved": item.get("reserved", {}).get("flag", False),
        # "shippable" = the seller accepts shipping (not just pickup).
        "shippable": item.get("shipping", {}).get("user_allows_shipping", False),
        "location": {
            "city": location.get("city"),
            "region": location.get("region"),
            "postal_code": location.get("postal_code"),
        },
    }


# --- Listing liveness --------------------------------------------------------

# Wallapop's search index can return listings whose seller account has been
# deleted. The item endpoint still returns data for them, but the public web
# page 404s, so they're dead links. The reliable signal is that the seller's
# /users/{id} endpoint returns 404. We cache results per user id (a seller with
# many listings is only checked once per process).
_seller_alive_cache = {}


def _seller_exists(user_id):
    """Return whether a seller account still exists (i.e. the listing is live).

    Uses a direct httpx status check (not _fetch_json) so a 404 is treated as a
    definitive "gone" without triggering the Playwright fallback. On any other
    error we assume the seller exists, to avoid dropping listings over a hiccup.

    Args:
        user_id (str): The seller's alphanumeric user id.

    Returns:
        bool: True if the seller exists (or we couldn't tell), False if deleted.
    """
    if not user_id:
        return True
    if user_id in _seller_alive_cache:
        return _seller_alive_cache[user_id]
    alive = True
    try:
        resp = httpx.get(f"{API_BASE}/users/{user_id}", headers=BASE_HEADERS, timeout=15)
        if resp.status_code == 404:
            alive = False
    except Exception:
        pass  # transient error -> keep the listing
    _seller_alive_cache[user_id] = alive
    return alive


# --- Public functions (future Claude tools) ----------------------------------

def search_wallapop(
    query,
    category_id=None,
    max_price=None,
    min_price=None,
    next_page=None,
    latitude=DEFAULT_LATITUDE,
    longitude=DEFAULT_LONGITUDE,
    verify=True,
    user_lat=None,
    user_lon=None,
    reach_km=REACH_KM,
):
    """Search Wallapop and return one page of listing summaries.

    A single call returns ~40 results plus a ``next_page`` token. To load more
    (the site's "load more" button), call this again with that token — keep the
    query/filters the same and pass ``next_page`` to walk through the results.

    Args:
        query (str): Free-text search keywords (e.g. "ryzen 5600 am4").
        category_id (int | None): Optional Wallapop category id to narrow results
            (e.g. 24200 for "Technology & electronics").
        max_price (float | None): Optional maximum price filter, in euros.
        min_price (float | None): Optional minimum price filter, in euros.
        next_page (str | None): Pagination token from a previous call's result,
            used to fetch the next page of the same search.
        latitude (float): Search-center latitude. Defaults to Madrid.
        longitude (float): Search-center longitude. Defaults to Madrid.
        verify (bool): If True (default), drop dead listings whose seller account
            no longer exists (orphaned listings whose web page 404s).
        user_lat (float | None): The user's latitude. When given (with user_lon),
            the search is centered there and results are filtered to listings the
            user can actually get: shippable OR within ``reach_km`` km. Each item
            also gets a ``distance_km`` field.
        user_lon (float | None): The user's longitude (see user_lat).
        reach_km (float): Pickup radius for the reachability filter.

    Returns:
        dict: ``{"items": [...], "next_page": str | None}``. Each item has keys:
            id, title, price, currency, url, thumbnail, description, category_id,
            reserved (always False — reserved listings are dropped, see below),
            shippable, distance_km, location. ``next_page`` is None when there are
            no more pages.
    """
    # Center the search on the user when we know where they are.
    if user_lat is not None and user_lon is not None:
        latitude, longitude = user_lat, user_lon

    params = {
        "keywords": query,
        "source": "search_box",
        "latitude": latitude,
        "longitude": longitude,
    }
    if category_id is not None:
        params["category_id"] = category_id
    if max_price is not None:
        params["max_sale_price"] = max_price
    if min_price is not None:
        params["min_sale_price"] = min_price
    if next_page is not None:
        params["next_page"] = next_page

    data = _fetch_json(f"{API_BASE}/search", params=params)
    items = data.get("data", {}).get("section", {}).get("payload", {}).get("items", [])

    if verify and items:
        # Check sellers concurrently and drop listings whose seller is gone.
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            alive = list(pool.map(lambda it: _seller_exists(it.get("user_id")), items))
        items = [it for it, ok in zip(items, alive) if ok]

    summaries = []
    for item in items:
        summary = _summarize_item(item)
        # Drop reserved listings outright — they can't be bought, so there's no
        # point showing them as candidates (relying on the model to notice and
        # skip a "reserved" flag is unreliable).
        if summary["reserved"]:
            continue
        # Reachability filter: keep only listings the user can get. Distance is
        # computed from the listing's (approximate) coordinates.
        if user_lat is not None and user_lon is not None:
            loc = item.get("location", {})
            if loc.get("latitude") is not None and loc.get("longitude") is not None:
                dist = geo.haversine_km(user_lat, user_lon, loc["latitude"], loc["longitude"])
                summary["distance_km"] = round(dist)
            else:
                dist = None
            reachable = summary["shippable"] or (dist is not None and dist <= reach_km)
            if not reachable:
                continue
        summaries.append(summary)

    return {"items": summaries, "next_page": data.get("meta", {}).get("next_page")}


def _get_seller(user_id):
    """Fetch a seller's public profile and reputation stats.

    Makes two calls: the user profile (for the display name) and the user stats
    (for rating and sales counts).

    Args:
        user_id (str): The alphanumeric Wallapop user id.

    Returns:
        dict: Seller info with keys: username, profile_url, rating, reviews_count,
            sales_count. Fields are None when unavailable.
    """
    if not user_id:
        return {}

    # A seller can be missing (deleted account -> HTTP 404). Fetch profile and
    # stats independently and tolerate either failing, so one dead seller never
    # breaks the whole listing lookup.
    seller = {}
    try:
        profile = _fetch_json(f"{API_BASE}/users/{user_id}")
        seller["username"] = profile.get("micro_name")
        seller["profile_url"] = profile.get("url_share")
    except Exception:
        pass
    try:
        stats = _fetch_json(f"{API_BASE}/users/{user_id}/stats")
        # "counters" is a list of {type, value}; flatten it for easy access.
        counters = {c["type"]: c["value"] for c in stats.get("counters", [])}
        seller["rating"] = stats.get("rating_average")  # avg star rating, e.g. 4.9
        seller["reviews_count"] = counters.get("reviews")
        seller["sales_count"] = counters.get("sold")  # completed sales
    except Exception:
        pass
    return seller


def get_ad_details(item_id):
    """Fetch the full detail record for a single ad, including seller reputation.

    Use the "id" field returned by search_wallapop (the alphanumeric item id,
    e.g. "3zlmrlyeg8jx"), not the numeric id in the page URL.

    Args:
        item_id (str): The alphanumeric Wallapop item id.

    Returns:
        dict: Detailed ad data with keys: id, available (bool — False if the
            listing is orphaned/dead), title, description, price, currency, url,
            images (list of URLs), last_updated (ISO string), category,
            characteristics, location, counters (views/favorites), and seller
            (username, rating, sales_count, reviews_count, profile_url).
    """
    data = _fetch_json(f"{API_BASE}/items/{item_id}")

    # The details endpoint wraps title/description in an "original" field and
    # nests the price under "cash" (both differ from the search endpoint).
    title = data.get("title", {})
    description = data.get("description", {})
    price = data.get("price", {}).get("cash", {})
    location = data.get("location", {})
    taxonomy = data.get("taxonomy", [])

    # Collect all photo URLs (biggest size available) so the agent can look at them.
    images = [
        img.get("urls", {}).get("big")
        or img.get("urls", {}).get("medium")
        or img.get("urls", {}).get("small")
        for img in data.get("images", [])
    ]
    images = [url for url in images if url]

    seller = _get_seller(data.get("user", {}).get("id"))

    return {
        "id": data.get("id"),
        # If the seller profile is gone, the listing is orphaned / its web page
        # 404s — flag it so the agent discards it. Same for "reserved": search
        # already filters these out, but a listing can be reserved between the
        # search and this call, so it's checked again here.
        "available": bool(seller.get("username")),
        "reserved": data.get("reserved", {}).get("flag", False),
        "title": title.get("original") if isinstance(title, dict) else title,
        "description": (
            description.get("original") if isinstance(description, dict) else description
        ),
        "price": price.get("amount"),
        "currency": price.get("currency"),
        "url": data.get("share_url", ""),
        "images": images,
        "last_updated": _format_timestamp(data.get("modified_date")),
        "category": taxonomy[0].get("name") if taxonomy else None,
        # Structured spec fields (socket, RAM type, etc.) when Wallapop has them.
        "characteristics": data.get("characteristics_details", []),
        "counters": data.get("counters", {}),  # views, favorites, conversations
        "location": {
            "city": location.get("city"),
            "postal_code": location.get("postal_code"),
            "country_code": location.get("country_code"),
        },
        "seller": seller,
    }
