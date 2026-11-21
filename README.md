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
| **Home** | Monitoring overview from the report database: news / posts / negative sentiment / active media (vs the previous period), coverage trend per day, sentiment, top media, top keywords, posts per platform, latest coverage; period 7 / 14 / 30 / 90 days; the search box on top filters it all. System health (checklist of HTTPS, sign-ins, API, Telegram, safe mode, schedules) with the bell showing what needs attention. **New search** (top left) opens any search in one click |
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

## Excel layouts (the team's format)

Under *Output* every search page has **Excel layout**:

- **Standard** — this app's own files (several sheets: links, status, info).
- **Online (client format)** for news — one sheet with `DATE` (09-Nov-2026), `TIME` (HH:MM:SS), `TITLE`, `URL`, `IMAGE`,
  `MEDIA`, `INFLUENCER`, `PERSON`, `AUTHOR`, `SUMMARY`, `CONTENT` (`<p>…</p>`), `SENTIMENT`. IMAGE, AUTHOR, SUMMARY and
  CONTENT are filled with *Full article*.
- **Medsos (client format)** for keyword searches and accounts — `No`, `Sentiment`, `Username`, `User ID`, `Follower`,
  `Text`, `Date`, `Time`, `Likes`, `Reply`, `Shares`, `Url`, `Emotion`, `User Image`, `Platform` (twitter, instagram,
  threads, facebook, tiktok, youtube), `Sub Type`, `Views Count`, `Gender`, … `Post ID`, `User ID Parent`. Text and
  numbers are filled with *caption & engagement*; User ID / Follower / User Image where the page shows them.

Files are named like the team's: `20261108_Online_Manual_24.00.xlsx` (period end date, layout, *Manual* or *Auto* for
schedules, `24.00` for a whole past day). Columns that need text analysis (sentiment, emotion, people, demographics)
stay empty — the app does not use an AI model. Deliveries to another server carry the same rows in that layout.

## News Search — Multi-source engine (the team's medmascrawler)

**Live results.** Each article is saved the moment it has been read and accepted — into the reporting database (Home
dashboard, Reports) and the Excel file — not at the end of the run. Quick sources (Google News, the known RSS feeds,
Bing, GDELT) run first for every keyword; the slow sweep (media sitemaps, looking for new RSS feeds — once per run)
follows. With "only new" schedules, articles saved live still count as new for that run's file.

News Search has two engines (card **Search engine**):

- **Multi-source (recommended)** — the team's *medmascrawler* (`crawler/medmas`, its code unchanged), no browser and
  no captcha:
  - Google News RSS split by date until nothing is cut off at ~100, plus a `site:` search per outlet
  - news sitemaps and RSS feeds read straight from the media (rotating over a ~12,000-outlet dictionary;
    working feeds are remembered), Bing News, GDELT, optionally DuckDuckGo
  - Google News links decoded to the media's own link (links that fail are retried in the next runs, then
    rescued by searching their title on Bing)
  - every article read (4-layer extraction); listing/tag pages left out; dates enforced
  - keywords support `"exact phrase"`, `AND`, `OR`, `NOT`, `( )`; an article whose keyword is not found in its text
    is **needs review** (kept in the review file `news_links_review_<run>.xlsx`, not silently dropped)
  - INFLUENCER (people quoted speaking), PERSON (people named) and SENTIMENT filled
  - a database in the data folder (`news_multi/medmascrawler.db`) remembers every link across runs:
    *Only articles not delivered before* makes each file hold only new articles
  - own media dictionary: put `media_dictionary.xlsx/.csv/.txt` in the data folder to replace the bundled one
  - **Media checked per run**: *Normal* (400 sitemaps + 300 outlets probed for RSS per run — the whole dictionary
    turns in ~1.3 days with hourly runs) or *Wide* (1,000 + 1,000 — ~12 hours; each run takes longer). Working RSS
    feeds, once found, are read every run.
  - **Priority media**: domains that are always checked first in every run (per-outlet search, sitemaps, RSS
    discovery) — they never wait for their turn — and count as registered media.
  - Hourly monitoring: schedule it every hour, period *Yesterday + today*, *only what is new* on.
- **Browser (Google pages)** — the previous engine. Its Online file now also gets INFLUENCER/PERSON from the article text.

