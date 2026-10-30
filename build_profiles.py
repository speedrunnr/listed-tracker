"""Company profile data for the Listed Tracker.

Writes artifact/data/companies/<ID>.json for each company in data/tracker.json. Run refresh.py first: every
saved profile ends on the tracker's close, and live_quotes.py lays current prices over it on the page. Finish with `refresh.py --rebuild`, which
links the new profiles and writes the publish manifest.

  price chart   Yahoo Finance daily history since listing + the last 5 sessions of 5-minute bars (India only)
  financials    Yahoo Finance consolidated statements: INR crore, or USD million for Nasdaq listings
  news          inc42.com WordPress API; only posts whose headline names the company or one of its brands

Usage:  python build_profiles.py [ETERNAL_NS ...]     full build (about 30 minutes for every company)
        python build_profiles.py --no-news [ids]      prices and financials only; saved stories stay as they are
"""
from __future__ import annotations

import argparse, csv, html, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

from refresh import profile_id, ticker_symbol, exchange_label

ROOT = Path(__file__).resolve().parent
TRACKER = ROOT / "data/tracker.json"
OUT = ROOT / "artifact/data/companies"
ALIASES = ROOT / "config/aliases.csv"
IST = "Asia/Kolkata"
NEWS_MAX = 8
MAX_FAIL_SHARE = 0.2      # more failures than this and the run exits non-zero
RETRY_WAIT_S = 60
UA = {"User-Agent": "Mozilla/5.0 (Inc42 Datalabs tracker profile builder)"}


def log(msg: str) -> None:
    print(msg, flush=True)


def epoch_ist(ts: pd.Timestamp) -> int:
    """Seconds since epoch of the IST wall-clock time read as UTC, so the chart axis shows IST."""
    naive = ts.tz_convert(IST).tz_localize(None)
    return int((naive - pd.Timestamp("1970-01-01")).total_seconds())


def num(v, nd=2) -> float | None:
    return None if v is None or pd.isna(v) else round(float(v), nd)


# ---------------------------------------------------------------- prices
def end_on_quote(daily: list, session_date: str, last: float) -> None:
    """The history ends on the tracker's close, not on whatever the source has since."""
    if daily and daily[-1][0] == session_date:
        row = daily[-1]
        row[4] = last
        row[2], row[3] = max(row[2], last), min(row[3], last)
    elif not daily or daily[-1][0] < session_date:
        daily.append([session_date, last, last, last, last, 0])


def sessions_from_bars(m: pd.DataFrame, daily: list, session_date: str) -> list:
    """The last five sessions of 5-minute bars (IST) as [{date, prev_close, points}]; each session
    starts on its open and ends on that day's close in `daily`."""
    closes = {r[0]: r[4] for r in daily}
    dates = [r[0] for r in daily]
    sessions = []
    for day, bars in m.groupby(m.index.normalize()):
        ds = day.strftime("%Y-%m-%d")
        if ds not in closes or ds > session_date or bars.empty:
            continue
        k = dates.index(ds)
        start = day + pd.Timedelta(hours=9, minutes=15)
        pts = [[epoch_ist(start), num(bars.Open.iloc[0])]]
        pts += [[epoch_ist(ts + pd.Timedelta(minutes=5)), num(v)] for ts, v in bars.Close.items() if pd.notna(v)]
        pts[-1][1] = closes[ds]
        sessions.append({"date": ds, "prev_close": daily[k - 1][4] if k else None, "points": pts})
    return sessions[-5:]


def daily_rows(d: pd.DataFrame, session_date: str) -> list:
    d = d[d.index <= pd.Timestamp(session_date)]
    return [[i.strftime("%Y-%m-%d"), num(r.Open), num(r.High), num(r.Low), num(r.Close), int(r.Volume or 0)]
            for i, r in d.iterrows() if pd.notna(r.Close) and r.Close > 0]


def fill_from_bars(daily: list, m: pd.DataFrame | None, session_date: str) -> list:
    """Daily rows for sessions Yahoo's daily data skipped but its 5-minute bars cover."""
    if m is None or m.empty:
        return daily
    have = {r[0] for r in daily}
    extra = []
    for day, bars in m.groupby(m.index.normalize()):
        ds = day.strftime("%Y-%m-%d")
        bars = bars.dropna(subset=["Close"])
        if ds in have or ds > session_date or bars.empty or (daily and ds < daily[0][0]):
            continue
        extra.append([ds, num(bars.Open.iloc[0]), num(bars.High.max()), num(bars.Low.min()), num(bars.Close.iloc[-1]),
                      int(bars.Volume.fillna(0).sum())])
    return sorted(daily + extra, key=lambda r: r[0]) if extra else daily


