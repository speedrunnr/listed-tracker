"""Weekly refresh for the Inc42 Listed New-Age Tech Company Tracker.

Reads the editorial inputs (config/companies.csv, config/settings.json), pulls closing prices, splits and
USD/INR from Yahoo Finance, validates, and writes:
  data/tracker.json             the data the widgets render
  data/history/<as_of>.json     a dated copy, one per market week
  data/qa_report.txt            what was checked and anything that needs an editor's eye
  artifact/index.html           site/index.html with the data inlined (the claude.ai artifact)
  public/                       the same pages as full documents, as GitHub Pages serves them

Exit codes: 0 = written; 2 = validation failed, nothing written; 3 = no new market data since the last run
(nothing written unless --force); 4 = same market date, but closes that were late last time have come in: only those
companies are written, and a line "FILLED <id> ..." names them.

Usage: python refresh.py [--force]      (--force writes even when the market date has not moved)
       python refresh.py --as-of 2026-10-30   (ignore bars after that date, to correct a past week's table)
       python refresh.py --rebuild    (re-inline data/tracker.json after editing site/index.html; no download)
"""
import csv, json, os, re, shutil, sys, datetime as dt

from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent
CFG, SETTINGS = ROOT / "config/companies.csv", ROOT / "config/settings.json"
OUT, HIST, QA = ROOT / "data/tracker.json", ROOT / "data/history", ROOT / "data/qa_report.txt"
TEMPLATE, ARTIFACT = ROOT / "site/index.html", ROOT / "artifact/index.html"
PROFILES, PUBLIC = ROOT / "artifact/data/companies", ROOT / "public"
MARKET = ("price", "price_date", "mcap", "week_price", "mcap_chg", "price_chg", "week_chg", "lp_factor", "listing_price", "stale")
EDITORIAL = ("name", "sector", "exchange", "listing_year", "hq_city", "hq_region", "debut_mcap", "sales", "sales_yoy", "net_profit", "note")
MIN_FRESH = 0.90          # share of companies that must have a close in the latest market week
STALE_DAYS = 6            # a close this many calendar days older than the market date is flagged stale
WOW_ALERT = 0.40          # week-on-week price moves above this are listed for review
MAX_TOTAL_MOVE = 0.25     # a combined market cap move above this in one run is a broken feed, not a market
MAX_NASDAQ_DRIFT = 0.15   # Yahoo's USD market cap this far from last week's, scaled by price, is not used
MAX_SME_GAP = 0.30        # an SME feed's bar this far from the main feed's last close is ignored
FX_RANGE = (50, 200)      # a USD/INR quote outside this is a bad tick, not a rate


def profile_id(ticker):
    """Stable id for a company's profile: ETERNAL.NS -> ETERNAL_NS."""
    return re.sub(r"[^A-Z0-9]+", "_", ticker.upper()).strip("_")


def ticker_symbol(ticker):
    return re.sub(r"(-SM)?\.(NS|BO)$", "", ticker)


def exchange_label(ticker):
    return "NSE" if ticker.endswith(".NS") else "BSE" if ticker.endswith(".BO") else "Nasdaq"


def f(x):
    try:
        v = float(x)
        return None if pd.isna(v) else v
    except (TypeError, ValueError):
        return None


def load_config():
    rows = list(csv.DictReader(open(CFG)))
    for r in rows:
        for k in ("debut_mcap", "listing_price", "shares", "sales", "sales_yoy", "net_profit"):
            r[k] = f(r.get(k))
        r["listing_year"] = int(r["listing_year"])
    return rows, json.load(open(SETTINGS))


def settled(h, t):
    """Daily bars on plain dates. A run during Indian market hours would pick up today's partial bar; keep only settled closes."""
    now_ist = dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30)))
    h.index = pd.to_datetime(h.index)
    if h.index.tz is not None: h.index = h.index.tz_localize(None)
    h.index = h.index.normalize()
    h = h[h["Close"] > 0]
    if t.endswith((".NS", ".BO")) and now_ist.hour < 16 and len(h) and h.index[-1].date() == now_ist.date():
        h = h.iloc[:-1]
    return h