The output is the same: News Links, the Online / Standard Excel, schedules and deliveries. Schedules made before
keep the browser engine; make a new one to use Multi-source. A Multi-source run with every source on takes longer
(minutes per keyword; Google's decoding limit is the slow part).

### Social media through Google (News Search → Search target → Social media)

Searches Google (All results, day by day) limited to the chosen platforms, e.g.
`pln (site:instagram.com OR site:threads.com OR site:threads.net)`: the public posts Google has indexed — useful next to
each platform's own search. Only post links are kept (no profile / hashtag pages). The result goes into the **Medsos**
layout (Username from the link, Post ID, Sentiment & Emotion); *Full article* opens each post for caption, engagement
and the account; empty IDs / followers are filled with the API when it is set up. Runs on the browser engine (Google
may ask for a verification).

## Live News Stream (always on, every new article)

**Live Stream** (sidebar, News) is not a search that starts and ends: once an admin presses *Start stream* it keeps
running (also after a restart) and reads every new article from the registered media:

- **RSS engine** — every working RSS feed of the media dictionary (curated + all feeds the multi-source engine has
  discovered) is checked every 10 minutes; an unchanged feed answers "304 not modified" (almost free).
- **Sitemap engine** — every outlet's news sitemap (~12,000 outlets), how often depends on the outlet's tier.
- **Tiers** — every 10 minutes the outlets are ranked by the new articles they published in the last 7 days:

  | Tier | Who | Sitemap | RSS |
  | --- | --- | --- | --- |
  | Active | the top 1,000 of the last 7 days | every hour | every 10 min |
  | Semi-active | published in the last 7 days, not in the top | every 4 h | every hour |
  | Rare | nothing in 7 days, something in 30 | every 24 h | every 24 h |
  | Inactive | nothing in 30 days | every 3 days | every 3 days |
  | Unreachable | failed 3 times in a row | every 7 days | every 7 days |

  A new outlet is checked once right away; an outlet that publishes again is promoted at once. All numbers can be
  changed under Live Stream → Stream settings.
- **Every link not yet in the database** — when an outlet is visited, every article link seen (RSS, sitemaps and
  its front page) that is not in the database yet is taken, whatever its date: today's news first, older links behind
  them, up to 1,000 older links per visit (the rest on the next visits). "Recent only" can be switched back on.
- **Site check** — when the stream starts and every Sunday 02:00 WIB (adjustable) every registered site is checked:
  does it answer, does it list sitemaps. Sites that are down are not crawled at all (no feed, no sitemap) until the
  next check; a site that is back is put in the rotation at once. A site that looks down gets a second try; if most
  sites look down at once, the server's own connection is the problem and nothing is marked.
- **Late articles** — once a day (01:00 WIB) a sweep reads every reachable outlet's sitemaps for the last 2 days,
  including its dated archive sitemaps: articles listed late, or missed while an outlet was checked rarely, are taken
  then (known links are skipped; late links wait behind fresh news). So every outlet is looked at at least daily.
- **Safety** — each outlet's robots.txt is respected (pages it disallows are never opened), outlets can be excluded
  completely (e.g. one that forbids crawling without permission), one page at a time per outlet with a pause.
- **Every article of a day** — *Download every article of a day* writes one Online Excel (the uploader's rules) of
  everything the stream read for that day, even 100,000+ articles; it lands in Exports.
- **Readers** — 16 threads (2–64) read the new articles newest-first: one page at a time per outlet with a pause
  between pages, so no outlet is hammered.
- Every article read is kept in the stream database (7 days, adjustable). Articles matching a **stream keyword or any
  report topic** go to the reporting database at once — Home dashboard, Reports, Excel.

Measured on a 4-CPU test machine with local test sites: ~37 articles/second (≈ 3 million/day) using less than one CPU
core. On the real internet the pace is set by how many articles the media publish and how fast their sites answer; the
page shows the live pace and the "per day at this pace" figure.

## Richer data (v47)

- **Fresh-news lane**: the Live Stream keeps two queues — news from the stream's days (and undated links) and the
  backlog (older links, media-archive jobs). Readers always take fresh news first; one reader works the backlog, the
  others take it only when no fresh page can be read. "Waiting to be read" shows both.
- **The media list grows by itself**: an outlet News Search or Bulk links found that the list lacks is registered and
  read by the Live Stream from then on (platforms, aggregators and excluded outlets are left out; Stream settings).
- **Second tries**: articles that failed for a passing reason (timeout, connection, 5xx / 429, empty answer) are
  queued again after 3 hours, at most twice.
- **Compare with a list** (News Search): paste another service's links — the share already in your database, and for
  each missing one why (could not be read, filtered, outlet registered but article not found, outlet not in the list);
  read the missing ones at once, add the missing outlets.
- **Disk safety**: a Telegram message at 75% and at 90% full; under 5 GB free, older links are not taken in (news
  still is) until there is room again.
- **Follows a server resize**: `install-server.sh` sets the app's memory / CPU share from the server's size
  (`COLLECTOR_MEM`, `COLLECTOR_CPUS` in `.env`). After a resize, run it again.
- Newsroom policy pages ("Kode Etik Jurnalistik", "Standar Perlindungan Profesi Wartawan", "Susunan Redaksi …") are
  not news: skipped, and ones already stored leave News Stream and the exports.

## International (non-Indonesian) outlets — pause and catch up

The stream decides once a day which outlets are international: the address first (.id = Indonesian; another
country's ending such as .my, .sg, .co.uk = international), then for .com / .net / .org the language of the titles it
read (fewer than a quarter with Indonesian words, from at least 8 titles = international; fewer titles = not decided,
never paused). Media Database shows them (INTL, filter *International*) and an admin can mark an outlet either way.

Stream settings → **Pause international (non-Indonesian) outlets**: their feeds and sitemaps are not checked, so the
readers work on Indonesian news. Turned off again, they are checked at once, and the paused period shows as
"missed": **Catch up international outlets** for those dates (or any period) — up to 7 days back a sweep of their
sitemaps; longer, one media-archive job per outlet (Media Database → Media archive).

## When a site refuses the normal request (Googlebot / cloudscraper)

The article reader (the team's engine) tries a normal browser request, then — when refused — asks again as
"Googlebot", then with cloudscraper (gets past Cloudflare's bot check), then the AMP page. Live Stream → Stream
settings → *When a site refuses the normal request*: both steps can be switched off (on by default, as before). Off:
the Googlebot step becomes a plain retry with the normal headers; the cloudscraper step is skipped. Next to each switch:
how many articles that step brought in the last 7 days (and the share of all reads), so the cost of switching it off
is known first. Applies to the stream and to News Search / Bulk links within seconds.

## Smart automation (skip what is already known)

Schedules → **How often: Automatic** and **Period: Everything since the last run** (both the default for new schedules):

- **Since the last run**: each run searches from the start of the last run that finished well (a few minutes of
  overlap) — no fixed "yesterday / 7 days"; a gap while the server was off is covered by itself (max. 31 days back).
  API runs use it to the minute (fewer pages, fewer credits).
- **Automatic pace**: much new (a surge against the schedule's own recent runs) → the next run comes sooner (down to
  safe mode's minimum, at least 1 h); nothing new → later (up to 12 h). The log says when the pace changes.
- **Stop at what is known**: scheduled browser searches stop scrolling after two screens of posts that earlier runs
  collected; the API stops paging at a page of known posts. Known posts get no paid look-ups (comments, followers,
  user ID) and are not opened again for their details.
- **News from the Live Stream**: News Search and Bulk links take an article the Live Stream already read from its
  database instead of downloading it again.
- **One link, many keywords**: a post or article found by several keywords is one row with all of them (also API).
- Every run ends with a line like `📊 This run: 120 found · 35 new · 85 already in the database · 60 taken from the
  Live Stream · 14 credits · 3 min`; the Schedules page shows the week's totals and each schedule's new results per run.

## Keyword Search with the API

Keyword Search → **Search engine**: *Browser (no API)* — free, uses the signed-in accounts — or *API (uses credits)* —
fast, no browser, the file in the client's Medsos format (sentiment, emotion, user ID…). With the API:
**Output** *Links only* (cheapest: no account look-ups) or *Links + content & engagement* (+ optional follower counts and
comments, extra credits); **Max credits for this run** (the run stops there; 0 = up to today's limit set under API
settings); **Max posts per keyword per platform**. Exclude words, X language and the Instagram choices apply too. X needs
the Twitter API key. Results: Keyword Links, the Excel in Exports and the reporting database; *Schedule it* works the
same way.

## Bulk links (read the articles of your own links)

News Search → **Bulk links**: paste article links (one per line, thousands are fine) → **Process links**. Every link is
opened and read like a search result — no search, no date or keyword filter. Google News links are turned into the
media's own link first. Links the server read before are reused at once (tick *Read again even if already collected*
for fresh text). Results arrive live on the News Links page and in an Online Excel file (Exports), and go to the
reporting database, so News Stream and Reports show them too. API: `POST /api/news/bulk {"links": "...", "force": false}`.

## News Stream (reading view) and the client API

**News Stream** (sidebar, News) reads the articles the Live Stream collected like a news app: filters on the left
(report topic, start / end date, sentiment, newest / oldest, grid or list, a keyword query with `AND` `OR` `NOT`
`"phrase"` `( )` `prefix*`), positive / negative / neutral shares on top, article cards in the middle, the full article on
the right with the keywords highlighted. New matching articles are announced every 30 seconds ("↑ 5 new articles").

**Source** chooses which databases it reads: *All databases* (the default — the Live Stream plus every News Search /
Keyword / scheduled run and imported Online file; a link found by both shows once), *Live stream only*, or *News Search &
reports only*. Articles from News Search carry a "News Search" tag. Clear both dates to search every day on the server.
The client API takes the same choice: `&source=all|stream|reports`.

### Client API — a key per client, only their news (recommended)

Sidebar → **Client API** (admins): a client's name, what they may see (a query — `AND` `OR` `NOT` `"phrase"` `( )`,
or a plain keyword list — or a report topic), which databases, optionally *data from* a date, an expiry date, requests
per minute and the websites allowed to call from the browser. **Create key** shows the key once (`wtc_…`; only its hash
is kept). The client needs no account and no sign-in:

```bash
curl -H "X-Client-Key: wtc_…" "https://app.workingteamzzz.id/api/client/articles?start=2026-11-20&size=50"
```

- `GET /api/client/articles` — their articles: `q` narrows further (never widens), `start` / `end`, `sentiment`, `sort`,
  `page`, `size` (≤ 100); with the sentiment shares. `start` / `end` also take a time (`2026-11-20 06:00`, WIB);
  `last_hours=6` = published in the last 6 hours; `received_since=<server_time of the previous answer>` = everything
  that reached the server since then, late arrivals included; `sort=received` = newest arrival first. Typical client
  schedule: hourly `received_since`, every 6 h `last_hours=6`, daily `last_hours=48`; store by `key` (no doubles).
  `GET /api/client/articles/one?key=…` — one of theirs in full.
  `GET /api/client/me` — what the key gives.
- The key reaches nothing else: no files, no dashboard, no other client's articles or keywords. Wrong key → 401 (and
  too many wrong keys block the address for a while), too many calls → 429.
- **Social media too**: tick *Social media posts & comments* — `GET /api/client/posts` gives the client's posts and
  comments from Keyword Search, Collect and the API scraper (same filters, plus `platform`). A key can carry news,
  social media or both.
- **Guide for the client's IT team**: `https://<server>/client-docs` (public, holds no data): authentication,
  parameters, the hourly / 6-hour / 2-day recipe, examples in curl, Python and JavaScript, error codes.
- Browser dashboards work without changing `.env` (CORS for these paths, no cookies); "Only from these websites"
  limits it. **New key** replaces a leaked one at once; **Pause**, an expiry date or **Delete** end the access.

### Giving a client's dashboard the data (API)

1. Users → add a **viewer** user for the client (e.g. `klien-pln`), then **New API key** — give the key to the client
   privately. Viewers can read, never change anything; delete the user to cut the access.
2. The client's system calls the server with the key in the `X-API-Key` header:

```bash
curl -H "X-API-Key: <the client's key>" "https://app.workingteamzzz.id/api/articles?q=pln%20AND%20(padam%20OR%20gangguan)&start=2026-11-20&end=2026-11-20&sentiment=negative&page=1&size=50"
```

   - `GET /api/articles` — `q`, `topic` (a Reports topic id or name), `start`, `end` (YYYY-MM-DD), `sentiment`
     (`positive` / `negative` / `neutral`), `sort` (`newest` / `oldest`), `page`, `size` (≤ 100). Returns the total,
     the sentiment shares and the articles (title, url, media, date, time, image, summary, sentiment, keywords).
   - `GET /api/articles/one?key=…` — one article in full (paragraphs, author, spokespersons, people).
   - Also: `/api/search`, `/api/stories` (top stories), `/api/dashboard` (trend, top media), `/api/report` (Excel).
     The full, clickable list is at `https://<server>/docs` after signing in.
3. A dashboard that calls the API **from the browser** (JavaScript on another website) also needs its address in
   `.env`: `CLIENT_ORIGINS=https://dashboard.klien.id` (comma separated), then restart. Server-to-server calls (their
   backend, n8n, Power BI, Looker Studio connectors) need nothing more.
4. Pushing instead of pulling: a schedule can send each run's results to the client's webhook (Schedules → results
   sent to …).

### Realtime push per article (client feeds)

Live Stream → **Client feeds — realtime** (admins): a client's name, what they get — **every article** the stream
reads (all data), a **query** (`AND` `OR` `NOT` `"phrase"` `( )`) or a plain **keyword list** (comma / one per line =
any of them) — their **webhook** (https://…) and an optional shared **token**. Every article the
stream reads that matches is pushed within seconds:

```json
POST <their webhook>
{"event": "articles", "feed": {"id": "…", "name": "PLN UID Jabar"}, "sent_at": "…", "count": 2,
 "articles": [{"key": "…", "url": "…", "media": "detik.com", "title": "…", "date": "2026-11-20", "time": "08:15",
               "image": "…", "summary": "…", "content": "<p>…</p>", "author": "…", "influencer": "…", "person": "…",
               "sentiment": "negative", "read_at": "…"}]}
```

With a token: `Authorization: Bearer <token>` and `X-Collector-Signature: sha256=<HMAC-SHA256 of the body>`. Any 2xx
answer counts as delivered; otherwise the articles wait in an outbox and are sent again (30 s, 2 min, 10 min, 30 min,
then hourly for a day). *Test* sends one sample now; the table shows sent / waiting / last error per client.

## Free up space by period (back up, delete, restore)

Everything is kept on the server until an admin deletes it. Exports → **Free up space by period**:

1. Choose a period (e.g. 1 Oct – 1 Nov 2026) → **Check the period**: how many stream articles (≈ size), report rows
   and result files it holds.
2. **Make the Excel**: every article of the period in one Online file — save it to Google Drive.
3. **Delete the period** (type DELETE to confirm): the stream's articles (and their search entries), the reporting
   database's rows and the result files made in the period (the backup just made is kept). Deleted articles are
   remembered so the stream never reads them again. The database reuses the freed space for new articles.
4. Changed your mind? **Restore a period from its backup**: upload the Excel (sent in 8 MB pieces, so a big month is
   fine) — its articles come back to News Stream, Explore and Reports (a Medsos file goes back to Reports).

## Media archive (every article of one outlet over months or years)

Media Database → **Media archive** (or *Archive* on an outlet's row): an outlet, a period (e.g. two years) and a speed.
The worker walks every sitemap of that outlet (dated archives outside the period are skipped), queues every article
link of the period and the stream's readers read them behind fresh news, one page at a time for that outlet (polite
≈ 50,000 articles a day, faster ≈ 100,000). Archive articles are **kept for good** (the stream's 7-day clean-up skips
them), are searchable in News Stream and Explore, and *Make Excel* writes them all as one Online file. The table shows
sitemaps read, links found, read / waiting / not readable; *Cancel* stops a job (what was read stays), *Delete*
removes its articles. Needs the Live Stream running. How far back it reaches depends on the outlet: most list every
article in their sitemaps; one without sitemaps for older years cannot be read back this way.

## Explore (search everything, top stories, spike alerts)

- **Search** every article the live stream read (title + summary, SQLite full-text index, kept as long as the stream
  keeps articles): words, `"exact phrase"`, `OR`, `NOT` / `-word`, `( )`, `prefix*`. Results highlight the words,
  show which outlets carry most, can be narrowed to one outlet, and go to one Online Excel with **Excel**.
- **Top stories** — articles of the last 3–24 h whose titles share most of their meaningful words are one story,
  ranked by how many outlets carry it; *Search this story* opens all its articles.
- **Spike alerts** — every 15 minutes, for each stream keyword and report topic: the last hour against the usual hour
  of the day before. ≥ 3× and ≥ 5 articles (adjustable) → one Telegram message, at most once per 3 h per keyword.

Measured with 1,000,000 articles in the stream database: dashboard refresh 0.04 s, search 0.2 s, top stories 0.2 s,
tiers 0.06 s, media statistics 2.7 s (cached 5 minutes).

## Media Database (registered media, activity, access, paid media)

**Media Database** (sidebar, News) lists the registered media (~12,400 outlets; the bundled dictionary until an admin
edits it — then `media_dictionary.txt` in the data folder). For each outlet, from the stream's last 7 days:

- **Activity** — Active (top 1,000) / Semi-active / Rare / Inactive / Unreachable / Not checked yet, and new articles
  per day.
- **Access** — Readable (≥ 80 % of its articles read) / Partly readable / Cannot be read (blocked, Cloudflare, HTTP
  403 …, with the most common error) / Paywall? (pages too short to be the article) / Refused by robots.txt / Site
  unreachable / No new articles / Not read yet / Excluded.

Admins add outlets (one per line, `domain` or `domain, name`) and remove them (the stream stops reading them at once).
**Weekly access report**: every Monday 07:00 WIB (adjustable) an Excel (Summary, Cannot be read, Readable, All media) is
written to Exports and a summary goes to Telegram; *Access report now* makes one any time.

**Paid media**: *Login* on an outlet's row takes the cookies of a browser logged in to it (the Cookie header, a cookie
export JSON or cookies.txt). They are kept on the server (file mode 600), never shown again, and every article of that
outlet is read with them — by the stream and by News Search. Check the outlet's subscription terms first.

## Google News links (news.google.com / rss) → the media's own link

Every news search turns Google News links into the media's own links. Whatever the browser could not decode gets a
**second pass** at the end of the search, from the server itself (the same method as the team's decoder notebook,
with pauses and retries when Google says "too many requests"). For older files: **Exports → Fix Google News links in a
file** — choose an `.xlsx`, `.csv` or `.txt`; every `news.google.com/…/articles/…` link is replaced, other cells stay
as they are, and a `Decode Log` sheet lists each link. The result appears in Exports as `…_decoded.xlsx`.

## API Scraper (the team's Social Scraper, via the scraping API)

A separate section of the dashboard (**API scraper → API Scraper**, operators and admins) runs the team's Colab
*Social Scraper* (v58) on the server: TikTok, YouTube, Threads, Instagram, Facebook and Twitter through
ScrapeCreators (and the optional Twitter API), with the same options as the Colab form — keyword / BY LINK /
BY PROFILE / MIXED, time filter, credit cap, language and negative words, comments, followers, retweets, import.
The output is the team's file, unchanged: `Data <Platform>_<date time>.xlsx` (Sheet1 in the Sample_Crawling layout
with User ID, Username, Follower, User Image, **Sentiment and Emotion**, plus *Rekap* and *Codebook*) and a CSV, in Exports.

- **API settings** (admins only; hidden for others) holds the API keys. They are stored on the server only
  (`api_scraper.json`, mode 600) and shown back as the last 4 characters. *Check credit balance* costs nothing.
- Sentiment/Emotion use the team's lexicon analysis (the AI models need a GPU and are not loaded on the server).
  The same analysis also fills **Sentiment/Emotion** in the normal Medsos layout and **SENTIMENT** in the Online layout.
- Stop ends the run like the credit cap does: what was collected is still saved.
- The post's own ID column is named **Post ID** (as in the Medsos upload template); the rest is the team's layout.
- **Saving credits**:
  - the page opens on the **Hemat** preset: 20 per keyword, cap 100, last 24 hours, no comments, and a
    profile call only when an ID/photo is missing. **Lengkap** is 50 per keyword, max coverage, followers and comments.
  - account profiles already looked up are reused (user ID/photo 30 days, followers 7 days), so the same
    accounts are not paid for again in the next runs (`api_profiles.json` in the data folder).
  - admins set a **credit limit per day and per month** under API settings; a run gets at most what is left,
    and new runs are refused once it is used up. Credits used today / this month show on the page.
  - filters (time, strict, language, negative words, duplicates) run *before* the profile calls, so dropped rows cost nothing extra.
- Updating to a new version of the team's script: `python tools/port_scraper.py social_scraper_advanced_vNN_en.py`
  regenerates `crawler/apiscraper/engine.py` (the tool stops if a place it changes is different in the new version).
- API: `GET /api/apiscraper/info`, `POST /api/apiscraper/start`, `GET /api/apiscraper/balance` (operator);
  `GET/POST /api/apiscraper/settings` (admin).

## Safe mode (on by default) — keeping Google and the accounts from being blocked

- **Budget per site**: pages the app may open per hour / per day (search pages, post pages and background reads all
  count). Three levels — *Careful* (Google Search 40/150, Instagram 60/200, X 120/400, TikTok 100/300, Facebook 40/150,
  Threads 100/300, YouTube 300/1000), *Normal* (2×, the default) and *Loose* (4×) — and any site's numbers can be set
  by hand (Settings → Safe mode). When a budget is used, that site waits; what the browser could not read is filled
  with the API (if set up).
- **Calmer pace**: pauses between pages are longer (Careful 1.7×, Normal 1.4×, Loose 1.15×).
- **Blocks are recognised and rested**: Instagram/Threads "Try again later" / "We restrict certain activity",
  Facebook "You're temporarily blocked" / checkpoint, X rate limit / locked, TikTok "too many attempts", Google's 403 —
  the site is then left alone (Google 6 h, platforms 24 h) instead of knocking again, and the team gets a Telegram
  message. *Settings → Safe mode* shows each site's use and lets an admin release a resting site early.
- **Schedules** that use the accounts or Google's search pages run at most every 6 hours (older, more frequent ones
  are spaced out). News with the Multi-source engine (no accounts, no search pages) may run hourly.
- Admins can switch it off in Settings (not recommended).

## Automation: the flow from search to the team's file

```
Schedule (or Start) ─┬─ Medsos (keyword search / profiles, browser, free)
                     │     1. links + caption & engagement + IDs read from the post pages
                     │     2. Post ID taken from each link (never needs the API)
                     │     3. API auto-fill, only for fields still empty:
                     │          free: known profiles / other rows of the same account
                     │          1 credit per post missing caption/engagement (also brings the account's ID & photo)
                     │          1 credit per ACCOUNT still missing ID/photo/followers
                     │          stops at the per-run fill cap and the daily/monthly limit
                     │     4. comments of the most engaged posts (API, optional, ~1 credit per post)
                     │     5. Sentiment & Emotion (team lexicon) → Medsos Excel
                     └─ News (Multi-source engine, no browser)
                           1. Google News by date + per outlet, sitemaps, RSS, Bing, GDELT → decode → read articles
                           2. only if the engine fails → the browser reads the Google News feed (never Google's search pages)
                           3. INFLUENCER / PERSON / SENTIMENT, the team's cleaning rules → Online Excel
   → Exports + reporting database (topics → reports) + delivery webhook + Telegram message ("✅ … finished — N links", button to the dashboard)
   → a verification on the way → Telegram message with "Solve it" / "Skip"
```

API settings: **Auto-fill empty Medsos data** (on by default when a key is set) and **Auto-fill credit cap per run**.

## Telegram bot

Set in `.env` on the server, then `sudo docker compose up -d`:

```
TELEGRAM_BOT_TOKEN=…        # from @BotFather
TELEGRAM_CHAT_ID=…          # send /start to the bot: it answers with your chat id (groups: starts with -)
PUBLIC_URL=https://…        # the dashboard address (buttons open it)
CAPTCHA_WAIT_MINUTES=60     # how long a run waits for someone to solve a verification
```

The bot messages the team when a verification is waiting (**Solve it** opens the dashboard, **Skip** skips it) and when
a scheduled run finishes or fails. Commands: `/status`, `/schedules` (each with **Run now**), `/stop`. It answers only
the chats in `TELEGRAM_CHAT_ID`; files and results are never sent to Telegram, only counts and the dashboard link,
which needs a sign-in. A verification is solved on the server's own browser: nobody's Google account is used.

## Reports (topics + database)

Every News / Keyword / Profile / API-scraper run is also saved in `archive.db` (data folder), already in the team's
Online / Medsos columns, with the keyword that found each row; a link found again is merged, not doubled.

On **Reporting → Reports**:
- **Topics** group keywords: e.g. *PLN* = pln, pemadaman listrik, listrik padam, tarif listrik (operators/admins).
- **Make a report**: topic (or typed keywords) + Online news / Social media + period → the numbers on the page
  (sentiment, per day, top media/platforms, per keyword, people / accounts) and an Excel
  `Report_<topic>_<Online|Medsos>_<from>_<to>.xlsx`: Sheet1 in the team's template + a Summary sheet.
  *Also rows that only mention a keyword* adds saved rows whose text contains a topic keyword.
- **Database → Add the Excel files already in Exports**: puts files made before this version into the database.

API: `GET/POST /api/report/topics`, `POST /api/report/topics/delete`, `POST /api/report`, `POST /api/report/import`.

## Schedules (searches that run by themselves)

The **API Scraper** can be scheduled too (*Schedule it* on its page): any interval from every hour (safe mode does not
limit it — no account is used), a **credit budget per run** is required, the daily/monthly credit limits still apply,
keyword runs collect the schedule's period (e.g. "today" every hour), and *only new* makes the engine skip posts it
already delivered. Files land in Exports and in the reporting database (Home, Reports, Explore).

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
| Data kept | Result files are kept until an admin deletes them (Exports → select → Delete); `RETENTION_DAYS` > 0 deletes older ones automatically. Page copies (they can show account names) are deleted after `DEBUG_RETENTION_DAYS` (7). |
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

### Users and rights

People sign in with **username + password**. Each has a role:

| Role | May |
| --- | --- |
| **Viewer** | see results, schedules and account lists; download the Excel files (e.g. for a client or a manager) |
| **Operator** | + run searches, schedules, change account lists, fix Google News links in files, solve verifications |
| **Admin** | + the **Users** page (add people, change roles, new passwords, disable, delete), import platform cookies, the access log |

The installer makes one account per name you give it — the first is admin, the others operators — with random
passwords, printed at the end and saved in `/root/post-link-collector-keys.txt`. Everyone can change their own
password (their name in the sidebar), and make a **personal API key** for scripts/n8n with their own rights.
Passwords are stored only as salted scrypt hashes; five wrong passwords lock that name for 15 minutes; changing a
role or password, or disabling someone, signs them out at once. The emergency access key in `.env`
(`COLLECTOR_API_KEYS`) acts as admin — keep it safe. Lost every admin password? On the server:
`docker compose exec collector python serve.py add-user <name> admin` prints a new one.

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
- **Telegram to your phone (free):** create a bot with **@BotFather** (it gives a token), send `/start` to
  **@userinfobot** to see your chat id, then set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (several ids comma
  separated) in `.env` and run `sudo docker compose up -d`. You get a message when a verification is waiting and
  when a scheduled search finishes. Raise `CAPTCHA_WAIT_MINUTES` (e.g. 60) if you don't always reply quickly.
- To be told by another service, set `CAPTCHA_WEBHOOK_URL` (a Slack/Teams incoming webhook, or an n8n/Make webhook that
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
| `crawler/api.py`, `serve.py` | The FastAPI server (sign-in, roles, audit log, `/docs`, cookie import) |
| `crawler/users.py` | User accounts: passwords (scrypt), roles viewer/operator/admin, personal API keys |
| `crawler/retention.py` | Deletes old result files and page copies on a server |
| `crawler/captcha.py` | Lets a person solve a verification from the dashboard when the browser runs on a server |
| `crawler/schedule.py` | Searches that run by themselves (Schedules page) |
| `crawler/delivery.py` | Sends finished results to another server as JSON (token, signature, retries) |
| `crawler/templates.py` | The team's Excel layouts (Online, Medsos) |
| `crawler/gnews.py` | Server-side Google News link decoder (second pass, file tool) |
| `crawler/cookies.py` | Reads exported cookies (Cookie-Editor JSON, storage state, cookies.txt) |
| `Dockerfile`, `docker-compose.yml`, `Caddyfile`, `.env.example`, `requirements-server.txt` | Running it on a server (HTTPS, locked-down container) |
| `install-server.sh` | One-command setup on a fresh Ubuntu VPS |
| `data/lists/` | Account lists; `Ditjen SDA.csv` is the bundled example |
| `tests/` | Unit, end-to-end and stress tests with fake platform pages (`python -m pytest tests`, about 2 minutes) |
