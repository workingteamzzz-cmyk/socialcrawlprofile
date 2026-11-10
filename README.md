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

To update: close the app, extract the new ZIP anywhere and start it. While this copy has not been signed in yet, it
looks for an older copy of the app (next to it, or in Downloads, Desktop, Documents or your home folder) and copies its
logins — the current profile is kept as a backup. Lists are copied once the same way.

If the old copy is somewhere else, use **Import logins from an older copy…** (sidebar): pick a copy that was found,
or paste the old app folder (the one with `run.py`) or its `browser_profile` folder.

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
After the links of an account/search are collected, caption and numbers are read the quickest way that works, the way
a person would see them:

1. **X** — the replies / reposts / likes / views under each tweet are read while scrolling; no tweet is opened.
2. **TikTok, YouTube, Threads, Instagram** — post pages are read a few at a time in the background with your own
   session (like background tabs; 2 at a time on Instagram, which is the quickest to say "try again later").
3. Only what could not be read that way (and Facebook) is opened one by one, without idle waiting.

| Platform | Caption | Likes | Comments | Shares / reposts | Views |
| --- | --- | --- | --- | --- | --- |
| X | ✓ | ✓ | ✓ (replies) | ✓ (reposts) | ✓ |
| YouTube | ✓ (title + description) | ✓ | ✓ | – | ✓ |
| TikTok | ✓ | ✓ | ✓ | ✓ | ✓ |
| Instagram | ✓ | ✓ (unless hidden) | ✓ | – | reels, when shown |
| Threads | ✓ | ✓ | ✓ (replies) | ✓ (reposts + quotes) | – |
| Facebook | ✓ | ✓ (all reactions) | ✓ | ✓ | videos, when shown |

The Activity Log shows how long it took, e.g. `details: 120 posts in 45s (110 read without opening, 10 opened)`.
The numbers are a snapshot at collection time. Empty cells mean the page did
not show that number. The Excel gets extra columns Caption, Likes, Comments, Shares / Reposts, Views.

## News from Google

Three sources, all on by default (*Advanced options → Sources*); an article found by several appears once:

- **Google News feed** (most reliable): the browser asks news.google.com for the keyword per day and gets a
  structured list (title, media, exact publish time — up to 100 articles per search). The feed only gives
  news.google.com links, so *Get the media's own link* turns them into the media's own URL without opening them: older
  links contain the URL in their id; newer ones are translated by Google News itself in the background (the same request
  its redirect page makes), a few at a time — also in *Full article* mode, which then opens the media page directly.
Only if that fails is the link opened briefly (as `news.google.com/articles/…`; the feed's `/rss/articles/…` form only
shows a blank page). Turn it off to keep the
  news.google.com links (they still open the article).
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

- Chrome shows a bar "You are using an unsupported command-line flag: --disable-blink-features=AutomationControlled".
  It is harmless — close it. The flag keeps TikTok and others from treating the window as a bot.
- If X shows nothing, the Activity Log says why (asks to log in, account locked, rate limited, "Something went wrong").
  X's search is tried first; when it stays empty the account's profile timeline is read instead.
- Platforms can change their pages at any time; if one platform suddenly returns 0 links for every account, its page
  layout probably changed.
- Random pauses between scrolls and accounts are deliberate so the logged-in accounts aren't rate-limited. Avoid
  back-to-back runs.
- Collab posts created by other accounts, stories and deleted posts are not included.

## Schedules (searches that run by themselves)

Set a search up on the **News Search**, **Keyword Search** or **Collect** page as usual, then click
**Schedule it (runs by itself)…** instead of Start. Choose how often (every day at a set time, or every 1–12 hours)
and which period each run collects (yesterday, today so far, yesterday + today, or the 7 days before). The
**Schedules** page lists them with the next run and the last result; *Run now*, *Pause* and *Delete* are there too.

- Each run saves its own Excel file (Exports). One search runs at a time: a schedule that comes due while another
  search runs starts right after it; runs missed while the app was off are done once when it is back.
- On the server they run 24 hours a day; on a laptop only while the app is open.
- `NOTIFY_WEBHOOK_URL` (Slack/Teams/n8n) gets a short message when a scheduled run finishes.

## Sending the results to another server (a client's API, n8n, Make, Zapier)

When a search finishes, its links can be **pushed** as JSON to another server:

- per schedule: *When finished, send the results to* (+ optional *access token*) in the schedule window;
- every finished search, scheduled or by hand: `RESULTS_WEBHOOK_URL` (+ `RESULTS_WEBHOOK_TOKEN`) in `.env`.

```
POST <their URL>
Content-Type: application/json
Authorization: Bearer <token>                       (when a token is set)
X-Collector-Signature: sha256=<HMAC-SHA256 of the body, key = token>

{"event": "results.ready", "id": "…unique…", "sent_at": "2026-11-10T07:00:41+0700", "source": "post-link-collector",
 "kind": "news", "schedule": {"id": "…", "name": "News: tarif listrik"},
 "period": {"start": "2026-11-09", "end": "2026-11-09", "tz": "WIB"},
 "ok": true, "error": null, "count": 39, "file": "news_links_….xlsx", "download_path": "/download/news_links_….xlsx",
 "links": [{"keyword": "tarif listrik", "url": "https://…", "title": "…", "media": "…", "published": "2026-11-09"}, …]}
```

