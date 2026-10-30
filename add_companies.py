"""Add queued companies to config/companies.csv, filling what the owner left blank from Yahoo Finance.

The Add Company form on the artifact writes to its "additions" collection. The scheduled task saves the pending
documents to data/pending_additions.json (a JSON list; each item carries its document "id") and runs this script.
On GitHub the "Add a company" form leaves one file per company in data/queue/, and --queue takes them all at once.
Content is treated as data: every field is validated here and nothing from it is ever executed.

  Input   data/pending_additions.json, or with --queue every data/queue/*.json
  Output  config/companies.csv and config/aliases.csv updated, data/additions_result.json:
          [{"id", "ticker", "status": "added"|"failed", "reason", "profile_id"}]

When a company is added it then runs refresh.py --force, build_profiles.py for the new companies and
refresh.py --rebuild, so the table, charts and profiles are ready to publish. If that fails the config is put back
and the companies stay queued. Publishing and writing each status back to the collection are the task's job.

Exit 0 when the file was processed (some items may have failed; see the results), 2 when the input is unreadable.
Prints "PUBLISH" as its last line when there is something new to publish.
"""
import csv, json, os, re, subprocess, sys
from pathlib import Path

import pandas as pd
import yfinance as yf

from refresh import profile_id

ROOT = Path(__file__).resolve().parent
CFG, ALIASES = ROOT / "config/companies.csv", ROOT / "config/aliases.csv"
PENDING, RESULT = ROOT / "data/pending_additions.json", ROOT / "data/additions_result.json"
QUEUE = ROOT / "data/queue"
TICKER = re.compile(r"^[A-Z0-9&_-]{1,20}(\.(NS|BO))?$")
EXCHANGES = {"NSE, BSE", "NSE", "BSE", "NSE (SME)", "BSE (SME)", "Nasdaq"}
NCR = {"Gurugram", "Gurgaon", "Noida", "New Delhi", "Delhi"}
FRESH_DAYS = 10
BASELINE_NOTE = "Added through the tracker's Add Company form"


class Reject(Exception):
    pass


def clean(v, maxlen):
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(v or "")).strip()[:maxlen]


def number(v):
    if v is None or v == "": return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise Reject(f"{v!r} is not a number")
    return None if pd.isna(x) else x


def history(t):
    h = t.history(period="max", auto_adjust=False)
    if h.empty:
        h = t.history(period="5d", auto_adjust=False)
    if h.empty:
        return h
    h.index = h.index.tz_localize(None).normalize()
    return h[h["Close"] > 0]


def statement(t, currency):
    """Latest annual revenue, its YoY change and net income, in INR crore or USD million."""
    inc = t.income_stmt
    if inc.empty: return None, None, None
    div = 1e6 if currency == "USD" else 1e7
    get = lambda *names: next((inc.loc[n] for n in names if n in inc.index), None)
    rev, ni = get("Total Revenue", "Operating Revenue"), get("Net Income Common Stockholders", "Net Income")
    sales = round(float(rev.iloc[0]) / div) if rev is not None and pd.notna(rev.iloc[0]) else None
    yoy = None
    if rev is not None and len(rev) > 1 and pd.notna(rev.iloc[0]) and pd.notna(rev.iloc[1]) and rev.iloc[1] > 0:
        yoy = round((rev.iloc[0] / rev.iloc[1] - 1) * 100)
    np_ = round(float(ni.iloc[0]) / div) if ni is not None and pd.notna(ni.iloc[0]) else None
    return sales, yoy, np_


def build_row(doc, existing):
    ticker = clean(doc.get("ticker"), 24).upper()
    name, sector, exchange = clean(doc.get("name"), 80), clean(doc.get("sector"), 48), clean(doc.get("exchange"), 12)
    if not TICKER.match(ticker): raise Reject("The ticker isn't in Yahoo Finance form.")
    if len(name) < 2 or not sector: raise Reject("The name or sector is missing.")
    if exchange not in EXCHANGES: raise Reject("The exchange isn't one the tracker knows.")
    if ticker in {r["ticker"] for r in existing}: raise Reject(f"{ticker} is already in the tracker.")
    if name.lower() in {r["name"].lower() for r in existing}: raise Reject(f"{name} is already in the tracker.")
    t = yf.Ticker(ticker)
    h = history(t)
    if h.empty:
        if yf.Ticker("^NSEI").history(period="5d").empty:     # Yahoo itself is unreachable: stay queued, don't blame the ticker
            raise RuntimeError("Yahoo Finance did not respond")
        raise Reject(f"Yahoo Finance has no prices for {ticker}. Check the ticker.")
    if (pd.Timestamp.now().normalize() - h.index[-1]).days > FRESH_DAYS:
        raise Reject(f"Yahoo Finance's last price for {ticker} is {h.index[-1].date()}, too old to use.")
    fi = t.fast_info
    currency = (fi.get("currency") or "").upper()
    if currency not in ("INR", "USD"): raise Reject(f"{ticker} trades in {currency or 'an unknown currency'}; the tracker handles INR and USD.")
    if (currency == "USD") != (exchange == "Nasdaq"): raise Reject(f"{ticker} trades in {currency}, which doesn't match {exchange}.")
    shares = fi.get("shares")
    if currency == "INR" and not shares: raise Reject(f"Yahoo Finance has no share count for {ticker}.")
    try:
        info = t.info or {}
    except Exception:
        info = {}

    first = h.index[0]
    year = int(number(doc.get("listing_year")) or first.year)
    full_history = len(h) > 5 or (pd.Timestamp.now().normalize() - first).days < 15   # not a symbol that answers only 5 days
    is_listing = full_history and first.year == year                                   # the first bar really is the listing
    listing_price = number(doc.get("listing_price"))
    if listing_price is None and is_listing: listing_price = round(float(h["Open"].iloc[0]), 2)
    debut = number(doc.get("debut_mcap"))
    div = 1e6 if currency == "USD" else 1e7
    if debut is None and listing_price and shares: debut = round(listing_price * shares / div)      # how Inc42's own figures work
    # the owner's figures win; Yahoo fills the rest
    pick = lambda key, val: val if number(doc.get(key)) is None else number(doc.get(key))
    sales, yoy, np_ = statement(t, currency)
    sales, yoy, np_ = pick("sales", sales), pick("sales_yoy", yoy), pick("net_profit", np_)
    city = clean(doc.get("hq_city"), 40) or clean(info.get("city"), 40)
    city = {"Gurgaon": "Gurugram", "Bangalore": "Bengaluru", "Delhi": "New Delhi"}.get(city, city)
    today = str(pd.Timestamp.now().date())
    row = dict(name=name, ticker=ticker, currency=currency, sector=sector, exchange=exchange, listing_year=year,
               listing_date=str(first.date()) if is_listing else "", hq_city=city, hq_region="Delhi NCR" if city in NCR else city,
               debut_mcap=debut, listing_price=listing_price, shares=int(shares) if currency == "INR" else "",
               shares_asof=today if currency == "INR" else "", shares_source="Yahoo shares outstanding" if currency == "INR" else "",
               sales=sales, sales_yoy=yoy, net_profit=np_, baseline_date=today, note=BASELINE_NOTE)
    return row, [a for a in (clean(x, 40) for x in (doc.get("aliases") or [])[:6]) if a]


