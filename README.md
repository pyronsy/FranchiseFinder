# FranchiseFinder

[![CI](https://github.com/pyronsy/FranchiseFinder/actions/workflows/ci.yml/badge.svg)](https://github.com/pyronsy/FranchiseFinder/actions/workflows/ci.yml)
[![Publish Docker image](https://github.com/pyronsy/FranchiseFinder/actions/workflows/publish.yml/badge.svg)](https://github.com/pyronsy/FranchiseFinder/actions/workflows/publish.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Scans TMDB for titles matching one or more franchises you define, compares
each against its own [MDBList](https://mdblist.com) list, and queues new
titles for your approval instead of adding them automatically. Approve or
reject from a small web UI, grouped by franchise. Approved items are pushed
to the matching MDBList list, which tools like
[Kometa](https://kometa.wiki) or list-aware Plex collection managers can
then sync into your library.

Optionally, an LLM gap-finding pass can run alongside the TMDB filter to
catch franchise titles the filter structurally can't (see below).

Repo: https://github.com/pyronsy/FranchiseFinder

## Quick start

No git clone or local build — pull the published image and run it:

```bash
mkdir franchisefinder && cd franchisefinder
curl -O https://raw.githubusercontent.com/pyronsy/FranchiseFinder/main/docker-compose.yml

docker compose pull
docker compose up -d
```

Docker creates `./config` and `./data` next to the compose file
automatically on first run — nothing else to set up beforehand. Images are
published automatically from this repo via GitHub Actions on every push to
`main`, tagged `ghcr.io/pyronsy/franchisefinder:latest`.

Once it's running, open `http://<your-host>:8420`. You'll see a banner
prompting you to add your TMDB and MDBList keys on the **Settings**
page — that's the only required step before your first scan. After that,
use **Manage Franchises** to add your first franchise from the browser —
no config files to write by hand.

(If you'd rather start a franchise from a template instead of the form,
the example config is in the repo at
[`config/franchises.example.json`](config/franchises.example.json) — copy
its contents into `config/franchises.json` yourself and edit that.)

## Updating

```bash
docker compose pull
docker compose up -d
```

`config/` and `data/` are untouched by an update — keys, franchises, and
franchises, and pending/approved state all persist across image versions.

## Configuring everything from the web UI

Nothing needs to be edited on disk — the **Settings** page covers:

- **TMDB API key** and **MDBList API key** *(required)*
- **Scan interval** (hours between automatic scans)
- **Notification webhook URL** *(optional)*
- **LLM provider** for the optional gap-finding pass *(optional)*

Each section has its own **Save & test connection** button that makes a
real request through the key you just entered and shows a pass/fail banner
before you rely on it. Keys are stored in `config/settings.json` on your
own server (git-ignored, never sent anywhere except the provider you
configured) and shown masked when you revisit the page.

## Rejected items

Rejecting a title on the Approvals page keeps it out of future scans
permanently — it won't get re-suggested even after the franchise's next
several scans. The **Rejected** page lists everything you've turned down,
grouped by franchise, and lets you:

- **Recheck** one item — sends it back to the Approvals queue for a fresh
  decision (this doesn't re-run the LLM or the TMDB filter, it just
  un-rejects it so it shows up for you to judge again).
- **Recheck all** for a franchise — useful right after you've changed that
  franchise's `tmdb_filter` or `llm_hint`, when old rejections may no
  longer reflect your current criteria and are worth a second look under
  the new settings.
- **Delete** — permanently forgets an item instead of rechecking it, if
  you're sure you never want to see it again and just want it off this
  list.

## Investigating an item before you decide

Every item on the Approvals and Rejected pages links out to its **TMDB**
page, and its **IMDb** page too when available — useful for checking cast,
plot, or release details before approving or rejecting something you're
not sure about. The IMDb ID is looked up once when an item is first
found (not during the bulk TMDB discover scan, to keep API usage
proportional to what's actually new), so items added before this feature
existed will show a TMDB link only until they're rechecked or a franchise
rescans them.

## Franchise Catalog

A browsable seed list of ~250 known multimedia franchises, sourced from
Wikipedia's
[Lists of multimedia franchises](https://en.wikipedia.org/wiki/Lists_of_multimedia_franchises),
grouped by category (literary works, comics, animated TV, live-action TV,
animated films, live-action films, video games with/without film-TV
tie-ins) with a search box to filter.

**What this does and doesn't do:** the catalog reliably gives you
franchise **names and Wikipedia links** — that part is pulled from
Wikipedia's raw wikitext via their API (not scraped HTML), which is
stable since Wikipedia's `[[link]]` syntax is consistent even though
rendered table layouts across different articles aren't. What it
deliberately does **not** do is auto-import each franchise's actual
movies and shows: most entries link to a franchise's general overview
article rather than a dedicated filmography page, and the pages that do
have one use inconsistent table structures — there's no reliable generic
way to scrape "the media" for all ~250 of them, and this app isn't going
to guess.

Instead, **Quick Add** creates a stub franchise using **LLM-only
matching** (`tmdb_filter.type: "none"`) with an `llm_hint` anchored to the
franchise's name and Wikipedia link, then sends you to the edit form to
pick its MDBList list — the one piece quick-add can't know for you. Once
that's set, use [Plex Import](#plex-import)'s Check feature or a normal
scan to actually populate its media, the same way any other LLM-only
franchise works; that mechanism already handles "what belongs to this
named franchise" reliably, which is a better fit for this problem than
scraping ever would be.

Quick Add requires an LLM provider configured on the Settings page first
— refuses to create a franchise that would never find anything otherwise.
Franchises you've already added show an **already added** badge and a
disabled button so you don't accidentally create duplicates.

The catalog is cached locally (`data/wikipedia_franchise_catalog.json`)
after the first load — **Refresh from Wikipedia** forces a re-fetch if
Wikipedia's list has changed since.

## Plex Import

Normal scans only look for *new* TMDB releases. **Plex Import** does the
opposite: it works from titles you **already own**. It's a two-step
process — import a snapshot of a Plex library once, then check that
stored snapshot against any number of franchises, independently and as
many times as you want, without re-fetching from Plex each time.

### Setup

Add your Plex server URL and token on the **Settings** page (optional —
only needed for this feature):

- **Server URL**: e.g. `http://192.168.1.10:32400` (must be reachable from
  wherever the container runs)
- **Token**: see
  [Plex's guide to finding your token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)

**Save & test connection** hits Plex's `/identity` endpoint to confirm
both are correct before you rely on them.

### Step 1 — Import

On the **Plex Import** page, pick a library (movie or TV — populated live
from your server) and import it. This stores a snapshot in
`data/plex_library.json` — not tied to any one franchise. Imports up to
500 items per run; re-running (e.g. on a bigger library, or after adding
new titles in Plex) only adds what's new, it never duplicates or resets
anything already stored.

Plex items without a recognizable TMDB ID in their metadata (rare, mostly
very old legacy-agent libraries) get one title/year search against TMDB
as a fallback during import; if that doesn't resolve either, they're
skipped and the import result tells you how many.

### Step 2 — Check against a franchise

Each franchise on the Plex Import page shows how many stored items have
been checked against it so far, how many matched, and how many are new
since the last check. Click **Check** to process just the new ones —
already-checked items for that franchise are always skipped, which is
what makes checking against several franchises (or re-checking after a
scan interval) cheap rather than reprocessing the whole library every
time. What "checking" means depends on that franchise's `tmdb_filter`:

- **company / keyword / network** — each item's TMDB ID is checked
  directly against TMDB's own data (production companies, keywords, or
  networks) for a match. No LLM involved, same criteria the normal scan
  uses, just checked in the opposite direction.
- **none (LLM only)** — new items are sent to your configured LLM in
  batches (25 at a time, not one call per title) to judge which ones
  genuinely belong. The LLM classifies every title in the batch, in
  order — matches are identified by position, not by matching the
  LLM's returned text back to Plex's title string, since models often
  paraphrase titles slightly when echoing them back.

Every checked item — match or not — gets a permanent notation recorded
against that franchise, which is exactly what keeps future checks fast.
If you change a franchise's `tmdb_filter` or `llm_hint`, its past
classifications may no longer reflect the new criteria — use **Reset
checks** on that franchise's row to clear its notation and re-evaluate
everything on the next Check run (this doesn't touch any other
franchise's history, or anything already in Approvals/Rejected).

Matches that aren't already in the target MDBList list, already pending,
or already rejected land in the same **Approvals** queue as a normal
scan, tagged **in your Plex library**.

### LLM rate limits during a large check run

A big library on an LLM-only franchise can mean a dozen-plus batched LLM
calls in one Check run (25 titles per call) — and since checked items are
skipped on future runs, this cost is front-loaded onto the *first* check
of a large library, not repeated every time. On a free-tier provider,
that first run is enough to trip a per-minute rate limit partway through.
Every LLM call in the app — including these — automatically retries a
429 up to twice with backoff (respecting a `Retry-After` header if the
provider sends one, otherwise 5s then 10s, capped at 30s), and there's
also a small proactive 1.5s gap between batches specifically to make
hitting the limit less likely in the first place. If a batch still fails
after retrying, it's skipped (not the whole run, and not marked as
checked — it'll be retried on the next Check click) and the result
message tells you exactly which provider/limit hit and suggests trying
again shortly.

### If a check finds zero matches

The result message breaks down the funnel — how many items were newly
checked (vs. already checked and skipped) and how many matched. That
breakdown usually tells you exactly what's going on without needing to
guess:

- **New-to-check is 0** — nothing to investigate; everything stored has
  already been checked against this franchise. Use **Reset checks** if
  you want a fresh pass under updated criteria.
- **Checked is nonzero but matches is 0** — for LLM-only franchises, check
  the app logs: a 0-match batch that parsed cleanly gets logged at info
  level with the raw LLM response, so you can see whether the model
  genuinely found nothing or is being unexpectedly conservative (often
  fixable by tightening or loosening the franchise's `llm_hint`).
- **Import result showed a low import count** — check the import
  message's unresolved count; items without a resolvable TMDB ID never
  make it into the stored snapshot at all.

## Getting API keys

- **TMDB**: https://www.themoviedb.org/settings/api (free, no card)
- **MDBList**: https://mdblist.com/preferences/#api_key_uid (free, no card)
- **LLM gap-finding** *(optional)* — see below

Create one MDBList list per franchise you want to track, and note each
list's ID (visible in the list's URL/edit page) for `config/franchises.json`.

## Configuring a franchise

Each entry in `config/franchises.json` looks like:

```json
{
  "id": "mcu",
  "name": "Marvel Cinematic Universe",
  "mdblist_list_id": "your_mcu_list_id",
  "tmdb_filter": { "type": "company", "id": 420 },
  "exclude_title_keywords": ["assembled", "legends"],
  "exclude_tmdb_ids": [],
  "llm_hint": "Include Marvel Studios films and Disney+ series. Exclude Fox X-Men films, Sony's non-MCU Spider-Man films, and making-of specials."
}
```

- **id** — short, unique, used internally for file storage (lowercase, no spaces).
- **name** — display name shown in the UI.
- **mdblist_list_id** — the MDBList list this franchise's approved items
  get added to. This must be the list's **numeric ID**, not the
  username/slug shown in its browser URL (e.g. `mdblist.com/lists/pyronsy/mcu-xyz123/`
  has slug `mcu-xyz123`, which is *not* the same as the numeric ID the API
  needs). The **Manage Franchises** page has a dropdown that fetches your
  actual lists from MDBList and fills in the correct numeric ID for
  you — use that instead of typing this by hand.
- **tmdb_filter** — how TMDB is queried. `type` is one of:
  - `"company"` — matches a production company/studio ID (e.g. Marvel
    Studios = 420). Best when a franchise is made almost entirely by one
    studio.
  - `"keyword"` — matches a TMDB keyword ID (e.g. "star wars" or "wizarding
    world"). Better for multi-studio franchises. Find keyword IDs by
    searching TMDB's website and checking a title's Keywords tab, or via
    `/search/keyword` in the API.
  - `"network"` — matches a TV network ID (TV only, ignored for movies).
    Useful for franchises anchored to one streaming service.
  - `"none"` — **skips the TMDB filter entirely.** No `discover` call
    happens at all; every suggestion comes purely from the LLM's
    gap-finding pass instead. Use this when a franchise is too scattered
    across studios/keywords for any single TMDB filter to catch reliably
    (an anthology series, a loosely-connected shared universe, a
    franchise TMDB just doesn't tag consistently). **Requires an LLM
    provider configured on the Settings page** — the app refuses to save
    a franchise as `"none"` without one, since it would otherwise never
    find anything. Works best paired with a specific `llm_hint`, since
    the LLM is now doing 100% of the discovery work rather than just
    annotating/supplementing a filter's results.
- **exclude_title_keywords** — titles containing any of these (case-insensitive)
  are skipped. Documentary-genre titles are always skipped automatically.
- **exclude_tmdb_ids** — permanently ignore specific titles, format
  `"movie:1234"` or `"tv:5678"`.
- **llm_hint** *(optional)* — extra guidance for the LLM gap-finding pass
  about what does/doesn't count for this franchise. Only used if an LLM
  provider is configured on the Settings page.

Add as many franchise entries as you want — the scan loop and UI handle
any number of them automatically.

## LLM-assisted gap-finding (optional)

Configured entirely from the **Settings** page in the web UI — no env vars
needed. Pick a provider, paste in your key (or a base URL for a local
model), and hit **Save & test connection** to confirm it works before
relying on it.

Supported providers:

| Provider | Cost | Notes |
|---|---|---|
| Anthropic (Claude) | Paid, no free tier | Best judgment quality |
| Google Gemini | **Free tier, no card required** | Good free option, generous daily quota |
| Groq | **Free tier, no card required** | Fast, free, open-weight models only |
| Ollama | **Free, self-hosted** | Runs a local model, no API key, no data leaves your machine — see the commented-out `ollama` service in `docker-compose.yml` |

### Getting a free API key

No credit card needed for either of these — an email/account is enough to
get a working key in a couple of minutes.

**Google Gemini** (recommended — best free reasoning quality):
1. Go to [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
2. Sign in with a Google account
3. Click **Create API key**
4. Copy the key, paste it into this app's Settings page with provider set
   to **Google Gemini**, and hit **Save & test connection**

**Groq** (fastest, good if you hit Gemini's daily cap):
1. Go to [console.groq.com/keys](https://console.groq.com/keys)
2. Sign in (email, Google, or GitHub)
3. Click **Create API Key**, copy it
4. Paste it into Settings with provider set to **Groq**

**Ollama** (no key at all, fully local):
No account or key needed — see the "Running the LLM step fully local"
section below instead.

Free tiers are rate-limited and change over time. If a scan hits a cap
mid-run, the app shows a yellow warning banner on the Approvals page (e.g.
"Groq rate limit or quota reached... gap-finding was skipped this cycle")
rather than failing silently — the TMDB filter results for that scan are
unaffected either way, only the LLM's reasoning/gap-finding step skips
until the next cycle.

### Running the LLM step fully local (no external API)

1. Uncomment the `ollama` service block in `docker-compose.yml`
2. `docker compose up -d ollama`
3. Pull a model: `docker exec -it ollama ollama pull llama3.1`
4. On the Settings page, set provider to **Ollama**, model to `llama3.1`,
   and base URL to `http://ollama:11434` (the default)
5. Save & test connection

Once configured, every scan cycle adds one combined LLM call per
franchise that does two things at once:

1. **Annotates the TMDB filter's own results** — every title the
   company/keyword filter just found gets a one-sentence reasoning from
   the LLM explaining why it belongs (or flagging if it looks like a bad
   match). These show up in the approval queue as **filter match + LLM
   reasoning**.
2. **Finds genuine gaps** — anything legitimately part of the franchise
   but missing from both the current list and this cycle's filter
   results, including recent/upcoming releases. Each suggestion is
   resolved against TMDB by title/year to get a real TMDB ID; anything
   that doesn't resolve to a confident match is skipped and logged rather
   than added blind. These show up tagged **LLM suggested**.

Every item in the approval queue always has *some* reasoning shown, so
you're never approving or rejecting blind. If the LLM is disabled, its
call fails, or it just doesn't annotate a particular title, that item
falls back to a plain, deterministic explanation of which TMDB filter
matched it instead (shown in a plainer grey rather than the italic blue
used for actual LLM reasoning, so you can tell the two apart at a glance).

For a franchise with `tmdb_filter.type` set to `"none"`, step 1 above is
always empty (there's nothing for the filter to have found), so every
item that shows up comes from step 2 tagged **LLM suggested** — this is
the intended way to bypass an overly narrow TMDB filter entirely for a
specific franchise, as opposed to disabling the LLM step globally.

This runs *in addition to* the TMDB filter, not instead of it. Leaving the
provider set to **Disabled** (the default) skips the LLM step entirely —
you still get the deterministic fallback reasoning on every item either
way.

Your key is stored in `config/settings.json` on your own server (mounted
volume, git-ignored) — never committed, never sent anywhere except the
provider you chose.

**Cost note:** this makes exactly one LLM call per franchise per scan
cycle (not per candidate, and not two calls for the annotate+gap-find
steps — they're combined into a single request), so cost stays low even
on a free tier with a short `CHECK_INTERVAL_HOURS`.

## If MDBList calls fail

**Wrong list ID is the most common cause.** MDBList's API requires the
list's **numeric ID**, not the username/slug from its browser URL. Use the
dropdown on the **Manage Franchises** page (fetches your real lists
directly from MDBList) rather than typing this by hand — see the
`mdblist_list_id` note above. A malformed ID here can surface as either an
HTTP error *or*, in some cases, as what looks like a connection failure
("no endpoint variant even got a response") if the resulting URL doesn't
route the way the app expects — so if you see that error, check this
first before assuming it's a network problem.

**Reading** a list (`get_current_mdblist_tmdb_ids()`) uses MDBList's
confirmed `GET /lists/{id}/items` endpoint and response shape — this part
is stable and shouldn't need adjustment.

**Adding** an approved item (`add_item_to_mdblist()`) is the one part of
this integration MDBList's public docs don't fully confirm — their Apiary
reference lists a single "Modify Static List Items" endpoint but the exact
path/method aren't reliably documented. Rather than hardcode one guess,
the app tries a few plausible variants in order (`POST`/`PUT` against
`/lists/{id}/items`, with and without a trailing `/add`), remembers
whichever one actually works for your account, and only re-probes if that
stops working later. If **all** variants fail, the item stays safely in
your approval queue (nothing is lost) and a red banner explains it —
check the app logs at that point for exactly which response each attempt
got back, and cross-check against MDBList's current docs at
https://api.mdblist.com/docs/ or https://mdblist.docs.apiary.io/.

## If the LLM connection test fails with a 404

LLM providers rename and retire model IDs fairly often — Google in
particular has moved through several Gemini generations, each with its own
shutdown schedule for the last one. A 404 on **Save & test connection**
almost always means the Model field (or the built-in default, if you left
it blank) points at an ID that's since been renamed or retired, not a
problem with your key.

The error message includes a link to the provider's current model list —
check it, update the Model field to a current ID, or clear the field
entirely to fall back to the app's default (also worth checking isn't
stale — model IDs can outlive an app release). Provider docs:

- Gemini: https://ai.google.dev/gemini-api/docs/models
- Groq: https://console.groq.com/docs/models
- Anthropic: https://docs.claude.com/en/docs/about-claude/models

## Notifications (optional)

Set a webhook URL (Discord webhook, ntfy topic URL, etc) in the **Settings**
page to get pinged whenever any franchise has new items awaiting approval.

## Project layout

This section describes the repo source, for reference or if you're
browsing/contributing — running FranchiseFinder doesn't require any of
this locally, see Quick Start above.

```
app.py                          # scan loop + Flask web UI
templates/index.html            # approval queue page
templates/rejected.html         # rejected items — recheck or delete permanently
templates/plex.html             # import + check a Plex library against franchises
templates/catalog.html          # browsable Wikipedia franchise catalog + quick add
templates/franchises.html       # add/edit/delete franchises
templates/settings.html         # TMDB/MDBList keys + LLM provider settings
config/franchises.example.json  # reference template for franchises.json (or just use the UI)
Dockerfile                      # builds the image published to GHCR
docker-compose.yml              # the file Quick Start has you curl and run
.github/workflows/publish.yml   # builds & pushes the image to ghcr.io on every push to main
.github/workflows/ci.yml        # syntax/build checks on push and PR
```

At runtime (not part of the repo), the container also uses:

```
config/settings.json            # all API keys/settings, written by the Settings page
config/franchises.json          # your actual franchises, written by the Manage Franchises page
data/                           # per-franchise pending/rejected approval state
data/plex_library.json          # stored Plex snapshot + per-franchise checked notations
data/wikipedia_franchise_catalog.json  # cached Wikipedia franchise catalog
```

## License

MIT — see [LICENSE](LICENSE).