`links` has the same columns as the Excel file (up to 5,000 rows; the file itself can be fetched from
`download_path` with an `X-API-Key`). Any 2xx answer counts as delivered; otherwise it is retried after 30 s, 2 min
and 10 min, and the Schedules page shows ✓ or ✗. The receiver can check the signature, e.g. in Python:

```python
import hashlib, hmac
ok = hmac.compare_digest(request.headers["X-Collector-Signature"],
                         "sha256=" + hmac.new(TOKEN.encode(), request.body, hashlib.sha256).hexdigest())
```

They can also **pull** whenever they like: `GET /api/results?mode=news` with their own key (see *Using the API*).

## Run as an API server (FastAPI, Docker)

The same app can run on a VPS/cloud machine as an API (FastAPI) — always on, without your laptop: the same dashboard,
JSON endpoints for scripts, n8n, Power Query and the like, and interactive docs at `/docs`.

**Read this first — a server is harder for this kind of tool than your laptop:**
- Instagram, X, TikTok, Threads and Facebook trust data-centre IP addresses much less than a home/office connection.
  Expect more login checks, captchas and empty results; Google may ask for captchas sooner (the Google News feed
  source is the least affected). A VPS in Indonesia helps.
- There is no screen: logins come from **cookies you import** (below), and a verification ("I'm not a robot") is
  solved by a person **from the dashboard** (below).
- The cookies let the server act as those accounts. Use separate accounts for collecting.

### How the data is protected

| Protection | How |
| --- | --- |
| HTTPS only | Caddy in front gets and renews the certificate itself; plain HTTP is redirected; HSTS. The app port is not published. |
| A key per person | `COLLECTOR_API_KEYS="ana=…,budi=…"`. Remove someone's entry and restart → their key and dashboard sign-in stop working at once. Keys are never stored in the sign-in cookie or the log. |
| Sign-ins expire | Dashboard sign-in lasts `SESSION_DAYS` (default 7); HttpOnly, Secure, SameSite=Strict cookie; changes are only accepted from the app's own page. |
| Guessing is blocked | 10 wrong keys from one address in 10 minutes → that address is refused for a while. The server refuses to start without keys (16+ characters each). |
| Optional office-only access | `ALLOWED_IPS=203.0.113.7,198.51.100.0/24` — everyone else gets "forbidden", even with a key. |
| Audit log | Who signed in, started/stopped what, imported cookies (which platforms, never the values), downloaded which file, changed which list — in the dashboard (*Exports → Access log*), `GET /api/audit` and `/data/audit.log`. |
| Less data kept | Result files are deleted after `RETENTION_DAYS` (30), page copies after `DEBUG_RETENTION_DAYS` (7). |
| Locked-down container | Runs as a normal user, read-only system files, no extra privileges (`cap_drop: ALL`, `no-new-privileges`), memory/process limits. Only `/data` is writable. |
| Security headers | Content-Security-Policy, no framing, no referrer, no caching of API answers, no server banner. |
| Secrets stay out of the image | Keys live only in `.env` on the server (excluded from the image and from git). |

What still depends on you: keep the server updated (`apt upgrade`, `docker compose pull && docker compose up -d --build`),
use SSH keys (no password logins) and a firewall that only opens ports 22, 80 and 443, turn on your provider's disk
encryption/backups if offered, and treat the `collector-data` volume (logins, results) as confidential.
For data that must stay in Indonesia (UU PDP), choose a provider with an Indonesian data centre.

### Quick server setup (one command)

1. Rent a VPS with **Ubuntu 24.04**, 2 vCPU and 4 GB RAM or more (e.g. Biznet Gio NEO Lite MS 4.2, Jakarta,
   monthly). Note its IP address and root password.
2. On your computer (Windows PowerShell or Mac Terminal), copy the app to the server:

```bash
scp post-link-collector.zip root@<server-ip>:~
```

3. Log in to the server and run the installer:

```bash
ssh root@<server-ip>
```

```bash
apt-get update && apt-get install -y unzip && unzip -o post-link-collector.zip && cd post-link-collector && bash install-server.sh
```

It asks for the names of the people who may use the app and an address (press Enter for a free HTTPS address like
`103-1-2-3.sslip.io`, or type your own domain), then installs Docker, a firewall, automatic security updates and
swap, starts everything and prints the address and each person's key (also saved in
`/root/post-link-collector-keys.txt`). For a new version: upload the new zip, unzip it and run the same command —
settings and data are kept.

### Start it (manually)

1. Get a VPS (2 vCPU / 4 GB RAM is enough) with Docker, and point a domain at it (DNS A record).
2. Copy the app there, then:

```bash
cp .env.example .env          # fill in DOMAIN and one key per person: openssl rand -hex 24
docker compose up -d --build
```