def price_history(t: yf.Ticker, session_date: str, last: float, intraday: bool) -> tuple[list, list]:
    d = t.history(period="max", interval="1d", auto_adjust=False)
    if d.empty:                                    # NSE SME symbols answer only short periods
        d = t.history(period="5d", interval="1d", auto_adjust=False)
    if not d.empty:
        d.index = d.index.tz_convert(IST if intraday else "America/New_York").normalize().tz_localize(None)
    m = None
    if intraday:
        try:
            m = t.history(period="5d", interval="5m", auto_adjust=False)
            m = m.tz_convert(IST) if not m.empty else None
        except KeyError:                           # NSE SME symbols can answer 5-minute requests without trading periods
            m = None
    if d.empty and (m is None or m.empty):      # yfinance answers a dropped connection with empty frames, not an error
        raise RuntimeError("no prices from Yahoo")
    daily = fill_from_bars(daily_rows(d, session_date), m, session_date)
    end_on_quote(daily, session_date, last)
    return daily, sessions_from_bars(m, daily, session_date) if m is not None else []


def week52(daily: list, session_date: str) -> dict:
    closes = pd.Series({r[0]: r[4] for r in daily})
    yr = closes[closes.index >= (pd.Timestamp(session_date) - pd.Timedelta(days=365)).strftime("%Y-%m-%d")]
    return {"wk52_high": num(yr.max()), "wk52_high_date": yr.idxmax(), "wk52_low": num(yr.min()), "wk52_low_date": yr.idxmin()}


# ---------------------------------------------------------------- financials
def _row(df: pd.DataFrame, *names):
    for n in names:
        if n in df.index:
            return df.loc[n]
    return pd.Series(index=df.columns, dtype=float)


def fy_label(d: pd.Timestamp) -> str:
    """Indian fiscal year ending March: 2026-03-31 → FY26."""
    return f"FY{(d.year + (1 if d.month > 3 else 0)) % 100:02d}"


def q_label(d: pd.Timestamp) -> str:
    q = {6: 1, 9: 2, 12: 3, 3: 4}[d.month]
    return f"Q{q} {fy_label(d)}"


def cy_label(d: pd.Timestamp) -> str:
    return f"CY{d.year % 100:02d}"


def cq_label(d: pd.Timestamp) -> str:
    return f"Q{d.quarter} CY{d.year % 100:02d}"


def statement_rows(df: pd.DataFrame, labeller, div: float) -> list[dict]:
    rev = _row(df, "Total Revenue", "Operating Revenue")
    ebitda = _row(df, "EBITDA", "Normalized EBITDA")
    pat = _row(df, "Net Income Common Stockholders", "Net Income")
    eps = _row(df, "Diluted EPS", "Basic EPS")
    amount = lambda v: None if v is None or pd.isna(v) else round(float(v) / div, 1)
    out = []
    for c in df.columns:
        if pd.isna(rev.get(c)) and pd.isna(pat.get(c)):
            continue
        out.append({"label": labeller(c), "end": c.strftime("%Y-%m-%d"), "revenue": amount(rev.get(c)),
                    "ebitda": amount(ebitda.get(c)), "pat": amount(pat.get(c)), "eps": num(eps.get(c))})
    return out


def quarter_slots(rows: list[dict], label, n: int = 5) -> list[dict]:
    """The last n quarter-ends in order, with an empty slot where Yahoo has no data."""
    if not rows:
        return []
    have = {r["end"]: r for r in rows}
    ends = pd.date_range(end=pd.Timestamp(rows[0]["end"]), periods=n, freq="QE")[::-1]
    return [have.get(e.strftime("%Y-%m-%d")) or {"label": label(e), "end": e.strftime("%Y-%m-%d"),
            "revenue": None, "ebitda": None, "pat": None, "eps": None} for e in ends]