def fetch(tickers, start, sme=()):
    """Daily bars with split events per ticker. Batch first, then retry misses one by one;
    NSE SME symbols (-SM.NS) only answer short periods, so they fall back to period="5d".
    The plain .NS feed of an NSE SME listing (sme) can lag by a session, so its -SM.NS bar wins when it is as new or newer."""
    out = {}
    raw = yf.download(tickers, start=start, auto_adjust=False, actions=True, group_by="ticker",
                      threads=True, progress=False)
    for t in tickers:
        try:
            h = raw[t].dropna(subset=["Close"])
            if len(h): out[t] = h
        except KeyError:
            pass
    for t in [t for t in tickers if t not in out]:
        for kw in ({"start": start}, {"period": "5d"}):
            try:
                h = yf.Ticker(t).history(auto_adjust=False, actions=True, **kw).dropna(subset=["Close"])
                if len(h):
                    out[t] = h; break
            except Exception:
                continue
    out = {t: settled(h, t) for t, h in out.items()}
    for t in sme:
        if t not in out or not len(out[t]): continue
        try:
            new = settled(yf.Ticker(t[:-3] + "-SM.NS").history(period="5d", auto_adjust=False, actions=True).dropna(subset=["Close"]), t)
        except Exception:
            continue
        if len(new) and new.index[-1] >= out[t].index[-1] and abs(new["Close"].iloc[-1] / out[t]["Close"].iloc[-1] - 1) <= MAX_SME_GAP:
            out[t] = pd.concat([out[t][out[t].index < new.index[0]], new])
    return out


def catch_up(data, comps, as_of, cap=None):
    """Yahoo can post a session's daily bar hours late for some names, even the morning after: on Saturday 31 Oct 2026 it
    had no Friday bar for 32 of 76 companies at 9:44 AM. Ask again for those one at a time. Returns the names still late."""
    late = []
    for c in comps:
        t = c["ticker"]
        if c["currency"] != "INR" or t not in data or data[t].index[-1] >= as_of: continue
        try:
            h = settled(yf.Ticker(t).history(period="5d", auto_adjust=False, actions=True).dropna(subset=["Close"]), t)
        except Exception:
            h = data[t].iloc[:0]
        if cap is not None: h = h[h.index <= cap]
        if len(h) and h.index[-1] > data[t].index[-1]:
            data[t] = pd.concat([data[t][data[t].index < h.index[0]], h])
        if data[t].index[-1] < as_of: late.append(c["name"])
    return late


def split_factor(h, after):
    """Product of split ratios strictly after the given date (2 = 1:2 split or 1:1 bonus, 0.1 = 1:10 consolidation)."""
    if h is None or "Stock Splits" not in h: return 1.0, []
    s = h.loc[h.index > pd.Timestamp(after), "Stock Splits"]
    s = s[s > 0]
    fac = 1.0
    for v in s: fac *= float(v)
    return fac, [f"{d.date()} x{v:g}" for d, v in s.items()]


def latest_fx(data, prev):
    """The newest USD/INR close from this run, else the last rate saved. There is no fixed fallback: a run with no
    rate at all fails validation rather than convert at a number nobody fetched."""
    h = data.get("USDINR=X")
    fx = f(h["Close"].iloc[-1]) if h is not None and len(h) else None
    if fx and FX_RANGE[0] < fx < FX_RANGE[1]:
        return fx, None
    fx = (prev or {}).get("meta", {}).get("usd_inr")
    if not fx:
        print("VALIDATION FAILED - nothing written: no USD/INR rate from Yahoo and none saved"); sys.exit(2)
    return fx, f"USD/INR unavailable; using the last rate fetched, {fx}"


def change(now, then):
    return round((now / then - 1) * 100, 2) if now and then else None


def apply_editorial(payload):
    """config/companies.csv always wins over saved data, so an edit there reaches the table on the next build, percentages
    included, with no download. Companies are matched by ticker; prices and market caps are left as fetched."""
    comps = {c["ticker"]: c for c in load_config()[0]}
    for r in payload["rows"]:
        c = comps.get(r["ticker"])
        if not c: continue
        r.update({k: c[k] for k in EDITORIAL})
        r["listing_date"] = c["listing_date"] or None
        if c["listing_price"]: r["listing_price"] = round(c["listing_price"] / (r.get("lp_factor") or 1), 4)
        r["mcap_chg"], r["price_chg"] = change(r.get("mcap"), r["debut_mcap"]), change(r.get("price"), r.get("listing_price"))