def take_queue():
    """Every company waiting in data/queue/ becomes the pending list, with its file name as its id."""
    docs = [{**json.loads(p.read_text()), "id": p.stem} for p in sorted(QUEUE.glob("*.json"))]
    PENDING.write_text(json.dumps(docs, ensure_ascii=False))
    return docs


def main():
    queued = "--queue" in sys.argv
    if queued and not take_queue():
        print("No companies queued"); return
    try:
        pending = json.loads(PENDING.read_text())
        assert isinstance(pending, list)
    except Exception as e:
        print(f"Cannot read {PENDING.name}: {e}"); sys.exit(2)
    with open(CFG, newline="") as f:
        rd = csv.DictReader(f); cols = rd.fieldnames; rows = list(rd)
    with open(ALIASES, newline="") as f:
        alias_rows = list(csv.DictReader(f))
    results = []
    for doc in pending:
        rid = str(doc.get("id", ""))
        ticker = clean(doc.get("ticker"), 24).upper()
        if any(r["ticker"] == ticker and r["note"] == BASELINE_NOTE for r in rows):      # added last time, publish did not finish
            results.append(dict(id=rid, ticker=ticker, status="added", reason="", profile_id=profile_id(ticker), resumed=True))
            continue
        try:
            row, extra = build_row(doc, rows)
        except Reject as e:
            results.append(dict(id=rid, ticker=doc.get("ticker"), status="failed", reason=str(e), profile_id=None)); continue
        except Exception as e:                       # a Yahoo hiccup: leave it queued for the next run
            results.append(dict(id=rid, ticker=doc.get("ticker"), status="pending", reason=f"{type(e).__name__}: {e}"[:200], profile_id=None)); continue
        rows.append({k: ("" if row.get(k) is None else row.get(k)) for k in cols})
        pid = profile_id(row["ticker"])
        base = [re.sub(r"\s+(Limited|Ltd\.?)$", "", row["name"], flags=re.I)]
        alias_rows.append({"id": pid, "aliases": "|".join(dict.fromkeys(base + extra))})
        results.append(dict(id=rid, ticker=row["ticker"], status="added", reason="", profile_id=pid))
    added = [r for r in results if r["status"] == "added" and not r.get("resumed")]
    publish = any(r.get("resumed") for r in results)
    if added:
        saved = {path: path.read_bytes() for path in (CFG, ALIASES)}
        rows.sort(key=lambda x: x["name"].lower())
        for path, fields, data in ((CFG, cols, rows), (ALIASES, ["id", "aliases"], alias_rows)):
            tmp = path.with_suffix(".csv.tmp")
            with open(tmp, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(data)
            os.replace(tmp, path)
        steps = (["refresh.py", "--force"], ["build_profiles.py", *[r["profile_id"] for r in added]], ["refresh.py", "--rebuild"])
        for step in steps:
            done = subprocess.run([sys.executable, *step], cwd=ROOT)
            if done.returncode:                      # nothing half-added: restore and leave them queued
                for path, blob in saved.items():
                    path.write_bytes(blob)
                subprocess.run([sys.executable, "refresh.py", "--force"], cwd=ROOT)
                subprocess.run([sys.executable, "refresh.py", "--rebuild"], cwd=ROOT)
                for r in added:
                    r.update(status="pending", reason=f"{step[0]} failed; will retry")
                break
        else:
            publish = True
    RESULT.write_text(json.dumps(results, indent=1, ensure_ascii=False) + "\n")
    if queued:                                       # a company Yahoo couldn't answer for stays queued for the next run
        for r in results:
            if r["status"] != "pending":
                (QUEUE / f"{r['id']}.json").unlink(missing_ok=True)
    for r in results:
        print(r["status"].upper(), r["ticker"], r["reason"])
    if publish:
        print("PUBLISH")


if __name__ == "__main__":
    main()
