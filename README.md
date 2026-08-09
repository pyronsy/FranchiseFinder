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

**Option A — clone and build locally:**

```bash
git clone https://github.com/pyronsy/FranchiseFinder.git
cd FranchiseFinder

cp .env.example .env
docker compose up -d --build
```

**Option B — pull the prebuilt image, from a full clone:**

Images are published automatically from this repo via GitHub Actions on
every push to `main`, tagged `ghcr.io/pyronsy/franchisefinder:latest`. If
you've already cloned the repo, either edit `docker-compose.yml` (comment
out `build: .`, uncomment the `image:` line), or just point at the
dedicated compose file instead:

```bash
git clone https://github.com/pyronsy/FranchiseFinder.git
cd FranchiseFinder

docker compose -f docker-compose.prebuilt.yml pull
docker compose -f docker-compose.prebuilt.yml up -d
```

**Option C — pull the prebuilt image, no clone at all:**

`docker-compose.prebuilt.yml` doesn't reference any other file in the
repo, so you can grab just that one file and skip cloning entirely:

```bash
mkdir franchisefinder && cd franchisefinder
curl -O https://raw.githubusercontent.com/pyronsy/FranchiseFinder/main/docker-compose.prebuilt.yml

docker compose -f docker-compose.prebuilt.yml pull
docker compose -f docker-compose.prebuilt.yml up -d
```

Docker creates `./config` and `./data` next to the compose file
automatically on first run — nothing else to set up beforehand.

---

`.env` can be left entirely blank in any of these — every credential and
setting is configured from the browser after the container starts (see
below). It only exists as an optional way to pre-fill defaults if you're
scripting a deployment; Options B and C skip it entirely since nothing is
required upfront.

Whichever option you use, once it's running open `http://<your-host>:8420`.
You'll see a banner prompting you to add your TMDB and MDBList keys on the
**Settings** page — that's the only required step before your first scan.
After that, use **Manage Franchises** to add your first franchise — no JSON
editing required anywhere. (If you'd rather template a franchise by hand
instead of the form, `cp config/franchises.example.json
config/franchises.json` before starting the container and edit that
instead — only available with Option A or B, since Option C doesn't have
the repo's example file locally.)

## Updating

**Local build (Option A):**
```bash
git pull
docker compose up -d --build
```

**Prebuilt image (Option B or C):**
```bash
docker compose -f docker-compose.prebuilt.yml pull
docker compose -f docker-compose.prebuilt.yml up -d
```
Your `config/` and `data/` are untouched by an update either way — keys,
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

Free tiers are rate-limited and change over time — if you hit a cap,
Settings will surface the error next time the app tries a call, and the
gap-finding pass just skips that cycle rather than breaking anything else.

### Running the LLM step fully local (no external API)

1. Uncomment the `ollama` service block in `docker-compose.yml`
2. `docker compose up -d ollama`
3. Pull a model: `docker exec -it ollama ollama pull llama3.1`
4. On the Settings page, set provider to **Ollama**, model to `llama3.1`,
   and base URL to `http://ollama:11434` (the default)
5. Save & test connection

Once configured, every scan cycle adds a second pass per franchise:

1. The app sends the LLM the franchise name, your `llm_hint`, and the
   titles currently in the list (plus anything already pending/rejected).
2. It asks the LLM what's missing — genuinely part of the franchise but
   not yet in the list.
3. Each suggestion is looked up on TMDB by title/year to get a real TMDB
   ID. Suggestions that don't resolve to a confident TMDB match are
   skipped and logged.
4. Anything that resolves gets added to the same approval queue as
   filter-matched items, tagged **LLM suggested** with the model's short
   reasoning shown next to it, so you can judge for yourself before
   approving.

This runs *in addition to* the TMDB filter, not instead of it. Leaving the
provider set to **Disabled** (the default) skips this step entirely.

Your key is stored in `config/settings.json` on your own server (mounted
volume, git-ignored) — never committed, never sent anywhere except the
provider you chose.

**Cost note:** this makes one LLM call per franchise per scan cycle (not
per candidate), so cost stays low even on a free tier with a short
`CHECK_INTERVAL_HOURS`.

## If MDBList calls fail

MDBList's exact field/endpoint shapes have shifted over time across
versions. Two functions in `app.py` are the ones to check first if items
aren't syncing correctly:

- `get_current_mdblist_tmdb_ids()` — reads an existing list
- `add_item_to_mdblist()` — pushes an approved item

Both log the raw response on failure. Cross-check against MDBList's current
docs at https://api.mdblist.com/docs/ if you hit errors.

## Notifications (optional)

Set a webhook URL (Discord webhook, ntfy topic URL, etc) in the **Settings**
page to get pinged whenever any franchise has new items awaiting approval.

## Project layout

```
app.py                          # scan loop + Flask web UI
templates/index.html            # approval queue page
templates/franchises.html       # add/edit/delete franchises
templates/settings.html         # TMDB/MDBList keys + LLM provider settings
config/franchises.example.json  # copy to franchises.json, or manage via the UI
config/settings.json            # all API keys/settings, written by the Settings page (git-ignored)
data/                           # per-franchise pending/rejected state (git-ignored)
Dockerfile
docker-compose.yml              # local build (default) or prebuilt image, edit to switch
docker-compose.prebuilt.yml     # standalone — pulls the prebuilt image, no clone needed
.env.example                    # optional — everything can be left blank and set via the web UI instead
```

## License

MIT — see [LICENSE](LICENSE).
