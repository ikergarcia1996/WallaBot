"""Quick manual test for the Wallapop access layer.

Run ``python test_access.py`` to do a live search and fetch details for the
first result. Optionally pass your own query:

    python test_access.py "ryzen 5600 am4"

This does NOT need a saved login session (the API works unauthenticated), but it
will use storage_state.json automatically if login.py has been run.
"""

import sys

from access import get_ad_details, search_wallapop


def main():
    query = sys.argv[1] if len(sys.argv) > 1 else "cpu ddr4 placa base"
    max_price = 300

    print(f"Searching Wallapop for: {query!r}  (max €{max_price})\n")
    page = search_wallapop(query, max_price=max_price)
    results = page["items"]
    print(f"Got {len(results)} results. Has next page: {bool(page['next_page'])}\n")

    # Show the first few summaries.
    for r in results[:5]:
        reserved = " [RESERVED]" if r["reserved"] else ""
        print(f"- {r['title']}  |  €{r['price']}{reserved}")
        print(f"  {r['location']['city']}  |  {r['url']}")
    print()

    # Demonstrate "load more" pagination.
    if page["next_page"]:
        page2 = search_wallapop(query, max_price=max_price, next_page=page["next_page"])
        print(f"Loaded {len(page2['items'])} more results via next_page token.\n")

    if not results:
        print("No results, nothing to detail. Try a different query.")
        return

    # Fetch full details (incl. seller reputation) for the first result.
    first = results[0]
    print(f"Fetching details for first result (id={first['id']})...\n")
    d = get_ad_details(first["id"])
    print(f"Title:       {d['title']}")
    print(f"Price:       €{d['price']} {d['currency']}")
    print(f"Category:    {d['category']}")
    print(f"Last update: {d['last_updated']}")
    print(f"Location:    {d['location']['city']} ({d['location']['country_code']})")
    print(f"Images:      {len(d['images'])}  ->  {d['images'][0] if d['images'] else '-'}")
    print(f"Counters:    {d['counters']}")
    s = d["seller"]
    print(f"Seller:      {s.get('username')}  |  rating {s.get('rating')}  |  "
          f"{s.get('sales_count')} sales  |  {s.get('reviews_count')} reviews")
    print(f"             {s.get('profile_url')}")
    print("\nDescription:")
    print((d["description"] or "")[:400])


if __name__ == "__main__":
    main()
