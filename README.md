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
- **mdblist_list_id** — the MDBList list this franchise's approved items get added to.
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

MDBList's exact field/endpoint shapes have shifted over time across
versions. Two functions in `app.py` are the ones to check first if items
aren't syncing correctly:

- `get_current_mdblist_tmdb_ids()` — reads an existing list
- `add_item_to_mdblist()` — pushes an approved item

Both log the raw response on failure. Cross-check against MDBList's current
docs at https://api.mdblist.com/docs/ if you hit errors.

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
```

## License

MIT — see [LICENSE](LICENSE).
