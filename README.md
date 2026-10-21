# CABA Rental Monitor

Watches Buenos Aires (CABA) rental listings and sends you a Telegram message the
moment a new one matches your criteria — Recoleta and Barrio Norte, Departamento
/ Casa / PH, at least 50 m², up to ARS 1.000.000 per month, **expensas excluded
from the price limit**.

Every five minutes it asks each source once, normalizes what it finds into a
single internal format, filters, and notifies only listings it has never seen
before. The SQLite database makes that memory survive restarts.

```
[20:20] MercadoLibre: 89 listings found, 55 matching, 55 new
[20:22] ArgenProp: 28 listings found, 2 matching, 1 new
[20:22] Notifications: 3 sent
```

---

## 1. What it does

- Polls **MercadoLibre Inmuebles**, **ArgenProp** and **CabaProp** on a fixed interval
  (ZonaProp is included but disabled — see [Sources](#3-sources-and-how-they-are-accessed)).
- Normalizes every advertisement into one `Listing` model, whatever the source.
- Filters on operation, property type, neighborhood, area, price and currency.
- Remembers every listing it has seen, so nothing is announced twice — not after
  a restart, not after a redeploy.
- Sends a Telegram message and/or an email per new listing, or one grouped
  digest when a single cycle turns up many.
- Lets anyone subscribe: `/start` in the bot subscribes a chat, `/stop`
  unsubscribes; `/status` and `/stats` show the monitor's health.
- Gives every subscriber their own search: `/settings` changes neighborhoods,
  property types, maximum rent and minimum area with buttons.

### What it deliberately does *not* do

- It does not add expensas to the price when comparing against `MAX_RENT`.
- It does not notify a listing whose price or area it could not read reliably
  ("desde" prices, ads quoting both ARS and USD, cards with no m² shown).
- It does not decide a neighborhood by searching for text in an address. Only a
  site's structured location field counts (see [Neighborhoods](#8-changing-the-search-criteria)).
- It does not bypass CAPTCHAs, Cloudflare or any other access control.

---

## 2. Architecture

```
monitor/
├── config.py            environment/file configuration, search criteria
├── neighborhoods.py     canonical neighborhood map (aliases, per-source slugs and ids)
├── models/              Listing (the normalized model) and ScrapeResult
├── scrapers/
│   ├── base.py          HttpClient (timeouts, retries, pacing, block detection)
│   ├── argenprop/       scraper + parser
│   ├── mercadolibre/    scraper + parser + optional official-API adapter
│   └── zonaprop/        disabled strategy (Cloudflare), kept isolated
├── services/
│   ├── filtering/       criteria → accept/reject with a reason
│   ├── deduplication/   dedupe key, "is this new?", restart safety
│   └── notification/    message formatting + Telegram delivery
├── database/            SQLite schema and repository
├── scheduler/           the monitoring cycle and the loop
└── telegram/            /start, /settings, /stop, /status, /stats command bot
```

The dependency direction is one-way: scrapers know nothing about SQLite or
Telegram, the formatter knows nothing about the Telegram API, and the scheduler
is the only place that wires them together. Adding a fourth source means adding
one package under `scrapers/`; adding email or Slack notifications means adding
one `Notifier` implementation.

**Per-source isolation.** A scraper never raises into the scheduler: failures
come back as a `ScrapeResult` with status `blocked`, `error` or `disabled`, are
recorded in `source_state`, and the source is backed off (15 min × consecutive
failures, capped at 2 h) while the other sources keep working. A blocked source
is reported as *blocked*, never as "0 listings found" — those mean different
things.

---

## 3. Sources and how they are accessed

| Source | Method | Requests per cycle | Status |
|---|---|---|---|
| MercadoLibre | Server-rendered search pages, filtered by URL | ~6 (2 neighborhoods × 3 types) | Working, verified live |
| ArgenProp | Server-rendered search pages, newest-first | 3, on its own 10-minute interval | Working, verified live |
| CabaProp | The site's own JSON search endpoint, newest-first | 1 | Working, verified live |
| ZonaProp | — | 0 | Disabled: Cloudflare challenge |

**MercadoLibre.** Its URL grammar carries every filter we need, which keeps the
request count tiny and the parsing honest:

```
/departamentos/alquiler/capital-federal/recoleta/_PriceRange_0ARS-1000000ARS_TOTAL*AREA_50-*
```

Barrio Norte is its own location slug there, so the neighborhood comes from the
site's structured field. A second page is fetched only when the first came back
completely full. An adapter for MercadoLibre's **official API** is included
(`MERCADOLIBRE_MODE=api`), but every endpoint now requires OAuth credentials,
so it is unverified — the HTML path is the one that was tested end to end.

**ArgenProp.** Also server-rendered, and each card repeats its key values as
attributes (`idaviso`, `idbarrio`, `idmoneda`, `montooperacion`), which the
parser cross-checks against the printed text. Both neighborhoods fit in one URL
(`/departamentos/alquiler/barrio-norte-o-recoleta?orden-masnuevos`; robots.txt
forbids three-way `-o-` combinations, two are fine). Its AWS WAF answers `202`
with an empty body when it dislikes your rate, and that block outlives short
backoffs — hence the 20-second delay between requests and the separate
`ARGENPROP_MIN_INTERVAL_SECONDS` interval.

**CabaProp.** The portal of CABA's real-estate brokers' board (CUCICBA), so it
only lists licensed agencies in the city. Its search page is a client-side app
that POSTs the filters to `/api/v1/properties/find-properties` and renders the
JSON; the scraper sends that same request — rent, property types, barrio ids,
price ceiling and minimum total area, newest first — so one request per cycle
covers everything new. Every field is structured: price and expensas with their
currency (`1` = USD, `2` = ARS), total and covered area, barrio ids. There is
no anti-bot challenge and robots.txt only disallows account pages. The endpoint
is undocumented, so a change in its response shape is reported as an error,
never as "0 listings found".

**ZonaProp.** Every URL, including the sitemaps listed in its own robots.txt,
returns `403` with `cf-mitigated: challenge` and Cloudflare's "Just a moment…"
page. There is no public API and no RSS feed. Getting past it would mean
executing the anti-bot challenge, so the source ships disabled: it reports
itself once per cycle and makes no requests. Three ways to change that:

1. `ZONAPROP_STRATEGY=provider` plus a commercial scraping provider that deals
   with the challenge on its side — you supply `ZONAPROP_PROVIDER_URL` and
   `ZONAPROP_API_KEY` and implement `monitor/scrapers/zonaprop/provider.py`;
2. create a saved-search alert on ZonaProp and ingest those emails;
3. `ZONAPROP_STRATEGY=http` to re-check whether the challenge is still there —
   expect it to report `blocked`.

**A note on terms of service.** ArgenProp's robots.txt permits the paths used
here. MercadoLibre's robots.txt blocks AI-crawler user agents rather than
ordinary clients, but scraping is still contrary to its terms — its official API
with your own credentials is the clean route. Keep the polling gentle: one cycle
every five minutes, not dozens of requests a second.

---

## 4. Notification channels

Telegram is the main channel: every chat that sent `/start` to the bot gets
the new listings matching its own search (see [Changing the search
criteria](#8-changing-the-search-criteria)). Email is optional and off in `.env.example`
(`EMAIL_ENABLED=false`). Telegram and email are independent: configure either,
or both. Every configured
channel receives every notification, and a listing counts as delivered as soon
as one channel accepts it — a misconfigured second channel never causes endless
re-sending. With no channel configured the service still runs and writes the
notifications to its log.

### Email (SMTP)

```bash
EMAIL_TO=you@gmail.com
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASSWORD=<16-character app password>
```

**Gmail needs an App Password**, not your account password:

1. Enable 2-step verification at
   [myaccount.google.com/security](https://myaccount.google.com/security).
2. Open [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords),
   create one named e.g. `alquiler`, and copy the 16 characters.
3. Put it in `SMTP_PASSWORD` (spaces are fine to remove).

`EMAIL_FROM` defaults to `SMTP_USER` because Gmail refuses to send as anyone
else. `EMAIL_TO` accepts several comma-separated addresses. Port `465` switches
to implicit TLS automatically; any other port uses STARTTLS.

Each message is sent as HTML with a plain-text alternative, so it stays readable
in any client.

## 5. Creating a Telegram bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot`, pick a display name, then a username ending in `bot`.
3. BotFather replies with a token like `123456789:AAF...`. That is
   `TELEGRAM_BOT_TOKEN`. Keep it secret — anyone holding it controls the bot.

That token is all the bot needs. Start the service, open your bot in Telegram
and send `/start`: you are subscribed. Share the bot's username with anyone else
who should get the listings — they send `/start` too. `/stop` unsubscribes, and
a chat that blocks the bot is unsubscribed automatically.

### Finding your chat ID (optional)

`TELEGRAM_CHAT_ID` is only a convenience: that chat is subscribed automatically
on the first start, so you get notifications without sending `/start`. If it
later sends `/stop`, a restart does not resubscribe it.

Send your new bot any message first (a bot cannot start a conversation), then:

```bash
curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates" | python3 -m json.tool
```

Look for `"chat": {"id": 123456789, ...}` — that number is `TELEGRAM_CHAT_ID`.
For a group, add the bot to the group, post a message there, and use the group's
negative id (e.g. `-1001234567890`).

The bot answers everyone. `/status` and `/stats` show only aggregate counts,
never who is subscribed. Subscribers are stored in the `subscribers` table of
the SQLite database.

---

## 6. Environment variables

Copy `.env.example` to `.env` and fill it in. Everything except the Telegram
bot token has a working default. Quote values that contain spaces
(`NEIGHBORHOODS="Recoleta,Barrio Norte"`) so the file can be sourced by a shell.

| Variable | Default | Meaning |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Bot token from BotFather. |
| `TELEGRAM_CHAT_ID` | — | Optional. Chat subscribed automatically on the first start. |
| `EMAIL_TO` | — | Comma-separated recipients. Enables the email channel. |
| `SMTP_HOST` / `SMTP_PORT` | `smtp.gmail.com` / `587` | SMTP server. Port `465` implies implicit TLS. |
| `SMTP_USER` / `SMTP_PASSWORD` | — | SMTP login. For Gmail, an App Password. |
| `EMAIL_FROM` / `EMAIL_FROM_NAME` | `SMTP_USER` / `CABA Rental Monitor` | Sender identity. |
| `EMAIL_SUBJECT_PREFIX` | `[Alquiler] ` | Prefix on every subject, handy for inbox filters. |
| `EMAIL_ENABLED` | `true` | Set to `false` to keep the settings but mute the channel. |
| `MAX_RENT` | `1000000` | Default maximum **rent**. Expensas are never added to it. |
| `MIN_AREA` | `50` | Default minimum area in m². |
| `CURRENCY` | `ARS` | Listings in any other currency are rejected. |
| `NEIGHBORHOODS` | `Recoleta,Barrio Norte` | Default neighborhoods. Must exist in the neighborhood map. |
| `PROPERTY_TYPES` | `Departamento,Casa,PH` | Default types. Any subset of the three. |
| `CHECK_INTERVAL` | `5` | Minutes between cycles. |
| `SOURCES` | `mercadolibre,argenprop,cabaprop` | Which sources to poll. |
| `NOTIFY_ON_FIRST_RUN` | `false` | On each source's first successful run, record what exists without notifying. |
| `DATABASE_PATH` | `data/listings.sqlite3` | SQLite file. `/data/listings.sqlite3` in Docker. |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `text` | `LOG_FORMAT=json` emits one JSON object per line. |
| `TZ` | `America/Argentina/Buenos_Aires` | Timezone used in messages and logs. |
| `TELEGRAM_GROUP_THRESHOLD` | `5` | More new listings than this in one cycle are grouped into a digest. |
| `MAX_NOTIFICATIONS_PER_CYCLE` | `20` | Upper bound on messages per cycle. |
| `ARGENPROP_MIN_INTERVAL_SECONDS` | `600` | ArgenProp is polled at most this often. |
| `ARGENPROP_REQUEST_DELAY` / `MERCADOLIBRE_REQUEST_DELAY` | `20` / `2` | Seconds between requests to that host. |
| `ARGENPROP_MAX_PAGES` / `MERCADOLIBRE_MAX_PAGES` | `1` / `3` | Pagination limit per search. |
| `CABAPROP_PAGE_SIZE` / `CABAPROP_MAX_PAGES` | `30` / `1` | Newest listings fetched per CabaProp request, and how many pages. |
| `CABAPROP_REQUEST_DELAY` / `CABAPROP_MIN_INTERVAL_SECONDS` | `5` / `0` | Pacing for CabaProp. |
| `MERCADOLIBRE_MODE` | `html` | `api` uses the official API and needs `MELI_ACCESS_TOKEN` (unverified). |
| `ZONAPROP_STRATEGY` | `disabled` | `http` or `provider` — see above. |
| `CONFIG_FILE` | — | Optional JSON file with the same keys. Environment variables win. |
| `NEIGHBORHOOD_MAP_FILE` | — | Optional JSON replacing the built-in neighborhood map. |

Secrets are read from the environment only; nothing is hardcoded.

---

## 7. Running

### Locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # then fill in TELEGRAM_BOT_TOKEN

# one cycle, then exit - the quickest way to see whether the scrapers work
set -a && . ./.env && set +a
.venv/bin/python -m monitor --once

# run continuously
.venv/bin/python -m monitor
```

Useful flags: `--once` (single cycle) and `--no-telegram-commands` (skip the
command poller). Stop with `Ctrl-C`; shutdown is graceful.

### With Docker

```bash
cp .env.example .env          # fill in TELEGRAM_BOT_TOKEN
docker compose up -d
docker compose logs -f
```

The SQLite file lives on the named volume `listings-data`, so
`docker compose down && docker compose up -d` keeps every listing already seen
and sends nothing twice. `docker compose down -v` deletes the volume — the next
start then treats every current listing as already-seen (because
`NOTIFY_ON_FIRST_RUN=false`) rather than flooding you.

### Tests and linting

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
```

The parser tests run against HTML captured from the live sites
(`tests/fixtures/`), so they never touch the network.

---

## 8. Changing the search criteria

Each subscriber sets their own search in the bot with `/settings`: tap
neighborhoods and property types on or off, and pick a maximum rent and a
minimum area from the buttons or type a number. The search is stored per chat in
the `subscriber_criteria` table, and takes effect from the next cycle.

The sites are asked once per cycle for the union of all active subscribers'
searches (every neighborhood and type anyone chose, the highest rent limit, the
smallest area); each subscriber's own filter then picks their listings out of
that. `deliveries` records which listing went to which chat.

When a subscriber widens the search (a new neighborhood, a higher rent limit),
the sites start returning listings that have been there all along. On that
cycle those are recorded without notifying, just like a source's first run;
listings the previous search would also have covered are still announced.
Each subscriber gets listings that appear after they set their search, not the
backlog.

The configuration below is the **default search**: what a new subscriber starts
with and what applies to chats that never opened `/settings`. Edit `.env` (or
the `CONFIG_FILE` JSON) and restart:

```bash
MAX_RENT=1500000
MIN_AREA=45
PROPERTY_TYPES=Departamento,PH
NEIGHBORHOODS=Recoleta
```

**Adding a neighborhood** takes one more step, because each site names and
codes neighborhoods differently. Copy the built-in map from
`monitor/neighborhoods.py` into a JSON file, add your entry, and point
`NEIGHBORHOOD_MAP_FILE` at it:

```json
{
  "Palermo": {
    "aliases": ["palermo"],
    "slugs": {"mercadolibre": ["palermo"], "argenprop": ["palermo"]},
    "ids": {"argenprop": ["12617"]}
  }
}
```

- `slugs` are what appears in each site's search URL.
- `ids` are the site's internal location ids. ArgenProp prints the sub-barrio
  "Parque Las Heras" on cards it files under `idbarrio=4` (Barrio Norte), so the
  id — not the printed label — is what gets such listings right. CabaProp
  splits Barrio Norte into four ids (10, 11, 12, 13), all mapped to it.
- `aliases` only ever match a site's structured location label, never an address.

A neighborhood that is not in the map is rejected at startup with an
explanation, rather than silently matching nothing.

---

## 9. Troubleshooting

**Nothing arrives at all.** The first log line after startup lists the active
notification channels; if it warns that none is configured, the credentials are
missing and messages are only being logged. For Telegram, send `/status` to the
bot — silence means a wrong token or the service is not running. For email, a
wrong Gmail password shows up as `email delivery failed ... 535`, which means you
used the account password instead of an App Password. Remember that each
source stays quiet on its first successful cycle by design, including a source
you enable later on an existing database.

**A source says `blocked`.** ArgenProp's WAF returns `202` with an empty body
when polled too hard; the source backs itself off automatically. If it persists,
raise `ARGENPROP_MIN_INTERVAL_SECONDS` and `ARGENPROP_REQUEST_DELAY`. ZonaProp
reports `disabled` by design.

**A source says `error`.** Sites change their markup. Run one cycle with
`LOG_LEVEL=DEBUG` to see the URL, then check whether the page still contains the
expected structure:

```bash
LOG_LEVEL=DEBUG .venv/bin/python -m monitor --once
curl -s -A "Mozilla/5.0" "<the url from the log>" | grep -c 'poly-component__title'   # MercadoLibre
curl -s -A "Mozilla/5.0" "<the url from the log>" | grep -c 'data-item-card'          # ArgenProp
```

A count of `0` means the markup moved: update the selectors in that source's
`parser.py` and refresh the fixture in `tests/fixtures/`.

**Found many, matched none.** That is usually correct, and the log says why —
each cycle reports its rejection reasons:

```
rejected="{'area_below_min': 30, 'currency_not_allowed': 4}"
```

`currency_not_allowed` means USD listings, `area_missing` means the card showed
no m² (common on ArgenProp), `price_unreliable` means a "desde" price or an ad
quoting both currencies. Note that MercadoLibre filters server-side on *total*
area while its cards usually print *covered* area, so a listing with 45 m²
covered and 55 m² total is rejected here; lower `MIN_AREA` slightly if you want
those.

**Inspecting the database.**

```bash
sqlite3 data/listings.sqlite3 \
  "SELECT source, neighborhood, price, area_m2, reject_reason FROM listings ORDER BY first_seen_at DESC LIMIT 20;"
sqlite3 data/listings.sqlite3 "SELECT * FROM source_state;"
```

**Re-sending everything on purpose.** Delete the database file (or the Docker
volume) and set `NOTIFY_ON_FIRST_RUN=true` for one run.

---

## 10. Known limitations

- ZonaProp is unavailable without a third-party provider or an anti-bot bypass.
- The MercadoLibre official-API adapter is written but unverified: it needs
  OAuth credentials this project does not have.
- ArgenProp cards sometimes omit m² entirely; those listings are stored but not
  notified, because an unknown area cannot be compared against `MIN_AREA`.
- Listings are read from search result pages only. Detail pages are never
  fetched, which keeps the request rate low but means a field absent from the
  card is absent here too.
