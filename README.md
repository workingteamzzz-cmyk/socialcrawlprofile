# Post Link Collector (no API)

A local app that collects **post links** for a **chosen date range** from **Instagram, X, TikTok, YouTube, Threads and
Facebook** — from lists of **profiles** or from each platform's **search for keywords** — and **news links from Google**.
It works the way a person does, in a real browser on your computer. No paid APIs, no AI.

What you get is chosen per run:
- **Links only** (fastest), or
- **Links + caption & engagement** for social posts (caption, likes, comments, shares/reposts, views), or
- for news: **links only**, **links + title, media & date**, or **full article** (title, author, published time, text).

## Download

- **Latest version (ZIP):** https://github.com/workingteamzzz-cmyk/socialcrawlprofile/archive/HEAD.zip
- Extract the ZIP and follow *Install & run* below.

## How it works

- The app drives a **real browser on your computer** (Chrome/Edge, or Chromium if neither is installed) with your own
  logins, opens each profile, scrolls, and reads the post links on the page.
- **Dates are decoded from the post ID**, so posts never have to be opened one by one:
  - Instagram & Threads: the `/p/…`, `/reel/…`, `/post/…` shortcode
  - X: the status ID (snowflake), plus X search `from:user since:… until:…` to narrow to the period
  - TikTok: the video/photo ID
  - YouTube: the channel's own feed gives the ~15 newest uploads with exact dates; older ones come from the
    Videos, Shorts and Live tabs, each video's date read from its page by the same browser. No login needed.
  - Facebook: read from the data the page loads while scrolling. The most fragile platform — best effort.
- Scrolling stops on its own once it passes the start date; pinned (old) posts don't stop it early.

## Install & run

1. Install **Python 3.10+**. Windows: https://www.python.org/downloads/ (tick **Add Python to PATH**).
   Mac: use the .pkg installer from python.org.
2. Download the ZIP (link above) and extract it.
3. Run:
   - **Windows:** double-click `start-windows.bat`
   - **Mac:** double-click `start-mac.command` (first time: right-click → Open → Open)
   - **Linux:** `./start-mac-linux.sh`
4. The first start installs the packages and a Chromium browser (a few minutes). Then http://127.0.0.1:8770 opens.
   Keep the black server window open while you use the app.

## Where your data lives (and updating)

The app keeps your data **outside the app folder**, so a new version keeps everything:

| What | Where |
| --- | --- |
| Account lists (CSV, open in Excel) | `Documents/Post Link Collector/lists` |
| Results (Excel/CSV) | `Documents/Post Link Collector/outputs` |
| Platform logins (browser profile) | Windows `%LOCALAPPDATA%\PostLinkCollector`, Mac `~/Library/Application Support/PostLinkCollector` |

To update: close the app, extract the new ZIP anywhere and start it. On its first start it copies logins and lists from
an older copy of the app found next to it (e.g. `post-link-collector` next to `post-link-collector (1)`), once.

Logins are reopened with the same browser every time (Chrome, Edge or Chromium encrypt saved logins differently, so
switching would log you out). The sidebar shows which platforms are signed in (✓/✗), checked at every login and run.
A platform can still sign you out on its own after a while — then use *Log in to platforms* again for that one.

## Using the dashboard

| Page | What it does |
| --- | --- |
| **Collect** | Pick the period (presets or any dates, WIB/WITA/WIT), toggle platforms, start/stop, live progress |
| **Accounts** | Several **account lists** (per client, project or topic): switch, create or delete lists; accounts grouped by **group** (organisation, brand, person); search, filter, select per account or group; **Add profiles** (any platform, `URL | Group`, or two columns pasted from Excel); remove accounts |
| **Post Links** | Every link found, filterable by platform/search, plus Excel/CSV download |
| **Account Status** | Accounts that were not found, need login, errored, or had no posts in the period |
| **Keyword Search** | Keywords (as many as you like, `#hashtags` too), period, platforms and advanced options; start/stop |
| **Keyword Links** | Links found per keyword (filter by keyword/platform/author), search status per keyword × platform, Excel/CSV |
| **News Search** | Keywords, period, output level (links / + title, media, date / full article), region, exact phrase, excluded words, only-these-sites |
| **News Links** | News links per keyword with the chosen details, search status, Excel/CSV |
| **Exports** | Excel files from earlier runs |
| **Activity Log** | Live log of the current job |

First time: click **Log in to platforms** (bottom left), sign in on each tab (Instagram, X, TikTok, Threads, Facebook),
then close that browser window. Sessions are stored in your user folder (see above), never in the app folder.
Use a regular account rather than a main office account.

