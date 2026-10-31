# Inc42 Listed New-Age Tech Company Tracker

The tracker table from
https://inc42.com/features/indian-listed-new-age-tech-company-tracker-market-cap-revenue-more/ as a widget that runs on its
own: search, filters, sorting, a profile for every company, and data that keeps itself current.

- Public address: https://speedrunnr.github.io/listed-tracker/
- Table: refreshes every Saturday at 8:00 AM IST with the week's closing prices
- Company profiles: follow the market, updating about every 20 minutes in market hours
- USD/INR: the latest rate, fetched automatically with the profile prices. There is no fixed rate anywhere

Nothing here needs a computer switched on. GitHub Actions does the work, GitHub Pages serves the result, and cron-job.org
starts each run on time.

## Files

| Path | What it is | Who edits it |
|---|---|---|
| `config/companies.csv` | One row per company: ticker, sector, exchange, listing year/date, HQ, debut market cap, listing price, shares, FY26 sales/YoY/net profit | Editors |
| `config/settings.json` | Titles, links, footnotes, the median-years facts | Editors |
| `config/aliases.csv` | Names used to search Inc42 stories for each company | Editors |
| `refresh.py` | Pulls Yahoo closes, splits and USD/INR; validates; writes `data/`, `artifact/` and `public/` | Code |
| `build_profiles.py` | Writes `artifact/data/companies/<ID>.json`: price history, financials, Inc42 stories | Code |
| `live_quotes.py` | Current prices and USD/INR for the profiles and the table's conversions | Code |
| `add_companies.py` | Validates new companies against Yahoo and adds them | Code |
| `data/queue/` | Companies waiting to be added, one file each | Generated |
| `data/weekly_done.txt` | The market date of the last finished weekly run | Generated |
| `check_site.py` | Last check before a deploy. A failure leaves the previous site up | Code |
| `site/` | The page templates. Data is inlined at build time | Code |
| `embed/` | The blocks to paste into an Inc42 article | Code |
| `.github/workflows/` | The automation | Code |
| `data/tracker.json`, `data/history/` | Latest table data and one dated copy per market week | Generated |
| `public/` | The site as published. Built on every run, not committed | Generated |

## How the data flows

```
Saturday 8:00 AM, mode=weekly
  refresh.py ──► data/tracker.json ──► build_profiles.py ──► artifact/data/companies/ ──► committed
                                                                       │
Every 20 minutes in market hours, mode=live                            ▼
  live_quotes.py ──► public/data/live/<ID>.json, fx.json ──►  public/ ──► check_site.py ──► GitHub Pages
```

A live run reads back the last published `data/live/quotes.json`, so a company Yahoo misses keeps its last price, and a run
with nothing new (a holiday, a weekend) stops without publishing.

Every step fails safe. `refresh.py` writes nothing if validation fails, `build_profiles.py` keeps a company's last profile if
Yahoo misses it, and `check_site.py` stops a bad build from replacing a good one. A failed run emails the account that started
it, and the next run recovers.

## Data checks

`config/companies.csv` always wins: on every build its values replace the saved ones in the table and each profile, and the
percentages are worked out again, so an edit there can't leave the two out of step.

| Stops a refresh | Stops a deploy (`check_site.py`) | Listed for review on the run's summary page |
|---|---|---|
| Under 90% of companies with a close | Any percentage that doesn't match its recalculation | A week's move above 40% with no split |
| The market date going backwards | Table, config and profile disagreeing | Figures that match last year's statement, not this year's |
| Combined market cap moving over 25% in a run | A company twice, dropped, or with the wrong ticker, currency or profile | A company with no price from Yahoo, which keeps its last figures |
| No USD/INR rate from Yahoo or saved | A zero or negative price, market cap or sales figure | Blank debut cap, listing price, sales or profit |
| | Stale or unusable USD/INR, or a stale close | |

Yahoo's own market cap is not used for Indian listings: for thin NSE SME names it can rest on a price frozen years ago. Market cap
is always latest close × shares. Yahoo's is used for Nasdaq names, and is replaced by last week's at this week's price if it
moves more than 15% from that. The plain `.NS` feed of an NSE SME listing can lag, so its `-SM.NS` bar wins when it is as new or
newer.

## Going live

1. **Create the repository.** A public repository named `listed-tracker`, then push this folder:
   ```bash
   git init -b main
   git add .
   git commit -m "Listed tracker widget"
   git remote add origin https://github.com/speedrunnr/listed-tracker.git
   git push -u origin main
   ```