def sync_profiles(rows):
    """The same editorial values in each saved profile, rewritten only where one differs."""
    for r in rows:
        path = PROFILES / f"{profile_id(r['ticker'])}.json"
        if not path.exists(): continue
        doc = json.loads(path.read_text())
        T = doc["tracker"]
        want = {**T, **{k: r.get(k) for k in ("debut_mcap", "listing_price", "sales", "sales_yoy", "net_profit")}}
        want["mcap_chg"], want["price_chg"] = change(want.get("mcap"), want["debut_mcap"]), change(doc.get("price"), want["listing_price"])
        names = {"company": r["name"], "sector": r["sector"], "exchange": r["exchange"], "hq_city": r["hq_city"] or None,
                 "listing_year": r["listing_year"], "listing_date": r["listing_date"]}
        if want != T or any(doc.get(k) != v for k, v in names.items()):
            doc.update(names); doc["tracker"] = want
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")))
            os.replace(tmp, path)


def full_document(page, attrs="", head=""):
    """The page with its head and body in the right places. The template has no skeleton because the artifact host adds it."""
    top, sep, body = page.partition("</style>")
    return (f'<!doctype html>\n<html lang="en"{attrs}>\n<head>\n<meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n{head}'
            f"{top}{sep}\n</head>\n<body>{body}</body>\n</html>\n")


def build_public(page, company):
    """public/, what GitHub Pages serves: the table on its own in the light theme, since the article has no dark mode,
    and the profiles beside it, plus the article embed's script. data/live/ is written by live_quotes.py --publish and
    left alone here."""
    robots = '<meta name="robots" content="noindex">\n'
    (PUBLIC / "data").mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "site/embed.js", PUBLIC / "embed.js")
    (PUBLIC / "index.html").write_text(full_document(page, ' data-theme="light" data-widget="table"', robots))
    (PUBLIC / "company.html").write_text(company.replace('<html lang="en">', '<html lang="en" data-theme="light">', 1)
                                         .replace("<head>\n", "<head>\n" + robots, 1))
    shutil.rmtree(PUBLIC / "data/companies", ignore_errors=True)
    shutil.copytree(PROFILES, PUBLIC / "data/companies")