During a run, keep the browser window the app opens (it can stay in the background) and solve any captcha there
(TikTok shows them most). **Stop** keeps everything collected so far.

## Keyword search

Each keyword is searched separately on each chosen platform, using the platform's own search in your logged-in
browser. Only links inside the period are kept (dates come from the post IDs). How much you get depends on the
platform's search:

| Platform | Search used | Coverage |
| --- | --- | --- |
| X | `keyword since:… until:…` on the *Latest* tab | **Full** — date-filtered, scrolls to the end of the period |
| YouTube | Search sorted by upload date with `after:`/`before:` (no login needed) | **Full** |
| Threads | Search, *Recent* tab | **Partial** — newest first, limited depth |
| Facebook | Post search with the *Recent posts* filter | **Partial** — best effort, unstable |
| Instagram | `#hashtag` page and keyword search page | **Top only** — Instagram shows popular posts, not every post |
| TikTok | Video search | **Top only** — ranked by relevance, not date |

Advanced options:
- **Max links per keyword per platform** (50 / 100 / 300 / 1,000 / no limit) — keeps runs short and your accounts safe.
- **Exact phrase** — multi-word keywords searched as one phrase (X, YouTube, Facebook).
- **Exclude words** — sent to X/YouTube as `-word`, and on every platform each result's visible text is read: a post
  with an excluded word (also as a hashtag or a longer form, e.g. `promo` → `#PromoHemat`, `promosi`) is dropped.
- **Only posts that mention the keyword** (on): drops results whose text does not mention the keyword at all —
  searches like Threads or TikTok add loosely related posts. With caption & engagement on, the full caption is read too.
- **X**: language (any / Indonesian / English), exclude replies, exclude reposts. A live preview shows the exact X query.
- **Instagram**: use the hashtag page, the keyword search page, or both.

A keyword only matches what the platform's search indexes (captions, hashtags, titles) — not text inside images or
videos. A link found by several keywords appears once, with all its keywords.

Output: `keyword_links_<period>_<time>.xlsx` with sheets **Post Links** (Keyword, Platform, Author, Date, Time,
Post Link), **Search Status** and **Info** (the options used), plus a `.csv`.

## Caption & engagement (optional)

Choose **Links + caption & engagement** under *Output* (Collect page) or *Advanced options → Output* (Keyword Search).
After the links of an account/search are collected, the browser opens each post and reads what the page shows:

| Platform | Caption | Likes | Comments | Shares / reposts | Views |
| --- | --- | --- | --- | --- | --- |
| X | ✓ | ✓ | ✓ (replies) | ✓ (reposts) | ✓ |
| YouTube | ✓ (title + description) | ✓ | ✓ | – | ✓ |
| TikTok | ✓ | ✓ | ✓ | ✓ | ✓ |
| Instagram | ✓ | ✓ (unless hidden) | ✓ | – | reels, when shown |
| Threads | ✓ | ✓ | ✓ (replies) | ✓ (reposts + quotes) | – |
| Facebook | ✓ | ✓ (all reactions) | ✓ | ✓ | videos, when shown |

It takes roughly 3–8 seconds per post, and the numbers are a snapshot at collection time. Empty cells mean the page did
not show that number. The Excel gets extra columns Caption, Likes, Comments, Shares / Reposts, Views.

## News from Google

Three sources, all on by default (*Advanced options → Sources*); an article found by several appears once:

- **Google News feed** (most reliable): the browser asks news.google.com for the keyword per day and gets a
  structured list (title, media, exact publish time — up to 100 articles per search). Each Google News link is then
  opened once to catch the media's own article URL (*Get the media's own link*; about 1–3 s each — turn it off to keep
  the news.google.com links, which still open the article).
- **Google Search · News tab**: Google with the keyword and a custom date range, sorted by date, page by page.
- **Google Search · All results**: the regular "All" tab with the same date range, read **to the last page**. Only
  articles from news media are kept: a built-in list of Indonesian and international media, plus sites that clearly
  look like a news portal's article (e.g. `radarxyz.com/2026/10/20/...`). Social media, video sites, forums,
  marketplaces, wikis, company/government pages and files are skipped. Add a portal that is not recognised under
  *Also count as media*.

*Media only* (on) applies the same junk filter to the other two sources. Search status notes how many results were
read, skipped as non-media, or outside the period.

