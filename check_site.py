"""Last check before public/ goes out. Exits 1 with the reasons if the site should not be published, so a bad build
never replaces a good one. Every number the table derives is worked out again here, and the config, the table and each
profile have to agree. Lines starting REVIEW don't stop a deploy: they are for an editor to look at, and appear on the
run's summary page.

Usage: python check_site.py
"""
import json, re, sys, datetime as dt
from pathlib import Path

from refresh import FX_RANGE, MIN_FRESH, load_config, profile_id

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
MIN_COMPANIES = 70        # far fewer rows than this means the data is broken, not that companies delisted
MAX_AGE_DAYS = 12         # the latest close can be this old, which covers a long market holiday
MAX_LIVE_AGE_DAYS = 5     # the same for the live files, across a weekend and a holiday
ROUNDING = 0.02           # how far a stored percentage can sit from its recalculation
SHARED = ("debut_mcap", "listing_price", "sales", "sales_yoy", "net_profit")     # the same in the table and every profile


def row_problems(r, c):
    """One company's table row against its config row and against arithmetic."""
    n = r["name"]
    if (r["currency"] == "USD") != (r["exchange"] == "Nasdaq"):
        yield f"{n}: currency {r['currency']} doesn't fit {r['exchange']}"
    if (r["currency"] == "INR") != bool(re.search(r"\.(NS|BO)$", r["ticker"])):
        yield f"{n}: ticker {r['ticker']} doesn't fit {r['currency']}"
    if r["listing_date"] and not r["listing_date"].startswith(str(r["listing_year"])):
        yield f"{n}: listed {r['listing_date']} but listing year {r['listing_year']}"
    for k in ("sector", "exchange", "listing_year", "debut_mcap", "sales", "sales_yoy", "net_profit"):
        if r[k] != c[k]:
            yield f"{n}: {k} is {r[k]} in the table and {c[k]} in the config"
    for k in ("price", "mcap", "listing_price", "debut_mcap", "sales"):
        if r.get(k) is not None and r[k] <= 0:
            yield f"{n}: {k} is {r[k]}"
    for k, now, then in (("mcap_chg", r.get("mcap"), r.get("debut_mcap")), ("price_chg", r.get("price"), r.get("listing_price")), ("week_chg", r.get("price"), r.get("week_price"))):
        want = (now / then - 1) * 100 if now and then else None
        have = r.get(k)
        if (want is None) != (have is None) or (want is not None and abs(want - have) > ROUNDING):
            yield f"{n}: {k} is {have} but works out at {want}"


def review(rows, label):
    """Worth an editor's eye, but no reason to hold a deploy."""
    close = lambda a, b: a is not None and b is not None and abs(a - b) <= max(1.5, abs(b) * 0.01)
    for r in rows:
        for k in ("debut_mcap", "listing_price", "sales", "net_profit"):
            if r.get(k) is None:
                yield f"{r['name']}: no {k}"
        if r.get("stale"):
            yield f"{r['name']}: last close is {r.get('price_date')}"
        path = PUBLIC / f"data/companies/{profile_id(r['ticker'])}.json"
        annual = json.loads(path.read_text())["financials"]["annual"] if path.exists() else []
        # A figure that matches last year's statement and not this year's is the mistake a pasted sheet makes
        if r["currency"] == "INR" and len(annual) > 1 and annual[0]["label"] == label:
            now, before = annual[0], annual[1]
            if close(r["sales"], before["revenue"]) and not close(r["sales"], now["revenue"]):
                yield f"{r['name']}: {label} sales {r['sales']} match {before['label']}, and {now['label']} is {now['revenue']}"
            if close(r["net_profit"], before["pat"]) and not close(r["net_profit"], now["pat"]):
                yield f"{r['name']}: {label} net profit {r['net_profit']} matches {before['label']}, and {now['label']} is {now['pat']}"


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

    comps = {c["ticker"]: c for c in load_config()[0]}
    if len({r["ticker"] for r in rows}) != len(rows) or len({r["name"].lower() for r in rows}) != len(rows):
        yield "a company appears twice"
    if {r["ticker"] for r in rows} != set(comps):
        yield f"the table and the config list different companies: {sorted({r['ticker'] for r in rows} ^ set(comps))}"

    for r in rows:
        if r["ticker"] in comps:
            yield from row_problems(r, comps[r["ticker"]])
        if not r.get("price_date"):
            continue
        path = PUBLIC / f"data/companies/{profile_id(r['ticker'])}.json"
        try:
            doc = json.loads(path.read_text())
            if not doc["daily"]:
                yield f"{r['name']}: its profile has no prices"
            if doc["yahoo"] != r["ticker"] or doc["currency"] != r["currency"]:
                yield f"{r['name']}: its link opens the profile of {doc['yahoo']}"
            for k in SHARED:
                if doc["tracker"].get(k) != r.get(k):
                    yield f"{r['name']}: {k} is {r.get(k)} in the table and {doc['tracker'].get(k)} on its profile"
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
    D = json.loads((ROOT / "data/tracker.json").read_text())
    for line in review(D["rows"], D["meta"]["financials_label"]):
        print("REVIEW:", line)
    print("Site checks passed")


if __name__ == "__main__":
    main()