2. **Turn on Pages.** Settings → Pages → Build and deployment → Source: **GitHub Actions**.
3. **Allow the weekly commit.** Settings → Actions → General → Workflow permissions: **Read and write permissions**.
4. **Check Yahoo access.** Actions → *Yahoo Finance access check* → Run workflow.
5. **First publish.** Actions → *Refresh and deploy* → Run workflow → mode `deploy`. The tracker is then live.
6. **Create the token.** GitHub → Settings → Developer settings → Fine-grained tokens. Repository access: `listed-tracker`
   only. Permissions: **Actions: Read and write**. Note the expiry date: the jobs below stop when it passes.
7. **Add the cron-job.org jobs**, all in `Asia/Kolkata` time, as POST requests to:

   ```
   https://api.github.com/repos/speedrunnr/listed-tracker/actions/workflows/refresh.yml/dispatches
   ```

   | Job | When | Crontab | Request body |
   |---|---|---|---|
   | Tracker weekly | Saturdays, 8:00 AM | `0 8 * * 6` | `{"ref":"main","inputs":{"mode":"weekly"}}` |
   | Tracker live | Weekdays, 9:05 AM to 4:45 PM and 7:05 to 11:45 PM, every 20 min | `5,25,45 9-16,19-23 * * 1-5` | `{"ref":"main","inputs":{"mode":"live"}}` |
   | Tracker live, US night | Tuesday to Saturday, 12:05 to 2:45 AM, every 20 min | `5,25,45 0-2 * * 2-6` | `{"ref":"main","inputs":{"mode":"live"}}` |

   Each sends these headers: `Accept: application/vnd.github+json`, `Authorization: Bearer YOUR-TOKEN`,
   `X-GitHub-Api-Version: 2022-11-28` and `Content-Type: application/json`. A successful call returns 204.

   The workflow also has its own Saturday 9:00 AM schedule as a backup. If the 8:00 AM run happened, it finds no new data and
   stops. If the token has expired, the table still updates, late, but profile prices stop.

## Embedding in an article

Paste `embed/wordpress-embed.html` into the editor's Code tab or a Custom HTML block. Use `embed/amp-embed.html` for AMP pages.

- The block fills the reading column (659 to 760px, depending on the screen), so its edges line up with the article text. The
  table scrolls sideways with its company column fixed, and sector names and column titles wrap so the market cap columns fit.
- Don't write an iframe tag or inline script in the article. On no-sidebar articles the theme's server code moves an iframe tag
  out of the column to the full width of the window, whatever its CSS, and splits the article in two. Inc42's app doesn't run
  scripts and prints their text as words. So the block is a figure and one script tag, and `site/embed.js` builds the frame.
- The script tag pins `embed.js` with an integrity hash, because it runs on inc42.com. If the file changes, every article needs
  the new hash from `openssl dgst -sha384 -binary site/embed.js | openssl base64 -A`, or browsers block it and only the link shows.
- `embed.js` also sizes the frame to the table, including when a filter shortens it. The script tag needs an editor or
  administrator account. In the app, or without the script, the reader sees only the "Open the tracker" link.
- Profiles open inside the frame. The back link returns to the table with the reader's filters, sort and scroll in place. In AMP
  they open in a new tab, because AMP allows no script to bring them into view.
- inc42.com sets no `frame-src` rule today, so the frame is allowed. If one is added, it must include
  `https://speedrunnr.github.io`.
- Recommended: a DNS CNAME such as `tracker.inc42.com` → `speedrunnr.github.io`, then Settings → Pages → Custom domain. Articles
  then embed an Inc42 address, and the widget can move hosts later without editing them.

The page posts `{type: "inc42-tracker-height", height}` and `{type: "inc42-tracker-page", page}` to its parent, plus AMP's
`embed-size`. The public root opens on the table, always in the light theme, and pages carry `noindex` so the widget doesn't
compete with the article in search. Other sections are at `?widget=highlights`, `sectors`, `hubs`, `years`, `time` and `about`.

## Live prices on profiles

The table stays on its Saturday close. Profiles follow the market. Each profile reads a small file of its own,
`data/live/<ID>.json` (about 2 KB), when it opens and every 5 minutes while it stays open, so readers never download every
company's prices.

Price, day's range, market cap, gains, 52-week range, P/E, P/B and the 1D and 5D charts follow the live data. Financials and
news stay weekly. Nasdaq names have no intraday bars, so they update their price but not a 1D chart.

