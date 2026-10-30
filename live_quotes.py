"""Live prices for the company profiles.

Writes data/live_quotes.json, one document with every company's current price, its last few daily bars, its recent
intraday bars and the latest USD/INR rate. Profile pages lay it over the data saved by build_profiles.py, and the
tracker page converts at its rate. The table's prices do not use it: they stay on the Saturday close from refresh.py.

Usage:  python live_quotes.py             the scheduled task sends the file to the artifact's database (live/quotes)
        python live_quotes.py --publish   also writes public/data/live/ for GitHub Pages: one small file per profile,
                                          fx.json for the table, and quotes.json, which the next run reads back

The last line of output says what to do next: WRITE (send the file) or SKIP (nothing changed, or Yahoo failed).
Like fetch() in refresh.py, fetch() here is the only place that talks to Yahoo; swap it for the licensed feed.
"""
from __future__ import annotations

import json, logging, sys, time, warnings
from pathlib import Path

import pandas as pd
import yfinance as yf

from build_profiles import epoch_ist
from refresh import FX_RANGE, profile_id

ROOT = Path(__file__).resolve().parent
TRACKER = ROOT / "data/tracker.json"
OUT = ROOT / "data/live_quotes.json"
PUBLIC_LIVE = ROOT / "public/data/live"
FX_TICKER = "USDINR=X"
DAYS = 8                  # daily bars kept: enough to cover a week between profile builds
SESSIONS = 5              # intraday sessions kept, the 5D chart's span
MIN_FRESH = 0.6           # fewer names than this back from Yahoo and the run changes nothing
MAX_MOVE = 0.5            # a day's move above this is a bad tick (Indian stocks have 20% circuit limits)
MAX_FX_MOVE = 0.05        # a USD/INR move above this between runs is a bad tick
MAX_BYTES = 240_000       # the artifact database takes 256 KiB per document