Google shows a limited number of results per search, so with **Search day by day** (on by default) a longer period is
searched one day at a time, newest day first. All three sources × every day means many Google searches — for long
periods start with a few keywords; if Google asks for a captcha ("unusual traffic"), solve it in the browser window
(the run waits up to 5 minutes).

Options: sources, media only, also count as media, get the media's own link, search day by day, max links per keyword (50 / 100 / 300 / 1,000 / no limit), region (Indonesia / global English), exact phrase, excluded words, only these
sites (e.g. `kompas.com, detik.com`), and the output level:
- **Links only** — keyword + link.
- **Links + title, media & date** — read from the result cards, no extra time.
- **Full article** — also opens each article and copies title, author, published time and the article text
  (paragraphs of the main article; "Baca juga"/related-link lines are skipped). Paywalled or multi-page articles may be partial.

Output: `news_links_<period>_<time>.xlsx` with sheets **News Links**, **Search Status** and **Info**, plus a `.csv`.

## Output (profiles)

Saved to `outputs/` automatically after each finished account or search (at most every 20 seconds, so big runs stay
fast), and always at the end or on Stop; downloadable from the dashboard. If one of these files is open in Excel
while a run is going, the app keeps collecting and saves again after the next account — close Excel to let it save.

- `post_links_<period>_<time>.xlsx`
  - **Post Links**: No, Platform, Group, Handle, Date, Time, Post Link (clickable)
  - **Account Status**: per-account status (OK / Not found / Login required / Invalid username / Error) and link count
  - **Info**: period and creation time
- a `.csv` with the same links.

## Account lists

- Each list is a CSV file in `data/lists/` with columns Group, Platform, Handle, URL, Status, Source, Notes.
  Create, fill and delete lists in the dashboard, or edit the CSV in Excel and reload the page. Comma or semicolon
  CSVs (Excel with Indonesian regional settings) and UTF-8 or plain "CSV (Comma delimited)" files are all read; the app
  keeps the file's separator when it saves. Close the file in Excel before adding or removing accounts in the dashboard.
  Deleted lists are kept as `*.csv.deleted` backups.
- Bundled example: **Ditjen SDA** — official accounts of 51 Ditjen SDA units on six platforms, checked through web
  search in November 2026:

  | Status | Meaning |
  | --- | --- |
  | **Verified** | The profile itself, or an official site/channel linking it, appeared in search results |
  | **Likely** | Strong but indirect evidence (e.g. linked from the unit's YouTube channel), or the account looks inactive |
  | **Unverified** | Handle with a valid format, not seen in search. Still collected — check **Account Status** after a run and remove it if "Not found" |
  | **Not found** | The handle cannot exist on that platform and the real account wasn't found. Kept in the CSV for reference; not collected |
  | **Added** | Added by you from the dashboard |

- Lists from earlier versions (`data/accounts.csv`, `data/custom_accounts.csv`) are moved into `data/lists/Ditjen SDA.csv`
  automatically on first start.

## When something returns 0 links

If a profile, keyword search or Google page loads but gives no links, the app keeps a copy of that page in
`outputs/debug/` and says so in the status notes. Send that file (with the Excel file) so the reader can be adjusted to
the site's current layout. Check it first — it can show the name of the account you are signed in with.

## Limitations

- Platforms can change their pages at any time; if one platform suddenly returns 0 links for every account, its page
  layout probably changed.
- Random pauses between scrolls and accounts are deliberate so the logged-in accounts aren't rate-limited. Avoid
  back-to-back runs.
- Collab posts created by other accounts, stories and deleted posts are not included.

## Project layout

| File | Purpose |
| --- | --- |
| `crawler/accounts.py` | Parses profile URLs, detects the platform, validates usernames |
| `crawler/idtime.py` | Decodes post dates from post IDs (IG/Threads/X/TikTok) |
| `crawler/browser.py` | Opens profiles in the browser, scrolls and collects links per platform |
| `crawler/keywords.py` | Keyword search per platform + query options |
| `crawler/details.py` | Optional caption & engagement readers per platform |
| `crawler/news.py` | Google News search + article text extraction |
| `crawler/store.py` | Account lists (`data/lists/*.csv`) |
| `crawler/job.py` | Background login/collection jobs, saving results |
| `crawler/output.py` | Excel/CSV writer |
| `crawler/server.py`, `crawler/static/index.html` | Local dashboard (reachable only from this computer) |
| `data/lists/` | Account lists; `Ditjen SDA.csv` is the bundled example |
| `tests/` | Unit, end-to-end and stress tests with fake platform pages (`python -m pytest tests`, about 2 minutes) |