The line under the price says what the reader sees: `Live`, `Delayed` (over 30 minutes old in an open market; 75 for Nasdaq),
`No trades yet today`, `Last updated` (the last update came before that session's close), `Market closed`, or the Saturday
wording when a profile has no live file. Prices are delayed from the exchange, and the page footer says so.

## USD/INR

Conversions matter wherever Nasdaq names meet Indian ones: the table's market cap and sales sorts, and every combined total.
All of them read one rate.

- `live_quotes.py` fetches `USDINR=X` on every live run and writes `data/live/fx.json`. It keeps moving after NSE closes, so a
  rate change alone is enough to publish.
- The page reads that file when it opens. If it can't, it uses the rate saved at the last refresh, which was also fetched, and
  the table's notes always show the rate in use and when it was fetched.
- A quote outside 50 to 200, or more than 5% from the last rate, is treated as a bad tick and ignored. If no rate can be found
  at all, the refresh stops rather than convert at a number nobody fetched.

Prices and market caps stay in each company's own currency: rupees for Indian listings, dollars for Nasdaq.

## Adding a company

Readers never see this. The table has no Add company button on the public site, and only people with write access to this
repository can run the form.

1. On GitHub, open Actions → *Add a company* → Run workflow.
2. Enter the name, Yahoo ticker and sector, spelled exactly as in the tracker. A different spelling creates a new sector in the
   filter. Everything else is optional and comes from Yahoo if blank.
3. Run it. Repeat for each company: requests can be sent one after another at any time on a Saturday.
4. Actions → *Refresh and deploy* shows the run that adds them. When it finishes, the table, filters and profiles are live.

Each request is saved to `data/queue/`, and every run of *Refresh and deploy* adds whatever is waiting, so nothing is lost if
several arrive together or a weekly run is in progress. A run started during the weekly refresh waits for it to finish.

**What updates:** every company's market cap and returns are recalculated. The new company gets its table row, profile, live
prices and Inc42 stories, and joins the company count, search, filters and sorting. On a Saturday the prices are already Friday's
close, so existing rows stay as they were. On a weekday, adding a company moves the whole table to the latest close.

**What to check on the new row:**

| Field | Where it comes from if left blank |
|---|---|
| Sales, YoY, net profit | Yahoo's latest annual statement. Can be blank or differ from Inc42's figures |
| Debut market cap | Listing price × today's shares |
| Listing price | Yahoo's first-day open. Blank for symbols with only recent history, such as NSE SME |
| Shares | Yahoo, not the NSE filing |

Enter the figures you trust in the form, or correct `config/companies.csv` afterwards and run *Refresh and deploy* in `deploy`
mode. If Yahoo rejects a ticker, the others still go through, the run turns red, and the reason is on its summary page.

## Running it locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python refresh.py              # weekly refresh (exit 0 written, 2 validation failed, 3 no new market date)
.venv/bin/python refresh.py --force      # write even if the market date has not moved
.venv/bin/python refresh.py --rebuild    # rebuild the pages from saved data after editing site/ or settings
.venv/bin/python live_quotes.py --publish
.venv/bin/python check_site.py
.venv/bin/python -m http.server 8000 --directory public
```

## Design

The light theme takes its values from the Inc42 article: Noto Sans, `#030303` / `#333` / `#666` text, 22px bold section heads,
4 to 8px corners, the `#FFF6F6` tint of the Summary box, and the nav bar's gradient (`#D81757` to `#E4381B`). Green and red only
ever mean gains and losses.

## Method

- Current market cap = latest close × total shares. Shares come from the NSE shareholding pattern where available
  (`shares_asof` = filing quarter), otherwise Yahoo. Splits, bonuses and consolidations after `shares_asof` are applied
  automatically from Yahoo's split events.
- Listing price = Yahoo's first-session open, already adjusted for later corporate actions; splits after `baseline_date` are
  applied each run. TAC Infosec, Menhood and Mobavenue keep editorial values (see `note`).
- Nasdaq names (Freshworks, MakeMyTrip) use Yahoo's USD market cap.
- Debut market cap and FY26 financials are editorial and do not change on refresh.
- Validation: at least 90% of companies must have a close in the latest market week; week moves above 40% without a split are
  flagged; a run during Indian market hours drops the day's partial bar.

## Looking after it

- **A run failed:** Actions shows the log. Most failures are Yahoo hiccups and the next run recovers.
- **The token expires:** create a new one and update all three cron-job.org jobs before it does.
- **Changed the code, settings or company data:** run *Refresh and deploy* in `deploy` mode. It needs no download.
- **Yahoo blocks GitHub:** run the build on a computer you control, commit the results, and let GitHub only host the files.
- **Limits:** GitHub Pages has a soft bandwidth limit of about 100 GB a month. A table view is about 100 KB and a profile about
  50 KB, which covers about a million views.

## Data source

Yahoo Finance data is licensed for personal use only. Prices come through `fetch()` in `refresh.py` and `fetch()` in
`live_quotes.py`, while `build_profiles.py` and `add_companies.py` also read Yahoo for price history and statements. Swap these for a
licensed feed (same output: daily closes and split events per ticker, plus USD/INR).

For information only, not investment advice. Inc42 does not rate or recommend securities.

## The private artifact

`artifact/` is the same tracker as a claude.ai artifact, which reads prices from its own database and keeps an owner-only Add
company button. Saved profiles live in `artifact/data/companies/`, and `public/` copies them from there.