def financials(t: yf.Ticker, currency: str) -> dict:
    div = 1e6 if currency == "USD" else 1e7
    inc, qinc, bs = t.income_stmt, t.quarterly_income_stmt, t.balance_sheet
    calendar = not inc.empty and inc.columns[0].month == 12        # Freshworks reports calendar years
    a_lab, q_lab = (cy_label, cq_label) if calendar else (fy_label, q_label)
    annual = statement_rows(inc, a_lab, div)[:4]
    quarterly = quarter_slots(statement_rows(qinc, q_lab, div), q_lab)
    balance = None
    if not bs.empty:
        c = bs.columns[0]
        amt = lambda *n: (lambda v: None if pd.isna(v) else round(float(v) / div, 1))(_row(bs, *n).get(c))
        balance = {"label": a_lab(c), "end": c.strftime("%Y-%m-%d"),
                   "cash": amt("Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents"),
                   "debt": amt("Total Debt"), "net_worth": amt("Stockholders Equity", "Common Stock Equity")}
    info = t.info or {}
    return {"basis": "Consolidated", "currency": "USD Mn" if currency == "USD" else "INR Cr",
            "source": "Company filings via Yahoo Finance", "calendar_year": calendar,
            "annual": annual, "quarterly": quarterly, "balance": balance,
            "pe_ttm": num(info.get("trailingPE"), 1), "pb": num(info.get("priceToBook"), 2)}


# ---------------------------------------------------------------- news
NEWS_MONTHS = 12          # how far back to look for a company Inc42 rarely covers
GAP_S = 1.0               # at most about one request a second to inc42.com
_last_call = [0.0]


def _search(q: str, after: pd.Timestamp, before: pd.Timestamp, tries: int = 4) -> list[dict]:
    """One page of WordPress search results, paced and retried: inc42.com answers bursts with 429."""
    u = "https://inc42.com/wp-json/wp/v2/posts?" + urllib.parse.urlencode(
        {"search": q, "per_page": 100, "orderby": "date", "_fields": "id,date,link,title",
         "after": after.strftime("%Y-%m-%dT00:00:00"), "before": before.strftime("%Y-%m-%dT00:00:00")})
    for attempt in range(tries):
        wait = _last_call[0] + GAP_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == tries - 1:
                raise
            ra = e.headers.get("Retry-After")
            time.sleep(float(ra) if ra and ra.isdigit() else 15 * 2 ** attempt)
    return []


def inc42_news(aliases: list[str]) -> tuple[list[dict], bool]:
    """Up to NEWS_MAX inc42.com stories whose headline names the company or a brand, and whether every
    search succeeded. Scans a calendar month at a time, the current month first."""
    complete = True
    rx = re.compile(r"\b(" + "|".join(map(re.escape, aliases)) + r")\b", re.I)
    today = pd.Timestamp.now(tz=IST).tz_localize(None).normalize()
    seen: dict[int, dict] = {}
    month = today.replace(day=1)
    for _ in range(NEWS_MONTHS):
        before = today + pd.Timedelta(days=1) if month == today.replace(day=1) else month + pd.offsets.MonthBegin(1)
        for q in aliases:
            try:
                posts = _search(q, month, before)
            except Exception as e:                   # one alias failing should not lose the rest
                log(f"  ! Inc42 search '{q}' {month:%b %Y} failed: {e}")
                complete = False
                continue
            for p in posts:
                title = html.unescape(re.sub(r"<[^>]+>", "", p["title"]["rendered"])).strip()
                if p["id"] in seen or not rx.search(title) or not p["link"].startswith("https://inc42.com/"):
                    continue
                seen[p["id"]] = {"date": p["date"], "title": title, "url": p["link"], "section": p["link"].split("/")[3]}
        if len(seen) >= NEWS_MAX:
            break
        month = month - pd.offsets.MonthBegin(1)
    return sorted(seen.values(), key=lambda x: x["date"], reverse=True)[:NEWS_MAX], complete


def merge_news(new: list[dict], old: list[dict], complete: bool) -> list[dict]:
    """A run where some searches failed only adds stories; it never drops ones already shown."""
    if complete:
        return new
    by_url = {n["url"]: n for n in old}
    by_url.update({n["url"]: n for n in new})
    return sorted(by_url.values(), key=lambda x: x["date"], reverse=True)[:NEWS_MAX]


def write_json(path: Path, doc) -> None:
    """Write next to the target and rename, so nothing ever reads half a file."""
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")))
    os.replace(tmp, path)


def default_aliases(name: str) -> list[str]:
    """Search terms from a company name: 'PB Fintech (Policybazaar)' -> ['PB Fintech', 'Policybazaar']."""
    m = re.match(r"^(.*?)\s*\((.*)\)$", name)
    parts = [m.group(1), m.group(2)] if m else [name]
    return [re.sub(r"\s+(Limited|Ltd\.?)$", "", p, flags=re.I).strip() for p in parts]


