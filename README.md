# Post Link Collector (no API)

A local app that collects **post links** for a **chosen date range** from **Instagram, X, TikTok, YouTube, Threads and
Facebook** — either from a list of **profiles**, or from each platform's **search for keywords**. No paid APIs, no AI,
no engagement data — just the post links with their dates.

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
  - YouTube: read with `yt-dlp` (Videos, Shorts and Live tabs). No login needed.
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

## Using the dashboard

| Page | What it does |
| --- | --- |
| **Collect** | Pick the period (presets or any dates, WIB/WITA/WIT), toggle platforms, start/stop, live progress |
| **Accounts** | All accounts grouped by unit: search, filter, select/deselect per account or unit, **Add profiles** for any other account (any platform), remove added ones |
| **Post Links** | Every link found, filterable by platform/search, plus Excel/CSV download |
| **Account Status** | Accounts that were not found, need login, errored, or had no posts in the period |
| **Keyword Search** | Keywords (as many as you like, `#hashtags` too), period, platforms and advanced options; start/stop |
| **Keyword Links** | Links found per keyword (filter by keyword/platform/author), search status per keyword × platform, Excel/CSV |
| **Exports** | Excel files from earlier runs |
| **Activity Log** | Live log of the current job |

First time: click **Log in to platforms** (bottom left), sign in on each tab (Instagram, X, TikTok, Threads, Facebook),
then close that browser window. Sessions are stored in `browser_profile/` on your computer (never committed).
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
| YouTube | Search sorted by upload date with `after:`/`before:` (yt-dlp, no login) | **Full** |
| Threads | Search, *Recent* tab | **Partial** — newest first, limited depth |
| Facebook | Post search with the *Recent posts* filter | **Partial** — best effort, unstable |
| Instagram | `#hashtag` page and keyword search page | **Top only** — Instagram shows popular posts, not every post |
| TikTok | Video search | **Top only** — ranked by relevance, not date |

Advanced options:
- **Max links per keyword per platform** (50 / 100 / 300 / 1,000 / no limit) — keeps runs short and your accounts safe.
- **Exact phrase** — multi-word keywords searched as one phrase (X, YouTube, Facebook).
- **Exclude words** (X, YouTube).
- **X**: language (any / Indonesian / English), exclude replies, exclude reposts. A live preview shows the exact X query.
- **Instagram**: use the hashtag page, the keyword search page, or both.

A keyword only matches what the platform's search indexes (captions, hashtags, titles) — not text inside images or
videos. A link found by several keywords appears once, with all its keywords.

Output: `keyword_links_<period>_<time>.xlsx` with sheets **Post Links** (Keyword, Platform, Author, Date, Time,
Post Link), **Search Status** and **Info** (the options used), plus a `.csv`.

## Output (profiles)

Saved to `outputs/` after every account, and downloadable from the dashboard:

- `post_links_<period>_<time>.xlsx`
  - **Post Links**: No, Platform, Unit, Handle, Date, Time, Post Link (clickable)
  - **Account Status**: per-account status (OK / Not found / Login required / Invalid username / Error) and link count
  - **Info**: period and creation time
- a `.csv` with the same links.

## Account base list

- `data/accounts.csv` is the base list: official accounts of 51 Ditjen SDA units on six platforms, with columns
  Unit, Platform, Handle, URL, Status, Source, Notes. It was checked through web search in November 2026:

  | Status | Meaning |
  | --- | --- |
  | **Verified** | The profile itself, or an official balai site/channel linking it, appeared in search results |
  | **Likely** | Strong but indirect evidence (e.g. linked from the balai's YouTube channel), or the account looks inactive |
  | **Unverified** | Handle from the original list with a valid format, not seen in search. Still collected — check **Account Status** after a run and remove it if "Not found" |
  | **Not found** | The original handle cannot exist on that platform and the real account wasn't found. Kept in the CSV for reference; not collected |

- Profiles added from the dashboard go to `data/custom_accounts.csv`.
- You can edit either CSV in Excel (save as CSV UTF-8). The dashboard picks up changes on reload.

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
| `crawler/youtube.py` | YouTube via yt-dlp (channels and search) |
| `crawler/keywords.py` | Keyword search per platform + query options |
| `crawler/store.py` | Base + custom account lists |
| `crawler/job.py` | Background login/collection jobs, saving results |
| `crawler/output.py` | Excel/CSV writer |
| `crawler/server.py`, `crawler/static/index.html` | Local dashboard (reachable only from this computer) |
| `data/accounts.csv` | Base account list with verification status |
| `tests/` | Unit tests and end-to-end flow tests with fake platform pages (`python -m pytest tests`) |