def build_pages(js):
    """Inline the data into the widget page and assemble everything the artifact publishes:
      artifact/index.html            the page (the artifact host adds the document skeleton)
      artifact/standalone.html       the same page as a full document, for local preview
      artifact/company.html          the company profile page, with the shared stylesheet inline
      artifact/publish_files.json    published path -> source path for the Artifact tool's `files`
    then the public site from the same pages. A company links to a profile only once its data file exists."""
    html = TEMPLATE.read_text()
    marker = "/*__TRACKER_DATA__*/null"
    if marker not in html:
        print("VALIDATION FAILED - nothing written: site/index.html has no data marker"); sys.exit(2)
    OUT.parent.mkdir(exist_ok=True); HIST.mkdir(parents=True, exist_ok=True); ARTIFACT.parent.mkdir(exist_ok=True)
    payload = json.loads(js)
    payload["meta"]["footnotes"] = json.load(open(SETTINGS))["footnotes"]
    apply_editorial(payload)
    sync_profiles(payload["rows"])
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    for r in payload["rows"]:
        pid = profile_id(r["ticker"])
        r["profile"] = pid if (PROFILES / f"{pid}.json").exists() else None
    page = html.replace(marker, json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/"))
    ARTIFACT.write_text(page)
    (ARTIFACT.parent / "standalone.html").write_text(full_document(page))
    company = (ROOT / "site/company.html").read_text()
    link = '<link rel="stylesheet" href="assets/tracker-ui.css">'
    if link not in company:
        print("VALIDATION FAILED - nothing written: site/company.html has no stylesheet link"); sys.exit(2)
    company = company.replace(link, "<style>\n" + (ROOT / "site/assets/tracker-ui.css").read_text() + "</style>")
    (ARTIFACT.parent / "company.html").write_text(company)
    build_public(page, company)
    base = ARTIFACT.parent
    files = {"company.html": "company.html"}
    for p in sorted((base / "data").rglob("*.json")):
        files[p.relative_to(base).as_posix()] = p.relative_to(base).as_posix()
    (base / "publish_files.json").write_text(json.dumps(files, indent=1) + "\n")


def main():
    if "--rebuild" in sys.argv:   # re-inline the last data after a change to site/index.html; no download
        build_pages(OUT.read_text()); print("rebuilt pages from", OUT.name); return
    force = "--force" in sys.argv
    cap = pd.Timestamp(sys.argv[sys.argv.index("--as-of") + 1]) if "--as-of" in sys.argv else None
    comps, S = load_config()
    tickers = [c["ticker"] for c in comps]
    start = (min(pd.Timestamp(c["shares_asof"] or c["baseline_date"]) for c in comps if c["currency"] == "INR")
             - pd.Timedelta(days=10)).date().isoformat()
    start = min(start, (pd.Timestamp.today() - pd.Timedelta(days=40)).date().isoformat())
    sme = [c["ticker"] for c in comps if c["exchange"] == "NSE (SME)" and not c["ticker"].endswith("-SM.NS")]
    data = fetch(tickers + ["USDINR=X"], start, sme)
    if cap is not None:
        data = {t: h[h.index <= cap] for t, h in data.items()}
        data = {t: h for t, h in data.items() if len(h)}
    qa, warn = [], []

    inr_dates = [data[c["ticker"]].index[-1] for c in comps if c["currency"] == "INR" and c["ticker"] in data]
    if not inr_dates:
        print("VALIDATION FAILED - nothing written: no Indian prices came back from Yahoo"); sys.exit(2)
    as_of = max(d for d in inr_dates if inr_dates.count(d) >= 0.25 * len(inr_dates))  # latest date most names share
    late = catch_up(data, comps, as_of, cap)
    if late:
        qa.append(f"{len(late)} closes not yet posted for {as_of.date()}: {', '.join(late)}")
        warn.append(f"{len(late)} companies still show an older close because Yahoo has not posted {as_of.date()} for them yet; a later run fills them in")

    prev = json.load(open(OUT)) if OUT.exists() else None
    fx, fx_warning = latest_fx(data, prev)
    if fx_warning: warn.append(fx_warning)
    prev_rows = {p["ticker"]: p for p in (prev or {}).get("rows", [])}

    rows, fresh = [], 0
    for c in comps:
        t, h = c["ticker"], data.get(c["ticker"])
        r = {k: c[k] for k in ("name", "ticker", "currency", "sector", "exchange", "listing_year", "listing_date",
                                "hq_city", "hq_region", "debut_mcap", "sales", "sales_yoy", "net_profit", "note")}
        r["listing_date"] = r["listing_date"] or None
        old = prev_rows.get(t, {})
        if h is None or not len(h):                    # a company Yahoo misses keeps its last figures, with its date showing
            warn.append(f"{c['name']} ({t}): no price from Yahoo; keeping the last figures")
            r.update({k: old.get(k) for k in MARKET if k not in ("listing_price", "stale")})
            r.update(listing_price=old.get("listing_price", c["listing_price"]), stale=True)
            rows.append(r); continue
        last_d, px = h.index[-1], float(h["Close"].iloc[-1])
        stale = (as_of - last_d).days >= STALE_DAYS
        fresh += not stale
        if stale: warn.append(f"{c['name']}: last close {last_d.date()} is older than the market date {as_of.date()}")
        wk = h.loc[h.index <= last_d - pd.Timedelta(days=7), "Close"]
        wk_px = float(wk.iloc[-1]) if len(wk) and (last_d - wk.index[-1]).days <= 12 else None

        fac_lp, ev_lp = split_factor(h, c["baseline_date"])
        lp = c["listing_price"] / fac_lp if c["listing_price"] else None
        if ev_lp: qa.append(f"{c['name']}: split/bonus after {c['baseline_date']} ({', '.join(ev_lp)}); listing price adjusted")

        if c["currency"] == "INR":
            fac_sh, ev_sh = split_factor(h, c["shares_asof"] or c["baseline_date"])
            shares = c["shares"] * fac_sh if c["shares"] else None
            if ev_sh: qa.append(f"{c['name']}: shares adjusted x{fac_sh:g} for {', '.join(ev_sh)}")
            mcap = shares * px / 1e7 if shares else None                     # INR crore
        else:
            try:
                mcap = yf.Ticker(t).fast_info["marketCap"] / 1e6                # USD million
            except Exception:
                mcap = None
            if old.get("mcap") and old.get("price") and old.get("price_date"):
                implied = old["mcap"] * px * split_factor(h, old["price_date"])[0] / old["price"]
                if not mcap or abs(mcap / implied - 1) > MAX_NASDAQ_DRIFT:
                    warn.append(f"{c['name']}: Yahoo's market cap of {mcap and round(mcap)} Mn is not credible; using last week's at this week's price, {round(implied)} Mn")
                    mcap = implied

        r.update(price=round(px, 4), price_date=str(last_d.date()), stale=stale, listing_price=round(lp, 4) if lp else None,
                 mcap=round(mcap, 2) if mcap else None, week_price=round(wk_px, 4) if wk_px else None, lp_factor=fac_lp)
        r["mcap_chg"], r["price_chg"], r["week_chg"] = change(mcap, c["debut_mcap"]), change(px, lp), change(px, wk_px)
        if r["week_chg"] is not None and abs(r["week_chg"]) > WOW_ALERT * 100 and not ev_lp:
            warn.append(f"{c['name']}: moved {r['week_chg']:+.1f}% in the week with no split on record; check for a corporate action")
        rows.append(r)

    share = fresh / len(comps)
    qa.insert(0, f"market date {as_of.date()}, fresh closes {fresh}/{len(comps)} ({share:.0%}), USD/INR {fx:.2f}")
    if share < MIN_FRESH:
        print(f"VALIDATION FAILED - nothing written: only {fresh}/{len(comps)} companies have a close for the week of {as_of.date()}")
        for w in warn: print("  ", w)
        sys.exit(2)

    if prev and str(as_of.date()) < prev["meta"]["as_of"]:
        print(f"VALIDATION FAILED - nothing written: the market date went back from {prev['meta']['as_of']} to {as_of.date()}"); sys.exit(2)
    filled = []
    if prev and prev["meta"]["as_of"] == str(as_of.date()) and not force:
        # Same market date: write only the companies whose close is newer than the saved one, the ones Yahoo was late
        # with last time. Every other row, and the rate, stay exactly as saved.
        for i, r in enumerate(rows):
            old = prev_rows.get(r["ticker"])
            if old and (old.get("price_date") or "") >= (r.get("price_date") or ""):
                rows[i] = {**r, **{k: old.get(k) for k in MARKET}}
            else:
                filled.append(r)
        if not filled:
            print(f"No new market data: still {as_of.date()}. Nothing written."); sys.exit(3)
        fx = prev["meta"]["usd_inr"]
        qa.append(f"filled late closes for {as_of.date()}: {', '.join(r['name'] for r in filled)}")
    if prev:
        total = lambda rs, rate: sum((p["mcap"] or 0) * (rate / 10 if p["currency"] == "USD" else 1) for p in rs)
        then, now = total(prev["rows"], fx), total(rows, fx)
        if abs(now / then - 1) > MAX_TOTAL_MOVE:
            print(f"VALIDATION FAILED - nothing written: combined market cap moved from {then:,.0f} to {now:,.0f} Cr in one run"); sys.exit(2)

    meta = dict(as_of=str(as_of.date()), refreshed_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30))).isoformat(timespec="minutes"),
                usd_inr=round(fx, 4), source="Yahoo Finance", schedule=S["refresh_schedule"], title=S["title"],
                publisher=S["publisher"], article_url=S["article_url"], methodology_url=S["methodology_url"],
                financials_label=S["financials_label"], editorial=S["editorial_facts"], footnotes=S["footnotes"])
    payload = dict(meta=meta, rows=rows)
    build_pages(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    (HIST / f"{as_of.date()}.json").write_text(OUT.read_text())
    QA.write_text("\n".join(["QA " + meta["refreshed_at"], *qa, "", "Needs review:" if warn else "Needs review: none", *warn]) + "\n")

    inr =sum(r["mcap"] for r in rows if r["currency"] == "INR" and r["mcap"])
    usd = sum(r["mcap"] for r in rows if r["currency"] == "USD" and r["mcap"])
    print(f"OK {as_of.date()}: {len(rows)} companies, fresh {fresh}, combined market cap ${inr / fx / 100 + usd / 1000:,.1f} Bn at {fx:.2f}")
    for w in warn: print("  review:", w)
    if filled:
        print("FILLED " + " ".join(profile_id(r["ticker"]) for r in filled)); sys.exit(4)


if __name__ == "__main__":
    main()