# ---------------------------------------------------------------- main
def build_one(r: dict, meta: dict, aliases: dict, with_news: bool) -> None:
    cid = profile_id(r["ticker"])
    usd = r["currency"] == "USD"
    t = yf.Ticker(r["ticker"])
    session = r["price_date"]
    daily, sessions = price_history(t, session, r["price"], intraday=not usd)
    fin = financials(t, r["currency"])
    path = OUT / f"{cid}.json"
    prev = json.loads(path.read_text()) if path.exists() else {}
    if not fin["annual"] and prev.get("financials", {}).get("annual"):     # a failed fetch must not blank saved statements
        fin = {**prev["financials"], "pe_ttm": prev["stats"].get("pe_ttm"), "pb": prev["stats"].get("pb")}
    if with_news:
        news, complete = inc42_news(aliases.get(cid) or default_aliases(r["name"]))
        news = merge_news(news, prev.get("news", []), complete)
        news_stamp = datetime.now().astimezone().isoformat(timespec="seconds") if complete else prev.get("news_updated")
    else:
        news, news_stamp = prev.get("news", []), prev.get("news_updated")
    price = r["price"]
    doc = {
        "id": cid, "ticker": ticker_symbol(r["ticker"]), "yahoo": r["ticker"], "company": r["name"], "sector": r["sector"],
        "exchange": r["exchange"], "exchange_label": exchange_label(r["ticker"]), "hq_city": r.get("hq_city") or None,
        "currency": r["currency"], "listing_date": r.get("listing_date"), "listing_year": r["listing_year"],
        "price": price, "prev_close": daily[-2][4] if len(daily) > 1 else None, "session_date": session,
        "tracker": {k: r.get(k) for k in ("mcap", "debut_mcap", "mcap_chg", "listing_price", "price_chg", "week_chg",
                                          "sales", "sales_yoy", "net_profit")},
        "financials_label": meta["financials_label"], "refresh": meta["schedule"],
        "stats": {**week52(daily, session), "pe_ttm": fin.get("pe_ttm"), "pb": fin.get("pb")},
        "financials": {k: v for k, v in fin.items() if k not in ("pe_ttm", "pb")},
        "news": news, "news_updated": news_stamp, "daily": daily, "sessions": sessions,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    write_json(path, doc)
    log(f"{r['name']} ({r['ticker']}): {len(daily)} days, {len(sessions)} sessions, {len(fin['annual'])} years, "
        f"{sum(q['revenue'] is not None for q in fin['quarterly'])}/{len(fin['quarterly'])} quarters, {len(news)} stories")


def built_today(cid: str) -> bool:
    path = OUT / f"{cid}.json"
    if not path.exists():
        return False
    doc = json.loads(path.read_text())
    today = datetime.now().astimezone().date().isoformat()
    return (doc.get("generated_at") or "").startswith(today) and (doc.get("news_updated") or "").startswith(today) and len(doc["daily"]) > 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="*", help="profile ids such as ETERNAL_NS (default: every company in the tracker)")
    ap.add_argument("--no-news", action="store_true", help="leave the saved Inc42 stories as they are")
    ap.add_argument("--resume", action="store_true", help="skip profiles already fully built today")
    a = ap.parse_args()
    D = json.loads(TRACKER.read_text())
    rows = [r for r in D["rows"] if r.get("price_date") and (not a.ids or profile_id(r["ticker"]) in a.ids)]
    aliases = {}
    if ALIASES.exists():
        aliases = {x["id"]: x["aliases"].split("|") for x in csv.DictReader(open(ALIASES))}
    OUT.mkdir(parents=True, exist_ok=True)
    if a.resume:
        rows = [r for r in rows if not built_today(profile_id(r["ticker"]))]
        log(f"{len(rows)} profiles left to build")
    failed = []
    for r in rows:
        try:
            build_one(r, D["meta"], aliases, not a.no_news)
        except Exception as e:      # one company failing keeps its last profile and must not stop the rest
            log(f"  ! {r['name']} ({r['ticker']}): {type(e).__name__}: {e}")
            failed.append(r)
    if failed:                      # a second pass catches a dropped connection or a brief Yahoo limit
        log(f"retrying {len(failed)} after a pause")
        time.sleep(RETRY_WAIT_S)
        still = []
        for r in failed:
            try:
                build_one(r, D["meta"], aliases, not a.no_news)
            except Exception as e:
                log(f"  ! {r['name']} ({r['ticker']}): {type(e).__name__}: {e}")
                still.append(r)
        failed = still
    log(f"built {len(rows) - len(failed)}/{len(rows)} profiles" + (f"; failed: {', '.join(r['name'] for r in failed)}" if failed else ""))
    return 1 if rows and len(failed) / len(rows) > MAX_FAIL_SHARE else 0


if __name__ == "__main__":
    sys.exit(main())