Open `https://<your domain>` and sign in with your key. Without Docker: `pip install -r requirements-server.txt`,
`python -m playwright install --with-deps chromium`, then `COLLECTOR_API_KEYS=... COLLECTOR_SERVER=1 python serve.py`
behind your own HTTPS proxy. All settings are listed at the top of `serve.py` and in `.env.example`.

### Sign in to the platforms (cookies)

1. On your own browser, sign in to the platform (ideally a separate account for collecting).
2. Export that site's cookies with an extension such as **Cookie-Editor** (Export → JSON).
3. On the server's dashboard click **Import cookies** and paste them (or choose the file) — or send them to the API.
   JSON from cookie extensions, Playwright storage state and `cookies.txt` are accepted; only cookies of the supported
   platforms and Google/YouTube are kept. Repeat per platform; re-import when the sidebar shows ✗. Delete the exported
   file from your computer afterwards.

### "I'm not a robot" on a server — who clicks?

You (or anyone with a key), from the dashboard — on a laptop or a phone. When Google or a platform asks for a
verification, the job pauses and the dashboard opens a **Verification needed** panel with a live picture of the
server's browser. Click on the picture exactly as you would on the page (tick the box, pick the images, press
Verify); type text if asked. The job continues by itself once the page is through.

- Nobody within `CAPTCHA_WAIT_MINUTES` (default 10) → that search is skipped. For news, Google Search is then left
  alone for the rest of the run (more requests only make the block longer) while the **Google News feed** keeps
  working — the feed rarely asks for a verification (tested from a cloud server: hundreds of links without one).
- To be told when it happens, set `CAPTCHA_WEBHOOK_URL` (a Slack/Teams incoming webhook, or an n8n/Make webhook that
  forwards to WhatsApp/Telegram) and `PUBLIC_URL` (the dashboard's address). It receives `{"text": "..."}`.
- Fewer verifications: start with the Google News feed as the source, keep periods and keyword lists modest, import
  Google cookies of a signed-in account, and prefer a VPS in Indonesia.
- The app never sends verifications to paid "captcha solving" services.

### Using the API

Send your key in the `X-API-Key` header (or sign in on the dashboard). One job runs at a time.

```bash
KEY=your-key; URL=https://collector.example.com
curl -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
     -d '{"keywords": ["tarif listrik"], "start": "2026-11-01", "end": "2026-11-09", "options": {"cap": 300}}' \
     $URL/api/news/start
curl -H "X-API-Key: $KEY" "$URL/api/status"                 # running? progress, log lines
curl -H "X-API-Key: $KEY" "$URL/api/results?mode=news"      # the links as JSON
curl -H "X-API-Key: $KEY" "$URL/api/outputs"                # Excel files; download: /download/<name>
```

| Endpoint | What it does |
| --- | --- |
| `POST /api/start` | Post links from accounts (`text` with profile links, or `list` + `ids`), `platforms`, `detail` |
| `POST /api/keywords/start` | Keyword search on the platforms (`keywords`, `platforms`, `options`) |
| `POST /api/news/start` | News from Google (`keywords`, `options`: sources, cap, exclude, detail ...) |
| `POST /api/stop` | Stop; links collected so far are kept |
| `GET /api/status`, `/api/results?mode=profiles\|keywords\|news`, `/api/outputs`, `/download/<file>` | Progress, results, files |
| `GET /api/logins`, `POST /api/logins/cookies` | Signed-in status (and who you are); import cookies |
| `GET /api/audit` | Who did what |
| `GET /api/schedules`, `POST /api/schedules/save` / `delete` / `run` | Searches that run by themselves |
| `GET /api/captcha/screenshot`, `POST /api/captcha` | The waiting verification's picture; click/type/key/scroll/skip |
| `GET/POST /api/lists...`, `/api/accounts...` | Account lists |
| `GET /api/health` | Up? (no key needed — for uptime monitors) |

All of them, with request examples, are in `/docs`.

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
| `crawler/api.py`, `serve.py` | The FastAPI server (keys per person, audit log, `/docs`, cookie import) |
| `crawler/retention.py` | Deletes old result files and page copies on a server |
| `crawler/captcha.py` | Lets a person solve a verification from the dashboard when the browser runs on a server |
| `crawler/schedule.py` | Searches that run by themselves (Schedules page) |
| `crawler/delivery.py` | Sends finished results to another server as JSON (token, signature, retries) |
| `crawler/cookies.py` | Reads exported cookies (Cookie-Editor JSON, storage state, cookies.txt) |
| `Dockerfile`, `docker-compose.yml`, `Caddyfile`, `.env.example`, `requirements-server.txt` | Running it on a server (HTTPS, locked-down container) |
| `install-server.sh` | One-command setup on a fresh Ubuntu VPS |
| `data/lists/` | Account lists; `Ditjen SDA.csv` is the bundled example |
| `tests/` | Unit, end-to-end and stress tests with fake platform pages (`python -m pytest tests`, about 2 minutes) |
