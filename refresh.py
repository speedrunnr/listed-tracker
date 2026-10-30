"""Weekly refresh for the Inc42 Listed New-Age Tech Company Tracker.

Reads the editorial inputs (config/companies.csv, config/settings.json), pulls closing prices, splits and
USD/INR from Yahoo Finance, validates, and writes:
  data/tracker.json             the data the widgets render
  data/history/<as_of>.json     a dated copy, one per market week
  data/qa_report.txt            what was checked and anything that needs an editor's eye
  artifact/index.html           site/index.html with the data inlined (the claude.ai artifact)
  public/                       the same pages as full documents, as GitHub Pages serves them

Exit codes: 0 = written; 2 = validation failed, nothing written; 3 = no new market data since the last run
(nothing written unless --force).

Usage: python refresh.py [--force]      (--force writes even when the market date has not moved)
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
MIN_FRESH = 0.90          # share of companies that must have a close in the latest market week
STALE_DAYS = 6            # a close this many calendar days older than the market date is flagged stale
WOW_ALERT = 0.40          # week-on-week price moves above this are listed for review
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


def fetch(tickers, start):
    """Daily bars with split events per ticker. Batch first, then retry misses one by one;
    NSE SME symbols (-SM.NS) only answer short periods, so they fall back to period="5d"."""
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
    now_ist = dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30)))
    for t, h in out.items():
        h.index = pd.to_datetime(h.index)
        if h.index.tz is not None: h.index = h.index.tz_localize(None)
        h.index = h.index.normalize()
        h = h[h["Close"] > 0]
        # a run during Indian market hours would pick up today's partial bar; keep only settled closes
        if t.endswith((".NS", ".BO")) and now_ist.hour < 16 and len(h) and h.index[-1].date() == now_ist.date():
            h = h.iloc[:-1]
        out[t] = h
    return out


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


def full_document(page, attrs="", head=""):
    """The page with its head and body in the right places. The template has no skeleton because the artifact host adds it."""
    top, sep, body = page.partition("</style>")
    return (f'<!doctype html>\n<html lang="en"{attrs}>\n<head>\n<meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n{head}'
            f"{top}{sep}\n</head>\n<body>{body}</body>\n</html>\n")


def build_public(page, company):
    """public/, what GitHub Pages serves: the table on its own in the light theme, since the article has no dark mode,
    and the profiles beside it. data/live/ is written by live_quotes.py --publish and left alone here."""
    robots = '<meta name="robots" content="noindex">\n'
    (PUBLIC / "data").mkdir(parents=True, exist_ok=True)
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
    comps, S = load_config()
    tickers = [c["ticker"] for c in comps]
    start = (min(pd.Timestamp(c["shares_asof"] or c["baseline_date"]) for c in comps if c["currency"] == "INR")
             - pd.Timedelta(days=10)).date().isoformat()
    start = min(start, (pd.Timestamp.today() - pd.Timedelta(days=40)).date().isoformat())
    data = fetch(tickers + ["USDINR=X"], start)
    qa, warn = [], []

    inr_dates = [data[c["ticker"]].index[-1] for c in comps if c["currency"] == "INR" and c["ticker"] in data]
    if not inr_dates:
        print("VALIDATION FAILED - nothing written: no Indian prices came back from Yahoo"); sys.exit(2)
    as_of = max(d for d in inr_dates if inr_dates.count(d) >= 0.25 * len(inr_dates))  # latest date most names share

    prev = json.load(open(OUT)) if OUT.exists() else None
    fx, fx_warning = latest_fx(data, prev)
    if fx_warning: warn.append(fx_warning)

    rows, fresh = [], 0
    for c in comps:
        t, h = c["ticker"], data.get(c["ticker"])
        r = {k: c[k] for k in ("name", "ticker", "currency", "sector", "exchange", "listing_year", "listing_date",
                                "hq_city", "hq_region", "debut_mcap", "sales", "sales_yoy", "net_profit", "note")}
        r["listing_date"] = r["listing_date"] or None
        if h is None or not len(h):
            warn.append(f"{c['name']} ({t}): no price from Yahoo; row shown without market data")
            r.update(price=None, price_date=None, mcap=None, listing_price=c["listing_price"], stale=True)
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
            if not mcap and prev:
                old = next((p for p in prev["rows"] if p["ticker"] == t), None)
                if old and old.get("mcap") and old.get("price"):
                    mcap = old["mcap"] * px / old["price"]; warn.append(f"{c['name']}: market cap carried from last week's at today's price")

        r.update(price=round(px, 4), price_date=str(last_d.date()), stale=stale, listing_price=round(lp, 4) if lp else None,
                 mcap=round(mcap, 2) if mcap else None, week_price=round(wk_px, 4) if wk_px else None)
        r["mcap_chg"] = round((mcap / c["debut_mcap"] - 1) * 100, 2) if mcap and c["debut_mcap"] else None
        r["price_chg"] = round((px / lp - 1) * 100, 2) if lp else None
        r["week_chg"] = round((px / wk_px - 1) * 100, 2) if wk_px else None
        if r["week_chg"] is not None and abs(r["week_chg"]) > WOW_ALERT * 100 and not ev_lp:
            warn.append(f"{c['name']}: moved {r['week_chg']:+.1f}% in the week with no split on record; check for a corporate action")
        rows.append(r)

    share = fresh / len(comps)
    qa.insert(0, f"market date {as_of.date()}, fresh closes {fresh}/{len(comps)} ({share:.0%}), USD/INR {fx:.2f}")
    if share < MIN_FRESH:
        print(f"VALIDATION FAILED - nothing written: only {fresh}/{len(comps)} companies have a close for the week of {as_of.date()}")
        for w in warn: print("  ", w)
        sys.exit(2)

    if prev and prev["meta"]["as_of"] == str(as_of.date()) and not force:
        print(f"No new market data: still {as_of.date()}. Nothing written."); sys.exit(3)

    meta = dict(as_of=str(as_of.date()), refreshed_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30))).isoformat(timespec="minutes"),
                usd_inr=round(fx, 4), source="Yahoo Finance", schedule=S["refresh_schedule"], title=S["title"],
                publisher=S["publisher"], article_url=S["article_url"], methodology_url=S["methodology_url"],
                financials_label=S["financials_label"], editorial=S["editorial_facts"], footnotes=S["footnotes"])
    payload = dict(meta=meta, rows=rows)
    js = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    build_pages(js)
    OUT.write_text(js); (HIST / f"{as_of.date()}.json").write_text(js)
    QA.write_text("\n".join(["QA " + meta["refreshed_at"], *qa, "", "Needs review:" if warn else "Needs review: none", *warn]) + "\n")

    inr =sum(r["mcap"] for r in rows if r["currency"] == "INR" and r["mcap"])
    usd = sum(r["mcap"] for r in rows if r["currency"] == "USD" and r["mcap"])
    print(f"OK {as_of.date()}: {len(rows)} companies, fresh {fresh}, combined market cap ${inr / fx / 100 + usd / 1000:,.1f} Bn at {fx:.2f}")
    for w in warn: print("  review:", w)


if __name__ == "__main__":
    main()
