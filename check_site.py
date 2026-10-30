"""Last check before public/ goes out. Exits 1 with the reasons if the site should not be published, so a bad build
never replaces a good one.

Usage: python check_site.py
"""
import json, sys, datetime as dt
from pathlib import Path

from refresh import FX_RANGE, MIN_FRESH, profile_id

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
MIN_COMPANIES = 70        # far fewer rows than this means the data is broken, not that companies delisted
MAX_AGE_DAYS = 12         # the latest close can be this old, which covers a long market holiday
MAX_LIVE_AGE_DAYS = 5     # the same for the live files, across a weekend and a holiday


def problems():
    if not (PUBLIC / "index.html").exists() or not (PUBLIC / "company.html").exists():
        yield "public/ has no pages; run refresh.py --rebuild"; return
    page = (PUBLIC / "index.html").read_text()
    profile = (PUBLIC / "company.html").read_text()
    if "/*__TRACKER_DATA__*/" in page or not page.startswith("<!doctype html>"):
        yield "index.html was not built from the template"
    if '<html lang="en" data-theme="light" data-widget="table">' not in page:
        yield "index.html does not open on the table in the light theme"
    if '<html lang="en" data-theme="light">' not in profile or 'href="assets/tracker-ui.css"' in profile:
        yield "company.html is not light, or its stylesheet is not inline"

    D = json.loads((ROOT / "data/tracker.json").read_text())
    rows, M = D["rows"], D["meta"]
    if len(rows) < MIN_COMPANIES:
        yield f"only {len(rows)} companies"
    priced = [r for r in rows if r.get("mcap") is not None]
    if len(priced) < MIN_FRESH * len(rows):
        yield f"only {len(priced)} of {len(rows)} companies have a market cap"
    age = (dt.date.today() - dt.date.fromisoformat(M["as_of"])).days
    if age > MAX_AGE_DAYS:
        yield f"the latest close is {age} days old"
    if not FX_RANGE[0] < (M.get("usd_inr") or 0) < FX_RANGE[1]:
        yield f"USD/INR {M.get('usd_inr')} is not a usable rate"

    for r in rows:
        if not r.get("price_date"):
            continue
        path = PUBLIC / f"data/companies/{profile_id(r['ticker'])}.json"
        try:
            doc = json.loads(path.read_text())
            if not doc["daily"]:
                yield f"{r['name']}: its profile has no prices"
        except (OSError, ValueError, KeyError) as e:
            yield f"{r['name']}: its profile cannot be read ({type(e).__name__})"

    # the live files are optional for the page, which falls back to saved data, but broken ones are not
    fx = PUBLIC / "data/live/fx.json"
    if fx.exists():
        L = json.loads(fx.read_text())
        if not FX_RANGE[0] < L["usd_inr"] < FX_RANGE[1]:
            yield f"the live USD/INR {L['usd_inr']} is not a usable rate"
        if (dt.datetime.now().timestamp() - L["updated"]) / 86400 > MAX_LIVE_AGE_DAYS:
            yield "the live files are stale"


def main():
    found = list(problems())
    for p in found:
        print("NOT PUBLISHED:", p)
    if found:
        sys.exit(1)
    print("Site checks passed")


if __name__ == "__main__":
    main()