def fetch(tickers: list[str], intraday: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Daily bars for all names and 5-minute bars for the Indian ones. Today's daily row is live while the market
    is open. A few small NSE listings have no 5-minute bars; they still get a price from the daily bars."""
    warnings.filterwarnings("ignore")
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    opts = dict(group_by="ticker", threads=True, progress=False, auto_adjust=False)
    return yf.download(tickers, period="12d", interval="1d", **opts), yf.download(intraday, period="5d", interval="5m", **opts)


def column(frame: pd.DataFrame, ticker: str) -> pd.DataFrame | None:
    return frame[ticker] if ticker in frame.columns.get_level_values(0) else None


def daily_bars(frame: pd.DataFrame, ticker: str) -> list:
    d = column(frame, ticker)
    if d is None:
        return []
    d = d.dropna(subset=["Close"])
    rows = []
    for ts, r in d.iterrows():
        c = float(r["Close"])
        o, h, l = (c if pd.isna(r[k]) else float(r[k]) for k in ("Open", "High", "Low"))
        rows.append([ts.strftime("%Y-%m-%d"), round(o, 2), round(h, 2), round(l, 2), round(c, 2), 0 if pd.isna(r["Volume"]) else int(r["Volume"])])
    return rows


def intraday_sessions(frame: pd.DataFrame, ticker: str, days: list, older_s: int) -> list:
    """Each session as [date, prev_close, first_time, step_seconds, closes, slots]. The latest session keeps its
    5-minute bars and older ones are thinned to older_s, which keeps the one document under its size limit. Times
    read as IST, the same way build_profiles.py saves them. slots is null when no bar is missing."""
    m = column(frame, ticker)
    if m is None:
        return []
    close = m["Close"].dropna()
    if close.empty:
        return []
    closes = {r[0]: r[4] for r in days}
    order = sorted(closes)
    dates = sorted({ts.strftime("%Y-%m-%d") for ts in close.index})[-SESSIONS:]
    out = []
    for ds in dates:
        step = 300 if ds == dates[-1] else older_s
        bars = close[close.index.strftime("%Y-%m-%d") == ds]
        if step != 300:
            bars = bars.resample(f"{step}s").last().dropna()
        t = [epoch_ist(ts) for ts in bars.index]
        slots = [(x - t[0]) // step for x in t]
        k = order.index(ds) if ds in order else -1
        out.append([ds, closes[order[k - 1]] if k > 0 else None, t[0], step, [round(float(v), 2) for v in bars], None if slots == list(range(len(slots))) else slots])
    if out[-1][0] == days[-1][0]:  # the chart ends on the headline price
        out[-1][4][-1] = days[-1][4]
    return out


def latest_fx(daily: pd.DataFrame, saved: float | None) -> float | None:
    """USD/INR now, or the last good rate if Yahoo's looks wrong. Today's daily row is live while the market trades."""
    d = column(daily, FX_TICKER)
    close = d["Close"].dropna() if d is not None else []
    if not len(close):
        return saved
    rate = round(float(close.iloc[-1]), 4)
    if not FX_RANGE[0] < rate < FX_RANGE[1] or (saved and abs(rate / saved - 1) > MAX_FX_MOVE):
        print(f"USD/INR {rate} looks like a bad tick; keeping {saved}")
        return saved
    return rate


def publish(doc: dict) -> None:
    """Static copies of the document, renamed into place so a reader never gets half a file."""
    PUBLIC_LIVE.mkdir(parents=True, exist_ok=True)
    head = {k: doc.get(k) for k in ("updated", "markets", "fx")}
    files = {f"{pid}.json": {**head, "q": {pid: q}} for pid, q in doc["q"].items()}
    files["quotes.json"] = doc
    if doc.get("fx"):
        files["fx.json"] = {"usd_inr": doc["fx"], "updated": doc["updated"]}
    for name, body in files.items():
        tmp = PUBLIC_LIVE / f".{name}.tmp"
        tmp.write_text(json.dumps(body, ensure_ascii=False, separators=(",", ":")))
        tmp.replace(PUBLIC_LIVE / name)


def build(rows: list, previous: dict, daily: pd.DataFrame, bars: pd.DataFrame, older_s: int) -> tuple[dict, list]:
    q, skipped = {}, []
    for r in rows:
        pid, ticker = profile_id(r["ticker"]), r["ticker"]
        days = daily_bars(daily, ticker)
        if not days:
            skipped.append(f"{r['name']}: no price")
        elif len(days) > 1 and abs(days[-1][4] / days[-2][4] - 1) > MAX_MOVE:
            skipped.append(f"{r['name']}: {days[-2][4]} to {days[-1][4]} looks like a bad tick")
        else:
            q[pid] = {"p": days[-1][4], "pc": days[-2][4] if len(days) > 1 else None, "d": days[-1][0],
                      "days": days[-DAYS:], "s": intraday_sessions(bars, ticker, days, older_s)}
            continue
        if pid in previous:
            q[pid] = previous[pid]
    return q, skipped


def run() -> tuple[str, bool]:
    """What to do next, and whether Yahoo answered well enough for this to count as a good run."""
    tracker = json.loads(TRACKER.read_text())
    rows = tracker["rows"]
    usd = {profile_id(r["ticker"]) for r in rows if r["currency"] == "USD"}
    saved = json.loads(OUT.read_text()) if OUT.exists() else {}
    previous = saved.get("q", {})
    tickers = [r["ticker"] for r in rows]
    daily, bars = fetch(tickers + [FX_TICKER], [r["ticker"] for r in rows if r["currency"] != "USD"])
    fx = latest_fx(daily, saved.get("fx") or tracker["meta"].get("usd_inr"))
    for older_s in (900, 1800, 3600):
        q, skipped = build(rows, previous, daily, bars, older_s)
        doc = {"updated": int(time.time()),
               "markets": {"NSE": {"date": max((v["d"] for k, v in q.items() if k not in usd), default=None)},
                           "US": {"date": max((v["d"] for k, v in q.items() if k in usd), default=None)}},
               "fx": fx, "q": q}
        text = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
        if len(text.encode()) <= MAX_BYTES:
            break
    else:
        print(f"Document is {len(text.encode()):,} bytes, over the limit even with hourly bars"); return "SKIP", False
    fresh = len(rows) - len(skipped)
    for s in skipped:
        print(s)
    print(f"{fresh} of {len(rows)} companies, NSE {doc['markets']['NSE']['date']}, US {doc['markets']['US']['date']}, USD/INR {fx}, {len(text.encode()):,} bytes")
    if fresh < MIN_FRESH * len(rows):
        print("Too few prices from Yahoo; keeping the last document"); return "SKIP", False
    if q == previous and fx == saved.get("fx"):
        print("Nothing changed"); return "SKIP", True
    OUT.write_text(text)
    return "WRITE", True


def main() -> None:
    static = "--publish" in sys.argv
    action, ok = "SKIP", False
    try:
        action, ok = run()
    finally:                                        # a failed run still leaves the last good files in place
        if static and OUT.exists():
            publish(json.loads(OUT.read_text()))
        print(action)
    if static and not ok:                           # on GitHub this fails the run, which emails the owner
        sys.exit(1)


if __name__ == "__main__":
    main()
