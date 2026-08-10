"""
Franchise List Curator
-----------------------
Reads a list of franchises from a config file (each pointing at a TMDB
filter — studio/company, keyword, or network — and its own MDBList list),
scans TMDB for matching titles, and queues anything new for approval
instead of adding it automatically. Approved items are pushed to the
franchise's MDBList list; rejected items are remembered per franchise.

Run as a single container: a background thread does the periodic scan
across all configured franchises, and Flask serves an approval UI on :8420.
"""

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

import requests
from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

CONFIG_FILE = Path(os.environ.get("CONFIG_FILE", "/config/franchises.json"))
SETTINGS_FILE = Path(os.environ.get("SETTINGS_FILE", "/config/settings.json"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))

TMDB_BASE = "https://api.themoviedb.org/3"
MDBLIST_BASE = "https://api.mdblist.com"

# All API keys and runtime options (TMDB, MDBList, notify URL, check
# interval, LLM provider) are configured via the Settings page in the web
# UI and stored in SETTINGS_FILE. The env vars below are only used as
# one-time pre-fill defaults the first time each field is loaded — handy if
# you're migrating from an older .env-based setup, but nothing here is
# required at startup anymore.

DOCUMENTARY_GENRE_ID = 99

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("franchisefinder")

app = Flask(__name__)
_lock = threading.Lock()
_config_lock = threading.Lock()

VALID_FILTER_TYPES = {"company", "keyword", "network", "none"}


def load_franchises() -> list:
    if not CONFIG_FILE.exists():
        return []
    franchises = json.loads(CONFIG_FILE.read_text())
    ids = [f["id"] for f in franchises]
    if len(ids) != len(set(ids)):
        raise SystemExit("Franchise ids in config must be unique.")
    return franchises


def save_franchises(franchises: list) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(franchises, indent=2))


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "franchise"


def unique_id(base_id: str, existing_ids: set) -> str:
    if base_id not in existing_ids:
        return base_id
    n = 2
    while f"{base_id}-{n}" in existing_ids:
        n += 1
    return f"{base_id}-{n}"


def get_franchise(franchise_id: str) -> dict:
    for f in load_franchises():
        if f["id"] == franchise_id:
            return f
    abort(404)


# --------------------------------------------------------------------------
# LLM settings (provider, API key, model) — managed via the Settings page
# --------------------------------------------------------------------------

LLM_PROVIDERS = {
    "none": {"label": "Disabled", "needs_key": False, "needs_base_url": False},
    "anthropic": {
        "label": "Anthropic (Claude)",
        "needs_key": True,
        "needs_base_url": False,
        "default_model": "claude-sonnet-5",
    },
    "gemini": {
        "label": "Google Gemini",
        "needs_key": True,
        "needs_base_url": False,
        "default_model": "gemini-flash-latest",
    },
    "groq": {
        "label": "Groq",
        "needs_key": True,
        "needs_base_url": False,
        "default_model": "llama-3.3-70b-versatile",
    },
    "ollama": {
        "label": "Ollama (self-hosted, local)",
        "needs_key": False,
        "needs_base_url": True,
        "default_model": "llama3.1",
        "default_base_url": "http://ollama:11434",
    },
}

DEFAULT_LLM_SETTINGS = {"provider": "none", "api_key": "", "model": "", "base_url": ""}

CORE_DEFAULTS = {
    "tmdb_api_key": "",
    "mdblist_api_key": "",
    "check_interval_hours": "24",
    "notify_url": "",
    "plex_url": "",
    "plex_token": "",
}


def _read_settings_file() -> dict:
    if not SETTINGS_FILE.exists():
        return {}
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except json.JSONDecodeError:
        log.warning("Could not parse %s, treating as empty", SETTINGS_FILE)
        return {}


def _write_settings_file(data: dict) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, indent=2))


def load_llm_settings() -> dict:
    data = _read_settings_file()
    return {**DEFAULT_LLM_SETTINGS, **data.get("llm", {})}


def save_llm_settings(settings: dict) -> None:
    data = _read_settings_file()
    data["llm"] = settings
    _write_settings_file(data)


def load_core_settings() -> dict:
    """
    TMDB/MDBList keys, notify URL, and check interval. Values saved via the
    Settings page take priority; if a field has never been set there, its
    corresponding env var (if any) is used as a fallback default — mainly
    so an existing .env-based setup keeps working after this change.
    """
    data = _read_settings_file()
    core = {**CORE_DEFAULTS, **data.get("core", {})}

    if not core["tmdb_api_key"]:
        core["tmdb_api_key"] = os.environ.get("TMDB_API_KEY", "")
    if not core["mdblist_api_key"]:
        core["mdblist_api_key"] = os.environ.get("MDBLIST_API_KEY", "")
    if core["check_interval_hours"] == CORE_DEFAULTS["check_interval_hours"] and os.environ.get(
        "CHECK_INTERVAL_HOURS"
    ):
        core["check_interval_hours"] = os.environ["CHECK_INTERVAL_HOURS"]
    if not core["notify_url"]:
        core["notify_url"] = os.environ.get("NOTIFY_URL", "")
    if not core["plex_url"]:
        core["plex_url"] = os.environ.get("PLEX_URL", "")
    if not core["plex_token"]:
        core["plex_token"] = os.environ.get("PLEX_TOKEN", "")

    return core


def save_core_settings(settings: dict) -> None:
    data = _read_settings_file()
    data["core"] = settings
    _write_settings_file(data)


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "•" * len(key)
    return key[:4] + "•" * (len(key) - 8) + key[-4:]


# --------------------------------------------------------------------------
# Per-franchise JSON-file state store
# --------------------------------------------------------------------------


def _state_path(franchise_id: str, name: str) -> Path:
    d = DATA_DIR / franchise_id
    d.mkdir(parents=True, exist_ok=True)
    return d / name


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        log.warning("Could not parse %s, starting fresh", path)
        return {}


def _save(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def load_pending(franchise_id: str) -> dict:
    return _load(_state_path(franchise_id, "pending.json"))


def save_pending(franchise_id: str, data: dict) -> None:
    _save(_state_path(franchise_id, "pending.json"), data)


def load_rejected(franchise_id: str) -> dict:
    return _load(_state_path(franchise_id, "rejected.json"))


def save_rejected(franchise_id: str, data: dict) -> None:
    _save(_state_path(franchise_id, "rejected.json"), data)


# --------------------------------------------------------------------------
# TMDB discovery — generic over company / keyword / network filters
# --------------------------------------------------------------------------


def _discover_param(tmdb_filter: dict, media_type: str):
    ftype = tmdb_filter["type"]
    fid = tmdb_filter.get("id")
    if ftype == "none":
        return None
    if ftype == "company":
        return ("with_companies", fid)
    if ftype == "keyword":
        return ("with_keywords", fid)
    if ftype == "network":
        # TMDB Discover only supports with_networks for TV
        if media_type != "tv":
            return None
        return ("with_networks", fid)
    raise ValueError(f"Unknown tmdb_filter type: {ftype}")


def _tmdb_discover(media_type: str, tmdb_filter: dict, tmdb_api_key: str) -> list:
    param = _discover_param(tmdb_filter, media_type)
    if param is None:
        return []
    param_name, param_value = param

    results = []
    page = 1
    total_pages = 1
    while page <= total_pages:
        resp = requests.get(
            f"{TMDB_BASE}/discover/{media_type}",
            params={
                "api_key": tmdb_api_key,
                param_name: param_value,
                "page": page,
                "sort_by": "primary_release_date.desc"
                if media_type == "movie"
                else "first_air_date.desc",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        total_pages = data.get("total_pages", 1)
        results.extend(data.get("results", []))
        page += 1
    return results


def _is_excluded(title: str, genre_ids: list, keywords: list) -> bool:
    lowered = (title or "").lower()
    if any(kw.lower() in lowered for kw in keywords):
        return True
    if DOCUMENTARY_GENRE_ID in (genre_ids or []):
        return True
    return False


def discover_candidates(franchise: dict, tmdb_api_key: str) -> list:
    """Returns normalized {tmdb_id, media_type, title, year} for one franchise."""
    tmdb_filter = franchise["tmdb_filter"]
    exclude_keywords = franchise.get("exclude_title_keywords", [])

    excluded_ids = set()
    for entry in franchise.get("exclude_tmdb_ids", []):
        if ":" not in entry:
            continue
        mtype, _, tid = entry.partition(":")
        excluded_ids.add((mtype.strip(), tid.strip()))

    candidates = []

    for movie in _tmdb_discover("movie", tmdb_filter, tmdb_api_key):
        tmdb_id = str(movie["id"])
        title = movie.get("title", "")
        if ("movie", tmdb_id) in excluded_ids:
            continue
        if _is_excluded(title, movie.get("genre_ids"), exclude_keywords):
            continue
        candidates.append(
            {
                "tmdb_id": tmdb_id,
                "media_type": "movie",
                "title": title,
                "year": (movie.get("release_date") or "")[:4],
            }
        )

    for show in _tmdb_discover("tv", tmdb_filter, tmdb_api_key):
        tmdb_id = str(show["id"])
        title = show.get("name", "")
        if ("tv", tmdb_id) in excluded_ids:
            continue
        if _is_excluded(title, show.get("genre_ids"), exclude_keywords):
            continue
        candidates.append(
            {
                "tmdb_id": tmdb_id,
                "media_type": "tv",
                "title": title,
                "year": (show.get("first_air_date") or "")[:4],
            }
        )

    return candidates


# --------------------------------------------------------------------------
# LLM gap-finding (optional)
# --------------------------------------------------------------------------


def _call_anthropic(prompt: str, settings: dict) -> str:
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": settings["api_key"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": settings["model"] or LLM_PROVIDERS["anthropic"]["default_model"],
            "max_tokens": 2048,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    return "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    )


def _call_gemini(prompt: str, settings: dict) -> str:
    model = settings["model"] or LLM_PROVIDERS["gemini"]["default_model"]
    resp = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        params={"key": settings["api_key"]},
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    candidates = data.get("candidates", [])
    if not candidates:
        return ""
    parts = candidates[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts)


def _call_groq(prompt: str, settings: dict) -> str:
    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {settings['api_key']}",
            "content-type": "application/json",
        },
        json={
            "model": settings["model"] or LLM_PROVIDERS["groq"]["default_model"],
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices", [])
    if not choices:
        return ""
    return choices[0].get("message", {}).get("content", "")


def _call_ollama(prompt: str, settings: dict) -> str:
    base_url = (settings.get("base_url") or LLM_PROVIDERS["ollama"]["default_base_url"]).rstrip("/")
    resp = requests.post(
        f"{base_url}/api/generate",
        json={
            "model": settings["model"] or LLM_PROVIDERS["ollama"]["default_model"],
            "prompt": prompt,
            "stream": False,
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json().get("response", "")


_PROVIDER_CALLERS = {
    "anthropic": _call_anthropic,
    "gemini": _call_gemini,
    "groq": _call_groq,
    "ollama": _call_ollama,
}


def call_llm(prompt: str, settings: dict) -> str:
    provider = settings.get("provider", "none")
    caller = _PROVIDER_CALLERS.get(provider)
    if not caller:
        raise ValueError(f"No LLM provider configured (current: {provider!r})")
    return caller(prompt, settings)


def _extract_json_object(text: str):
    """LLMs sometimes wrap JSON in prose or code fences — pull out the object."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {}
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        log.warning("Could not parse LLM JSON output")
        return {}


def llm_gap_and_annotate(franchise: dict, filter_matched: list, known_titles: list) -> dict:
    """
    Single combined LLM call per franchise per scan cycle that does two
    things at once (to keep cost/quota usage low):

    1. Annotates each title the TMDB filter just found with a one-sentence
       justification for why it belongs in the franchise.
    2. Suggests any other titles that are missing from the list entirely.

    Returns {"annotations_by_order": [reasoning_or_none, ...] aligned with
    filter_matched, "annotations_by_title": {title_lower: reasoning},
    "missing": [{title, year, media_type, reasoning}, ...]}. Raises
    requests.RequestException or ValueError on failure — callers should
    catch and use _friendly_llm_error() to report it.
    """
    settings = load_llm_settings()
    if settings.get("provider", "none") == "none":
        return {"annotations_by_order": [], "annotations_by_title": {}, "missing": []}

    hint = franchise.get("llm_hint", "")
    known_block = "\n".join(f"- {t}" for t in sorted(known_titles)) or "(list is currently empty)"

    if filter_matched:
        candidates_block = "\n".join(
            f"- {i['title']} ({i['year']})" for i in filter_matched
        )
    else:
        candidates_block = "(none this cycle)"

    prompt = f"""You are helping curate a Plex collection for the franchise "{franchise['name']}".

Already in the collection:
{known_block}

An automated studio/keyword filter just found these candidates to add this
cycle, and each needs a one-sentence justification for why it genuinely
belongs in "{franchise['name']}" (note briefly if you think one is actually
NOT a good match instead):
{candidates_block}

{hint}

Separately, list any OTHER movies or TV shows that are legitimately part of
this franchise but are missing from both the list above and the candidates
above, including recent or upcoming releases you are aware of. Be
conservative — only include titles you are confident genuinely belong,
not loosely related or unofficial content.

Respond with ONLY a JSON object (no prose, no markdown fences) of this
exact shape:
{{
  "annotations": [{{"title": "...", "reasoning": "one short sentence"}}],
  "missing": [{{"title": "...", "year": "YYYY", "media_type": "movie" or "tv", "reasoning": "one short sentence"}}]
}}

The "annotations" array must have exactly one entry per candidate listed
above, in the exact same order — this is required even if you leave the
candidates list empty (then "annotations" is also empty). If nothing is
missing, use an empty array for "missing".
"""

    raw = call_llm(prompt, settings)
    parsed = _extract_json_object(raw)

    raw_annotations = parsed.get("annotations", []) if isinstance(parsed, dict) else []

    annotations_by_order = [
        a_item.get("reasoning", "") if isinstance(a_item, dict) else ""
        for a_item in raw_annotations
    ]

    annotations_by_title = {}
    for a_item in raw_annotations:
        if isinstance(a_item, dict) and a_item.get("title"):
            annotations_by_title[a_item["title"].strip().lower()] = a_item.get("reasoning", "")

    missing = [
        s
        for s in (parsed.get("missing", []) if isinstance(parsed, dict) else [])
        if isinstance(s, dict) and s.get("title") and s.get("media_type") in ("movie", "tv")
    ]

    return {
        "annotations_by_order": annotations_by_order,
        "annotations_by_title": annotations_by_title,
        "missing": missing,
    }


def resolve_tmdb_id(title: str, year: str, media_type: str, tmdb_api_key: str):
    """Looks up a title on TMDB search and returns its TMDB id, or None."""
    params = {"api_key": tmdb_api_key, "query": title}
    if year:
        params["year" if media_type == "movie" else "first_air_date_year"] = year
    try:
        resp = requests.get(f"{TMDB_BASE}/search/{media_type}", params=params, timeout=20)
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except requests.RequestException as exc:
        log.warning("TMDB search failed for %r: %s", title, exc)
        return None
    if not results:
        return None
    return str(results[0]["id"])


def get_imdb_id(media_type: str, tmdb_id: str, tmdb_api_key: str) -> str:
    """
    Looks up the IMDb ID for a TMDB title, for linking out to IMDb from the
    approval queue. Called once per newly-found candidate (not for every
    result in a bulk discover scan) to keep TMDB API usage proportional to
    what's actually new, not to the full catalog being filtered.
    """
    try:
        resp = requests.get(
            f"{TMDB_BASE}/{media_type}/{tmdb_id}/external_ids",
            params={"api_key": tmdb_api_key},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("imdb_id") or ""
    except requests.RequestException as exc:
        log.debug("Could not fetch IMDb ID for %s:%s: %s", media_type, tmdb_id, exc)
        return ""


# --------------------------------------------------------------------------
# Plex (import existing library items for franchise matching)
# --------------------------------------------------------------------------

_TMDB_GUID_RE = re.compile(r"tmdb://(\d+)")
_TMDB_LEGACY_GUID_RE = re.compile(r"themoviedb://(\d+)")


def _plex_headers(plex_token: str) -> dict:
    return {"X-Plex-Token": plex_token, "Accept": "application/json"}


def test_plex_connection(plex_url: str, plex_token: str):
    """Hits Plex's /identity endpoint, which works with any valid token."""
    url = plex_url.rstrip("/") + "/identity"
    try:
        resp = requests.get(url, headers=_plex_headers(plex_token), timeout=15)
        if resp.ok:
            return True, "Plex server reachable."
        return False, f"Plex error {resp.status_code}: {resp.text[:150]}"
    except requests.RequestException as exc:
        return False, f"Could not reach Plex: {exc}"


def get_plex_libraries(plex_url: str, plex_token: str) -> list:
    """Returns [{key, title, type}] for movie and show libraries only."""
    url = plex_url.rstrip("/") + "/library/sections"
    resp = requests.get(url, headers=_plex_headers(plex_token), timeout=20)
    resp.raise_for_status()
    data = resp.json()
    directories = data.get("MediaContainer", {}).get("Directory", [])
    return [
        {"key": d.get("key"), "title": d.get("title", ""), "type": d.get("type", "")}
        for d in directories
        if d.get("type") in ("movie", "show")
    ]


def _extract_tmdb_id_from_plex_item(item: dict):
    """
    Modern Plex Media Server returns a `Guid` array like
    [{"id": "tmdb://12345"}, {"id": "imdb://tt123"}, ...] when the request
    includes includeGuids=1. Older/legacy-agent libraries instead embed a
    single `guid` string using the full agent identifier
    (e.g. "com.plexapp.agents.themoviedb://12345?lang=en" — note this is
    "themoviedb", not "tmdb", so it needs its own pattern). This checks
    both shapes.
    """
    for g in item.get("Guid", []) or []:
        match = _TMDB_GUID_RE.search(g.get("id", ""))
        if match:
            return match.group(1)
    legacy_guid = item.get("guid", "") or ""
    match = _TMDB_GUID_RE.search(legacy_guid) or _TMDB_LEGACY_GUID_RE.search(legacy_guid)
    if match:
        return match.group(1)
    return None


def get_plex_library_items(plex_url: str, plex_token: str, library_key: str, limit: int = 500) -> list:
    """
    Returns up to `limit` items from one Plex library as
    [{title, year, media_type, tmdb_id (may be None)}]. Caps at `limit` to
    keep a single import run's duration reasonable — very large libraries
    may need more than one run (each run skips titles already queued or
    already in the target MDBList list, so re-running is safe).
    """
    url = plex_url.rstrip("/") + f"/library/sections/{library_key}/all"
    resp = requests.get(
        url,
        headers=_plex_headers(plex_token),
        params={"includeGuids": 1, "X-Plex-Container-Start": 0, "X-Plex-Container-Size": limit},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    metadata = data.get("MediaContainer", {}).get("Metadata", [])

    items = []
    for m in metadata:
        plex_type = m.get("type")
        media_type = "movie" if plex_type == "movie" else "tv" if plex_type == "show" else None
        if not media_type:
            continue
        items.append(
            {
                "title": m.get("title", ""),
                "year": str(m.get("year", "")) if m.get("year") else "",
                "media_type": media_type,
                "tmdb_id": _extract_tmdb_id_from_plex_item(m),
            }
        )
    return items


def matches_tmdb_filter(media_type: str, tmdb_id: str, tmdb_filter: dict, tmdb_api_key: str):
    """
    Checks whether one TMDB title matches a franchise's company/keyword/
    network filter, by fetching its TMDB details and comparing IDs — the
    reverse direction of the normal Discover-based scan. Returns True/False,
    or None if tmdb_filter is type "none" (no criteria to check against;
    caller should use LLM classification instead).
    """
    ftype = tmdb_filter.get("type")
    fid = tmdb_filter.get("id")
    if ftype == "none":
        return None
    if ftype == "network" and media_type != "tv":
        return False

    params = {"api_key": tmdb_api_key}
    if ftype == "keyword":
        params["append_to_response"] = "keywords"
    try:
        resp = requests.get(f"{TMDB_BASE}/{media_type}/{tmdb_id}", params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        log.debug("TMDB detail lookup failed for %s:%s: %s", media_type, tmdb_id, exc)
        return False

    if ftype == "company":
        return any(c.get("id") == fid for c in (data.get("production_companies") or []))
    if ftype == "network":
        return any(n.get("id") == fid for n in (data.get("networks") or []))
    if ftype == "keyword":
        kw_block = data.get("keywords", {}) or {}
        keywords = kw_block.get("keywords") or kw_block.get("results") or []
        return any(k.get("id") == fid for k in keywords)
    return False


def _extract_json_array(text: str):
    """
    LLMs sometimes wrap JSON in prose or code fences — pull out the array.
    Also handles a model wrapping the array in an object (e.g.
    {"results": [...]}) despite being told not to, which some models do
    regardless of instructions — this is a real failure mode seen in
    practice, not just a theoretical one.
    """
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    # Fallback: the whole response might parse as a JSON object with the
    # array nested inside one of its values instead of at the top level.
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        obj = None
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for value in obj.values():
            if isinstance(value, list):
                return value

    log.warning("Could not parse LLM JSON output: %s", text[:300])
    return []


def llm_classify_plex_batch(franchise: dict, titles_batch: list) -> list:
    """
    For "none"-type (LLM-only) franchises, asks the LLM to classify every
    title in a batch from the Plex library, in order. Returns EXACTLY one
    {"belongs": bool, "reasoning": str} entry per input title, aligned by
    POSITION — not matched back by title text. LLMs commonly paraphrase a
    title slightly when echoing it (drop a subtitle, reformat the year,
    change punctuation), which silently broke matching in an earlier
    version of this function; positional alignment avoids that entirely,
    the same way the main gap-finding pass's filter annotations work.

    Batched (not one call per title) to keep this affordable against a
    large library.
    """
    settings = load_llm_settings()
    if settings.get("provider", "none") == "none" or not titles_batch:
        return []

    titles_block = "\n".join(f"- {t['title']} ({t['year']})" for t in titles_batch)
    hint = franchise.get("llm_hint", "")

    prompt = f"""You are checking which titles from a Plex media library belong to the
franchise "{franchise['name']}".

{hint}

Titles to check, in order:
{titles_block}

Respond with ONLY a JSON array (no prose, no markdown fences) containing
EXACTLY one entry per title above, in the exact same order — this is
required even for titles that don't belong. Each entry:
{{"belongs": true or false, "reasoning": "one short sentence"}}

Set "belongs" to true only for titles you are confident genuinely belong
to this franchise. The array length must match the number of titles
listed above exactly.
"""
    raw = call_llm(prompt, settings)
    parsed = _extract_json_array(raw)

    if len(parsed) != len(titles_batch):
        log.warning(
            "LLM Plex classification returned %d entries for a %d-title batch — "
            "results may be misaligned for this batch. Raw response: %s",
            len(parsed),
            len(titles_batch),
            raw[:300],
        )
    elif not any(
        isinstance(p, dict)
        and (p.get("belongs") is True or (isinstance(p.get("belongs"), str) and p.get("belongs").strip().lower() == "true"))
        for p in parsed
    ):
        # Parsed cleanly and the right length, but classified nothing as a
        # match — log the raw response so a genuinely-empty result (correct
        # for this batch) can be told apart from a systematic issue (wrong
        # llm_hint, model being overly conservative, etc) without needing
        # to reproduce the problem again.
        log.info(
            "LLM Plex classification found 0 matches in this %d-title batch for %r. Raw response: %s",
            len(titles_batch),
            franchise["name"],
            raw[:500],
        )

    return parsed


# --------------------------------------------------------------------------
# MDBList
# --------------------------------------------------------------------------


def get_my_mdblist_lists(mdblist_api_key: str) -> list:
    """
    Fetches the authenticated user's own MDBList lists via the confirmed
    `/lists/user` endpoint, returning each list's real numeric ID — this
    is the value MDBList's API actually requires, which is easy to
    confuse with the username/slug shown in a list's browser URL
    (e.g. mdblist.com/lists/username/list-slug/).
    """
    resp = requests.get(
        f"{MDBLIST_BASE}/lists/user",
        params={"apikey": mdblist_api_key},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        return []
    return [
        {
            "id": item.get("id"),
            "name": item.get("name", ""),
            "mediatype": item.get("mediatype", ""),
            "items": item.get("items", 0),
        }
        for item in data
        if item.get("id") is not None
    ]


def get_current_mdblist_tmdb_ids(list_id: str, mdblist_api_key: str):
    """
    NOTE: verify this endpoint/shape against https://api.mdblist.com/docs/
    if it comes back empty or errors — field names have shifted across
    MDBList API versions.
    """
    ids = set()
    offset = 0
    limit = 1000
    while True:
        resp = requests.get(
            f"{MDBLIST_BASE}/lists/{list_id}/items",
            params={"apikey": mdblist_api_key, "limit": limit, "offset": offset},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        if isinstance(data, dict):
            movies = data.get("movies", [])
            shows = data.get("shows", [])
        else:
            movies, shows = [], []

        for item in movies:
            tmdb_id = str(item.get("ids", {}).get("tmdb") or item.get("tmdb", ""))
            if tmdb_id:
                ids.add(("movie", tmdb_id))
        for item in shows:
            tmdb_id = str(item.get("ids", {}).get("tmdb") or item.get("tmdb", ""))
            if tmdb_id:
                ids.add(("tv", tmdb_id))

        got = len(movies) + len(shows)
        if got < limit:
            break
        offset += limit

    return ids


# Cache of (endpoint_template, method) that has already been confirmed to
# work, so we don't re-probe every single approval once we know the right
# one for this MDBList API version.
_mdblist_add_endpoint_cache = {"template": None, "method": None}


def _try_mdblist_add(template: str, method: str, list_id: str, mdblist_api_key: str, payload: dict):
    """Attempts one (path, method) combination. Returns (success, response) — response is
    the requests.Response on a network-level success (even if MDBList reported an error in
    the JSON body), or None if the request itself failed to complete."""
    url = template.format(base=MDBLIST_BASE, list_id=list_id)
    try:
        if method == "POST":
            resp = requests.post(url, params={"apikey": mdblist_api_key}, json=payload, timeout=30)
        else:
            resp = requests.put(url, params={"apikey": mdblist_api_key}, json=payload, timeout=30)
    except requests.RequestException as exc:
        log.debug("MDBList add attempt failed (%s %s): %s", method, url, exc)
        return False, None

    if resp.status_code == 404:
        log.debug("MDBList add attempt got 404 (%s %s) — trying next variant", method, url)
        return False, resp

    try:
        data = resp.json()
    except ValueError:
        data = None

    # MDBList's confirmed response shape for a successful modify is
    # {"added": {...}, "existing": {...}, "not_found": {...}} with counts
    # per category. Some error responses come back 200 with an "error" key.
    if resp.ok and isinstance(data, dict) and "error" not in data:
        return True, resp

    log.debug(
        "MDBList add attempt returned non-success (%s %s) status=%s body=%s",
        method,
        url,
        resp.status_code,
        resp.text[:300],
    )
    return False, resp


def add_item_to_mdblist(list_id: str, media_type: str, tmdb_id: str, mdblist_api_key: str) -> bool:
    """
    Adds one item to an MDBList static list.

    MDBList's own Apiary docs list a single "Modify Static List Items"
    endpoint under Static Lists (not separate add/remove URLs), but the
    exact path and HTTP method aren't confirmable from the published docs
    at the time of writing. Rather than hardcode one guess, this tries the
    most plausible variants in order, remembers whichever one actually
    works (module-level cache), and only probes again if the cached one
    stops working — e.g. after an MDBList API change.

    If NONE of the variants work, check the debug-level logs for what each
    attempt returned, and cross-check against https://api.mdblist.com/docs/
    or https://mdblist.docs.apiary.io/ for the current spec.
    """
    key = "movies" if media_type == "movie" else "shows"
    payload = {key: [{"tmdb": int(tmdb_id)}]}

    candidates = [
        ("{base}/lists/{list_id}/items", "POST"),
        ("{base}/lists/{list_id}/items", "PUT"),
        ("{base}/lists/{list_id}/items/", "POST"),
        ("{base}/lists/{list_id}/items/add", "POST"),
    ]

    # Try the previously-confirmed variant first, if we have one.
    cached = _mdblist_add_endpoint_cache
    if cached["template"] and (cached["template"], cached["method"]) in candidates:
        candidates.remove((cached["template"], cached["method"]))
        candidates.insert(0, (cached["template"], cached["method"]))

    last_resp = None
    for template, method in candidates:
        ok, resp = _try_mdblist_add(template, method, list_id, mdblist_api_key, payload)
        last_resp = resp or last_resp
        if ok:
            cached["template"], cached["method"] = template, method
            log.info(
                "Added %s:%s to MDBList list %s (via %s %s)",
                media_type,
                tmdb_id,
                list_id,
                method,
                template,
            )
            return True

    # Nothing worked — log the most informative failure we saw.
    if last_resp is not None:
        log.error(
            "MDBList add failed for list=%s %s:%s after trying %d endpoint variant(s) — "
            "last response (%s): %s. Cross-check against https://api.mdblist.com/docs/",
            list_id,
            media_type,
            tmdb_id,
            len(candidates),
            last_resp.status_code,
            last_resp.text[:500],
        )
    else:
        log.error(
            "MDBList add failed for list=%s %s:%s — no endpoint variant even got a response "
            "(network/connection issue). Check MDBLIST_BASE reachability and your API key.",
            list_id,
            media_type,
            tmdb_id,
        )
    return False


# --------------------------------------------------------------------------
# Scan cycle
# --------------------------------------------------------------------------


def notify(message: str, notify_url: str) -> None:
    if not notify_url:
        return
    try:
        requests.post(notify_url, json={"content": message}, timeout=10)
    except requests.RequestException as exc:
        log.warning("Notify failed: %s", exc)


def default_filter_reasoning(franchise: dict) -> str:
    """
    Fallback explanation for a filter-matched item when no LLM reasoning is
    available (LLM disabled, call failed, or this specific title wasn't
    annotated) — every item in the approval queue should have *something*
    explaining why it's there, not just the ones the LLM happened to touch.
    """
    tmdb_filter = franchise.get("tmdb_filter", {})
    ftype = tmdb_filter.get("type", "")
    fid = tmdb_filter.get("id", "")
    type_label = {
        "company": "studio/company",
        "keyword": "keyword",
        "network": "TV network",
    }.get(ftype, ftype or "filter")
    return f"Matched the TMDB {type_label} filter (ID {fid}) configured for {franchise['name']}."


def scan_franchise(franchise: dict, core: dict) -> dict:
    """Returns {'new_count': int, 'error': str|None, 'warnings': [str, ...]} for this franchise."""
    fid = franchise["id"]
    fname = franchise["name"]
    tmdb_api_key = core["tmdb_api_key"]
    mdblist_api_key = core["mdblist_api_key"]
    warnings = []
    log.info("Scanning %s...", fname)

    try:
        current = get_current_mdblist_tmdb_ids(franchise["mdblist_list_id"], mdblist_api_key)
    except requests.RequestException as exc:
        log.error("[%s] Could not fetch current MDBList items: %s", fid, exc)
        return {"new_count": 0, "error": f"MDBList error for {fname}: {exc}", "warnings": warnings}

    try:
        candidates = discover_candidates(franchise, tmdb_api_key)
    except requests.RequestException as exc:
        log.error("[%s] Could not query TMDB: %s", fid, exc)
        return {"new_count": 0, "error": f"TMDB error for {fname}: {exc}", "warnings": warnings}

    pending = load_pending(fid)
    rejected = load_rejected(fid)

    new_finds = []
    filter_matched = []
    for item in candidates:
        key = f"{item['media_type']}:{item['tmdb_id']}"
        if (item["media_type"], item["tmdb_id"]) in current:
            continue
        if key in pending or key in rejected:
            continue
        item["source"] = "filter"
        item["reasoning"] = ""
        item["llm_reasoning"] = False
        item["imdb_id"] = get_imdb_id(item["media_type"], item["tmdb_id"], tmdb_api_key)
        pending[key] = item
        new_finds.append(item)
        filter_matched.append(item)

    # --- LLM pass: annotate filter matches + find gaps ---------------------
    llm_enabled = load_llm_settings().get("provider", "none") != "none"
    if franchise["tmdb_filter"]["type"] == "none" and not llm_enabled:
        warnings.append(
            f"{fname} is set to \"None (LLM only)\" but no LLM provider is configured — "
            f"this franchise can't find anything until you set one on the Settings page."
        )
    if llm_enabled:
        # Build a plain-title view of what the LLM should treat as "already have":
        # current list contents + everything already sitting in the pending queue
        # (including what the filter pass just added above).
        known_titles = {i["title"] for i in candidates if (i["media_type"], i["tmdb_id"]) in current}
        known_titles |= {i["title"] for i in pending.values()}
        known_titles |= {i["title"] for i in rejected.values()}

        settings_for_error = load_llm_settings()
        try:
            result = llm_gap_and_annotate(franchise, filter_matched, sorted(known_titles))
        except (requests.RequestException, ValueError) as exc:
            msg = _friendly_llm_error(exc, settings_for_error.get("provider", ""))
            log.warning("[%s] LLM call failed: %s", fid, msg)
            warnings.append(f"{fname}: {msg}")
        else:
            # Apply reasoning to the items the filter already found.
            # Positional matching first — the prompt guarantees the LLM
            # returns annotations in the same order as the candidates it
            # was given, which is more reliable than a title-string match
            # (the LLM can paraphrase punctuation/casing slightly).
            # Title-based lookup is the fallback for when counts drift.
            by_order = result["annotations_by_order"]
            by_title = result["annotations_by_title"]
            for idx, item in enumerate(filter_matched):
                reasoning = by_order[idx] if idx < len(by_order) else ""
                if not reasoning:
                    reasoning = by_title.get(item["title"].strip().lower(), "")
                if reasoning:
                    item["reasoning"] = reasoning
                    item["llm_reasoning"] = True
                    pending[f"{item['media_type']}:{item['tmdb_id']}"]["reasoning"] = reasoning
                    pending[f"{item['media_type']}:{item['tmdb_id']}"]["llm_reasoning"] = True

            # Add anything the LLM flagged as missing entirely.
            for s in result["missing"]:
                tmdb_id = resolve_tmdb_id(s["title"], s.get("year", ""), s["media_type"], tmdb_api_key)
                if not tmdb_id:
                    log.info(
                        "[%s] LLM suggested %r but no TMDB match found — skipping",
                        fid,
                        s["title"],
                    )
                    continue
                key = f"{s['media_type']}:{tmdb_id}"
                if (s["media_type"], tmdb_id) in current or key in pending or key in rejected:
                    continue
                item = {
                    "tmdb_id": tmdb_id,
                    "media_type": s["media_type"],
                    "title": s["title"],
                    "year": s.get("year", ""),
                    "source": "llm",
                    "reasoning": s.get("reasoning") or "Suggested by the LLM gap-finding pass.",
                    "llm_reasoning": True,
                    "imdb_id": get_imdb_id(s["media_type"], tmdb_id, tmdb_api_key),
                }
                pending[key] = item
                new_finds.append(item)

    # Every item in the approval queue should have *something* explaining
    # why it's there — fall back to a deterministic explanation for
    # anything that still has no reasoning (LLM disabled, call failed, or
    # this particular title wasn't annotated).
    for item in new_finds:
        if not item.get("reasoning"):
            fallback = default_filter_reasoning(franchise)
            item["reasoning"] = fallback
            pending[f"{item['media_type']}:{item['tmdb_id']}"]["reasoning"] = fallback

    if new_finds:
        save_pending(fid, pending)
        titles = ", ".join(f"{i['title']} ({i['year']})" for i in new_finds)
        log.info("[%s] Found %d new candidate(s): %s", fid, len(new_finds), titles)
        notify(
            f"{fname}: {len(new_finds)} new title(s) awaiting approval — {titles}",
            core["notify_url"],
        )
    else:
        log.info("[%s] No new candidates found.", fid)

    return {"new_count": len(new_finds), "error": None, "warnings": warnings}


def run_scan() -> dict:
    """
    Runs one scan cycle across all franchises and returns a summary:
    {'skipped': bool, 'franchise_count': int, 'total_new': int,
     'errors': [str, ...], 'warnings': [str, ...]}
    """
    with _lock:
        core = load_core_settings()
        if not core["tmdb_api_key"] or not core["mdblist_api_key"]:
            log.warning(
                "Skipping scan — TMDB and/or MDBList API key not configured yet. "
                "Set them on the Settings page."
            )
            return {"skipped": True, "franchise_count": 0, "total_new": 0, "errors": [], "warnings": []}

        franchises = load_franchises()
        total_new = 0
        errors = []
        warnings = []
        for franchise in franchises:
            result = scan_franchise(franchise, core)
            total_new += result["new_count"]
            if result["error"]:
                errors.append(result["error"])
            for w in result["warnings"]:
                if w not in warnings:  # de-dupe identical rate-limit messages across franchises
                    warnings.append(w)

        return {
            "skipped": False,
            "franchise_count": len(franchises),
            "total_new": total_new,
            "errors": errors,
            "warnings": warnings,
        }


def scan_loop() -> None:
    while True:
        run_scan()
        try:
            interval_hours = float(load_core_settings()["check_interval_hours"])
        except (TypeError, ValueError):
            interval_hours = 24.0
        time.sleep(max(interval_hours, 0.1) * 3600)


# --------------------------------------------------------------------------
# Web UI
# --------------------------------------------------------------------------


@app.get("/")
def index():
    franchises = load_franchises()
    groups = []
    total = 0
    for f in franchises:
        pending = load_pending(f["id"])
        items = sorted(pending.values(), key=lambda i: (i["year"] or "", i["title"]))
        total += len(items)
        groups.append({"id": f["id"], "name": f["name"], "pending_items": items})

    core = load_core_settings()
    needs_setup = not core["tmdb_api_key"] or not core["mdblist_api_key"]

    return render_template(
        "index.html",
        groups=groups,
        total=total,
        needs_setup=needs_setup,
        scan_result=request.args.get("scan_result"),
        scan_found=request.args.get("scan_found"),
        scan_errors=request.args.get("scan_errors"),
        scan_warnings=request.args.get("scan_warnings"),
    )


@app.post("/approve/<franchise_id>/<media_type>/<tmdb_id>")
def approve(franchise_id: str, media_type: str, tmdb_id: str):
    franchise = get_franchise(franchise_id)
    key = f"{media_type}:{tmdb_id}"
    pending = load_pending(franchise_id)
    item = pending.pop(key, None)
    if item:
        mdblist_api_key = load_core_settings()["mdblist_api_key"]
        ok = add_item_to_mdblist(franchise["mdblist_list_id"], media_type, tmdb_id, mdblist_api_key)
        if not ok:
            pending[key] = item  # don't lose it if the push failed
            save_pending(franchise_id, pending)
            return redirect(
                url_for(
                    "index",
                    scan_result="error",
                    scan_errors=(
                        f"Couldn't add {item['title']} to MDBList — the item stayed in your "
                        f"approval queue so you haven't lost it. Check the app logs for the "
                        f"MDBList response and see the README's MDBList troubleshooting section."
                    ),
                )
            )
        save_pending(franchise_id, pending)
    return redirect(url_for("index"))


@app.post("/reject/<franchise_id>/<media_type>/<tmdb_id>")
def reject(franchise_id: str, media_type: str, tmdb_id: str):
    key = f"{media_type}:{tmdb_id}"
    pending = load_pending(franchise_id)
    item = pending.pop(key, None)
    if item:
        rejected = load_rejected(franchise_id)
        rejected[key] = item
        save_rejected(franchise_id, rejected)
        save_pending(franchise_id, pending)
    return redirect(url_for("index"))


@app.get("/plex")
def plex_page():
    core = load_core_settings()
    return render_template(
        "plex.html",
        franchises=load_franchises(),
        plex_configured=bool(core["plex_url"] and core["plex_token"]),
        scan_result=request.args.get("scan_result"),
        scan_message=request.args.get("scan_message"),
    )


@app.get("/plex/libraries")
def plex_libraries():
    """JSON endpoint the Plex Import page calls to populate the library picker."""
    core = load_core_settings()
    if not core["plex_url"] or not core["plex_token"]:
        return jsonify({"libraries": [], "error": "Plex isn't configured yet on the Settings page."})
    try:
        libraries = get_plex_libraries(core["plex_url"], core["plex_token"])
    except requests.RequestException as exc:
        return jsonify({"libraries": [], "error": f"Could not reach Plex: {exc}"})
    return jsonify({"libraries": libraries, "error": None})


@app.post("/plex/scan")
def plex_scan():
    core = load_core_settings()
    franchise_id = request.form.get("franchise_id", "")
    library_key = request.form.get("library_key", "")

    if not core["plex_url"] or not core["plex_token"]:
        return redirect(url_for("plex_page", scan_result="error", scan_message="Plex isn't configured on the Settings page."))
    if not core["tmdb_api_key"]:
        return redirect(url_for("plex_page", scan_result="error", scan_message="TMDB API key isn't configured on the Settings page."))
    if not library_key:
        return redirect(url_for("plex_page", scan_result="error", scan_message="Pick a Plex library first."))

    franchise = get_franchise(franchise_id)

    try:
        plex_items = get_plex_library_items(core["plex_url"], core["plex_token"], library_key)
    except requests.RequestException as exc:
        return redirect(url_for("plex_page", scan_result="error", scan_message=f"Could not read Plex library: {exc}"))

    try:
        current = get_current_mdblist_tmdb_ids(franchise["mdblist_list_id"], core["mdblist_api_key"])
    except requests.RequestException as exc:
        return redirect(url_for("plex_page", scan_result="error", scan_message=f"Could not read MDBList list: {exc}"))

    pending = load_pending(franchise["id"])
    rejected = load_rejected(franchise["id"])
    tmdb_filter = franchise["tmdb_filter"]
    is_llm_only = tmdb_filter.get("type") == "none"

    # Resolve any Plex items missing a TMDB ID (legacy-agent libraries) by title/year search.
    for item in plex_items:
        if not item["tmdb_id"]:
            item["tmdb_id"] = resolve_tmdb_id(item["title"], item["year"], item["media_type"], core["tmdb_api_key"])

    checkable = [i for i in plex_items if i["tmdb_id"]]
    unresolved_count = len(plex_items) - len(checkable)

    # Skip anything already in the target list or already queued/rejected.
    to_check = [
        i for i in checkable
        if (i["media_type"], i["tmdb_id"]) not in current
        and f"{i['media_type']}:{i['tmdb_id']}" not in pending
        and f"{i['media_type']}:{i['tmdb_id']}" not in rejected
    ]
    already_excluded_count = len(checkable) - len(to_check)

    log.info(
        "[%s] Plex scan funnel: fetched=%d resolved_to_tmdb=%d "
        "already_listed_or_queued_or_rejected=%d to_check=%d",
        franchise["id"],
        len(plex_items),
        len(checkable),
        already_excluded_count,
        len(to_check),
    )

    new_finds = []
    llm_batch_failures = 0

    if is_llm_only:
        if load_llm_settings().get("provider", "none") == "none":
            return redirect(
                url_for(
                    "plex_page",
                    scan_result="error",
                    scan_message=f"{franchise['name']} is set to \"None (LLM only)\" but no LLM provider is configured.",
                )
            )
        batch_size = 25
        for i in range(0, len(to_check), batch_size):
            batch = to_check[i : i + batch_size]
            try:
                classifications = llm_classify_plex_batch(franchise, batch)
            except (requests.RequestException, ValueError) as exc:
                log.warning("[%s] Plex LLM classification failed: %s", franchise["id"], exc)
                llm_batch_failures += 1
                continue
            for idx, plex_item in enumerate(batch):
                c = classifications[idx] if idx < len(classifications) else None
                if not isinstance(c, dict):
                    continue
                belongs = c.get("belongs")
                belongs = belongs is True or (
                    isinstance(belongs, str) and belongs.strip().lower() == "true"
                )
                if not belongs:
                    continue
                key = f"{plex_item['media_type']}:{plex_item['tmdb_id']}"
                item = {
                    "tmdb_id": plex_item["tmdb_id"],
                    "media_type": plex_item["media_type"],
                    "title": plex_item["title"],
                    "year": plex_item["year"],
                    "source": "plex",
                    "reasoning": c.get("reasoning") or "Matched by the LLM against your Plex library.",
                    "llm_reasoning": True,
                    "imdb_id": get_imdb_id(plex_item["media_type"], plex_item["tmdb_id"], core["tmdb_api_key"]),
                }
                pending[key] = item
                new_finds.append(item)
    else:
        for plex_item in to_check:
            is_match = matches_tmdb_filter(plex_item["media_type"], plex_item["tmdb_id"], tmdb_filter, core["tmdb_api_key"])
            if not is_match:
                continue
            key = f"{plex_item['media_type']}:{plex_item['tmdb_id']}"
            item = {
                "tmdb_id": plex_item["tmdb_id"],
                "media_type": plex_item["media_type"],
                "title": plex_item["title"],
                "year": plex_item["year"],
                "source": "plex",
                "reasoning": (
                    f"Already in your Plex library and matches the TMDB "
                    f"{tmdb_filter['type']} filter configured for {franchise['name']}."
                ),
                "llm_reasoning": False,
                "imdb_id": get_imdb_id(plex_item["media_type"], plex_item["tmdb_id"], core["tmdb_api_key"]),
            }
            pending[key] = item
            new_finds.append(item)

    if new_finds:
        save_pending(franchise["id"], pending)

    msg = (
        f"Checked {len(plex_items)} Plex item(s) from this library — "
        f"{len(checkable)} resolved to a TMDB ID, "
        f"{already_excluded_count} already in the list/queue/rejected, "
        f"{len(to_check)} actually checked against {franchise['name']}, "
        f"found {len(new_finds)} match(es), added to Approvals."
    )
    if unresolved_count:
        msg += f" {unresolved_count} item(s) had no TMDB match and were skipped."
    if llm_batch_failures:
        msg += f" {llm_batch_failures} LLM batch(es) failed and were skipped — check the app logs."
    return redirect(url_for("plex_page", scan_result="done", scan_message=msg))


@app.get("/rejected")
def rejected_page():
    franchises = load_franchises()
    groups = []
    total = 0
    for f in franchises:
        rejected = load_rejected(f["id"])
        items = sorted(rejected.values(), key=lambda i: (i["year"] or "", i["title"]))
        total += len(items)
        groups.append({"id": f["id"], "name": f["name"], "rejected_items": items})
    return render_template("rejected.html", groups=groups, total=total)


@app.post("/rejected/recheck/<franchise_id>/<media_type>/<tmdb_id>")
def rejected_recheck(franchise_id: str, media_type: str, tmdb_id: str):
    """Moves one item back from rejected into the pending approval queue."""
    key = f"{media_type}:{tmdb_id}"
    rejected = load_rejected(franchise_id)
    item = rejected.pop(key, None)
    if item:
        pending = load_pending(franchise_id)
        pending[key] = item
        save_pending(franchise_id, pending)
        save_rejected(franchise_id, rejected)
    return redirect(url_for("rejected_page"))


@app.post("/rejected/recheck-all/<franchise_id>")
def rejected_recheck_all(franchise_id: str):
    """Moves every rejected item for one franchise back into the pending queue at once —
    handy after updating a franchise's filter or LLM hint, when old rejections may no
    longer reflect the current criteria."""
    rejected = load_rejected(franchise_id)
    if rejected:
        pending = load_pending(franchise_id)
        pending.update(rejected)
        save_pending(franchise_id, pending)
        save_rejected(franchise_id, {})
    return redirect(url_for("rejected_page"))


@app.post("/rejected/delete/<franchise_id>/<media_type>/<tmdb_id>")
def rejected_delete(franchise_id: str, media_type: str, tmdb_id: str):
    """Permanently removes one item from the rejected list (not moved anywhere)."""
    key = f"{media_type}:{tmdb_id}"
    rejected = load_rejected(franchise_id)
    if key in rejected:
        del rejected[key]
        save_rejected(franchise_id, rejected)
    return redirect(url_for("rejected_page"))


@app.post("/scan-now")
def scan_now():
    result = run_scan()

    if result["skipped"]:
        return redirect(url_for("index", scan_result="skipped"))

    return redirect(
        url_for(
            "index",
            scan_result="error" if result["errors"] else "done",
            scan_found=result["total_new"],
            scan_errors=" | ".join(result["errors"]) if result["errors"] else None,
            scan_warnings=" | ".join(result["warnings"]) if result["warnings"] else None,
        )
    )


@app.get("/health")
def health():
    return {"status": "ok"}


# --------------------------------------------------------------------------
# Settings UI (core API keys + LLM provider)
# --------------------------------------------------------------------------


def test_tmdb_key(key: str):
    try:
        resp = requests.get(f"{TMDB_BASE}/configuration", params={"api_key": key}, timeout=15)
        if resp.ok:
            return True, "TMDB key works."
        return False, f"TMDB error {resp.status_code}: {resp.text[:150]}"
    except requests.RequestException as exc:
        return False, f"TMDB connection failed: {exc}"


def test_mdblist_key(key: str):
    try:
        resp = requests.get(f"{MDBLIST_BASE}/user", params={"apikey": key}, timeout=15)
        if resp.ok:
            return True, "MDBList key works."
        return False, f"MDBList error {resp.status_code}: {resp.text[:150]}"
    except requests.RequestException as exc:
        return False, f"MDBList connection failed: {exc}"


@app.get("/settings")
def settings_page():
    llm_settings = load_llm_settings()
    llm_display = {**llm_settings, "api_key_masked": mask_key(llm_settings.get("api_key", ""))}

    core_settings = load_core_settings()
    core_display = {
        **core_settings,
        "tmdb_api_key_masked": mask_key(core_settings.get("tmdb_api_key", "")),
        "mdblist_api_key_masked": mask_key(core_settings.get("mdblist_api_key", "")),
        "plex_token_masked": mask_key(core_settings.get("plex_token", "")),
    }

    return render_template(
        "settings.html",
        settings=llm_display,
        providers=LLM_PROVIDERS,
        core=core_display,
        test_result=request.args.get("test_result"),
        test_message=request.args.get("test_message"),
        core_test_result=request.args.get("core_test_result"),
        core_test_message=request.args.get("core_test_message"),
    )


@app.post("/settings/core/save")
def settings_core_save():
    existing = load_core_settings()

    submitted_tmdb = request.form.get("tmdb_api_key", "").strip()
    if submitted_tmdb and submitted_tmdb == mask_key(existing.get("tmdb_api_key", "")):
        submitted_tmdb = existing.get("tmdb_api_key", "")

    submitted_mdblist = request.form.get("mdblist_api_key", "").strip()
    if submitted_mdblist and submitted_mdblist == mask_key(existing.get("mdblist_api_key", "")):
        submitted_mdblist = existing.get("mdblist_api_key", "")

    submitted_plex_token = request.form.get("plex_token", "").strip()
    if submitted_plex_token and submitted_plex_token == mask_key(existing.get("plex_token", "")):
        submitted_plex_token = existing.get("plex_token", "")

    interval_raw = request.form.get("check_interval_hours", "").strip() or "24"
    try:
        interval = float(interval_raw)
        if interval <= 0:
            raise ValueError
    except ValueError:
        return redirect(
            url_for(
                "settings_page",
                core_test_result="error",
                core_test_message="Check interval must be a positive number of hours.",
            )
        )

    new_core = {
        "tmdb_api_key": submitted_tmdb,
        "mdblist_api_key": submitted_mdblist,
        "check_interval_hours": str(interval),
        "notify_url": request.form.get("notify_url", "").strip(),
        "plex_url": request.form.get("plex_url", "").strip().rstrip("/"),
        "plex_token": submitted_plex_token,
    }
    save_core_settings(new_core)

    if request.form.get("action") == "test":
        messages = []
        all_ok = True
        if new_core["tmdb_api_key"]:
            ok, msg = test_tmdb_key(new_core["tmdb_api_key"])
            all_ok = all_ok and ok
            messages.append(msg)
        else:
            all_ok = False
            messages.append("No TMDB key set.")
        if new_core["mdblist_api_key"]:
            ok, msg = test_mdblist_key(new_core["mdblist_api_key"])
            all_ok = all_ok and ok
            messages.append(msg)
        else:
            all_ok = False
            messages.append("No MDBList key set.")
        if new_core["plex_url"] and new_core["plex_token"]:
            ok, msg = test_plex_connection(new_core["plex_url"], new_core["plex_token"])
            all_ok = all_ok and ok
            messages.append(msg)
        elif new_core["plex_url"] or new_core["plex_token"]:
            all_ok = False
            messages.append("Plex needs both a server URL and a token to test.")
        else:
            messages.append("Plex not configured (optional).")
        return redirect(
            url_for(
                "settings_page",
                core_test_result="success" if all_ok else "error",
                core_test_message=" ".join(messages),
            )
        )

    return redirect(url_for("settings_page", core_test_result="saved"))


def _friendly_llm_error(exc: Exception, provider: str) -> str:
    """
    Adds an actionable hint for the most common failure modes: a model ID
    that's been renamed/retired (404), or a rate limit / quota cap being
    hit (429) — the latter is expected behavior on free tiers, not a bug.
    """
    text = str(exc)
    label = LLM_PROVIDERS.get(provider, {}).get("label", provider or "LLM provider")
    status_code = getattr(getattr(exc, "response", None), "status_code", None)

    is_429 = status_code == 429 or "429" in text or "rate limit" in text.lower() or "quota" in text.lower()
    if is_429:
        return (
            f"{label} rate limit or quota reached. This is expected on free "
            f"tiers if you scan often or track many franchises — the LLM "
            f"gap-finding pass was skipped this cycle and will retry next "
            f"scan automatically."
        )

    is_404 = status_code == 404 or "404" in text
    if not is_404:
        return f"Connection failed: {text}"

    hints = {
        "gemini": "Check https://ai.google.dev/gemini-api/docs/models for a current model ID, or clear the Model field to fall back to the default.",
        "groq": "Check https://console.groq.com/docs/models for a current model ID, or clear the Model field to fall back to the default.",
        "anthropic": "Check https://docs.claude.com/en/docs/about-claude/models for a current model ID, or clear the Model field to fall back to the default.",
        "ollama": "Check that the model has been pulled on your Ollama server (docker exec -it ollama ollama pull <model>) and that the name matches exactly.",
    }
    hint = hints.get(provider, "The model ID may be wrong or retired — check the provider's current model list.")
    return f"Connection failed (404 — model not found). This usually means the model ID is outdated. {hint}"


@app.post("/settings/save")
def settings_save():
    provider = request.form.get("provider", "none")
    if provider not in LLM_PROVIDERS:
        return redirect(
            url_for("settings_page", test_result="error", test_message="Unknown provider.")
        )

    existing = load_llm_settings()
    submitted_key = request.form.get("api_key", "").strip()
    # A masked key means the user didn't change it — keep the stored one.
    if submitted_key and submitted_key == mask_key(existing.get("api_key", "")):
        submitted_key = existing.get("api_key", "")

    new_settings = {
        "provider": provider,
        "api_key": submitted_key,
        "model": request.form.get("model", "").strip(),
        "base_url": request.form.get("base_url", "").strip(),
    }

    info = LLM_PROVIDERS[provider]
    if info["needs_key"] and not new_settings["api_key"]:
        return redirect(
            url_for(
                "settings_page",
                test_result="error",
                test_message=f"{info['label']} requires an API key.",
            )
        )

    save_llm_settings(new_settings)

    if request.form.get("action") == "test":
        test_prompt = 'Reply with exactly one word: OK'
        try:
            reply = call_llm(test_prompt, new_settings)
            ok = "ok" in reply.strip().lower()
            msg = f"Got a response: {reply.strip()[:200]}" if reply.strip() else "Empty response from provider."
            return redirect(
                url_for(
                    "settings_page",
                    test_result="success" if ok else "warning",
                    test_message=msg,
                )
            )
        except (requests.RequestException, ValueError) as exc:
            return redirect(
                url_for(
                    "settings_page",
                    test_result="error",
                    test_message=_friendly_llm_error(exc, provider),
                )
            )

    return redirect(url_for("settings_page", test_result="saved"))



# --------------------------------------------------------------------------
# Franchise management UI
# --------------------------------------------------------------------------


def _parse_franchise_form(form, existing_ids: set, editing_id: str = None) -> dict:
    name = form.get("name", "").strip()
    if not name:
        raise ValueError("Name is required.")

    mdblist_list_id = form.get("mdblist_list_id", "").strip()
    if not mdblist_list_id:
        raise ValueError("MDBList list ID is required.")

    filter_type = form.get("filter_type", "").strip()
    if filter_type not in VALID_FILTER_TYPES:
        raise ValueError("Filter type must be company, keyword, network, or none.")

    filter_id_raw = form.get("filter_id", "").strip()
    if filter_type == "none":
        if load_llm_settings().get("provider", "none") == "none":
            raise ValueError(
                "\"None (LLM only)\" requires an LLM provider to be configured on the "
                "Settings page first — otherwise this franchise would never find anything."
            )
        filter_id = 0
    else:
        if not filter_id_raw.isdigit():
            raise ValueError("Filter ID must be a number (TMDB company/keyword/network ID).")
        filter_id = int(filter_id_raw)

    exclude_keywords = [
        kw.strip() for kw in form.get("exclude_title_keywords", "").split(",") if kw.strip()
    ]
    exclude_ids = [
        eid.strip() for eid in form.get("exclude_tmdb_ids", "").split(",") if eid.strip()
    ]
    llm_hint = form.get("llm_hint", "").strip()

    if editing_id:
        franchise_id = editing_id
    else:
        franchise_id = unique_id(slugify(name), existing_ids)

    return {
        "id": franchise_id,
        "name": name,
        "mdblist_list_id": mdblist_list_id,
        "tmdb_filter": {"type": filter_type, "id": filter_id},
        "exclude_title_keywords": exclude_keywords,
        "exclude_tmdb_ids": exclude_ids,
        "llm_hint": llm_hint,
    }


@app.get("/franchises/mdblist-lists")
def franchises_mdblist_lists():
    """
    JSON endpoint the Manage Franchises page calls to populate a picker of
    the user's actual MDBList lists (with real numeric IDs) instead of
    requiring them to copy a list ID from a URL — see get_my_mdblist_lists().
    """
    mdblist_api_key = load_core_settings()["mdblist_api_key"]
    if not mdblist_api_key:
        return jsonify({"lists": [], "error": "No MDBList API key set on the Settings page."})
    try:
        lists = get_my_mdblist_lists(mdblist_api_key)
    except requests.RequestException as exc:
        return jsonify({"lists": [], "error": f"Could not reach MDBList: {exc}"})
    return jsonify({"lists": lists, "error": None})


@app.get("/franchises")
def franchises_page():
    return render_template(
        "franchises.html",
        franchises=load_franchises(),
        editing=None,
        error=request.args.get("error"),
    )


@app.get("/franchises/edit/<franchise_id>")
def franchises_edit_form(franchise_id: str):
    franchise = get_franchise(franchise_id)
    return render_template(
        "franchises.html", franchises=load_franchises(), editing=franchise, error=None
    )


@app.post("/franchises/add")
def franchises_add():
    with _config_lock:
        franchises = load_franchises()
        existing_ids = {f["id"] for f in franchises}
        try:
            new_franchise = _parse_franchise_form(request.form, existing_ids)
        except ValueError as exc:
            return redirect(url_for("franchises_page", error=str(exc)))
        franchises.append(new_franchise)
        save_franchises(franchises)
    return redirect(url_for("franchises_page"))


@app.post("/franchises/update/<franchise_id>")
def franchises_update(franchise_id: str):
    with _config_lock:
        franchises = load_franchises()
        if not any(f["id"] == franchise_id for f in franchises):
            abort(404)
        existing_ids = {f["id"] for f in franchises} - {franchise_id}
        try:
            updated = _parse_franchise_form(request.form, existing_ids, editing_id=franchise_id)
        except ValueError as exc:
            return redirect(url_for("franchises_page", error=str(exc)))
        franchises = [updated if f["id"] == franchise_id else f for f in franchises]
        save_franchises(franchises)
    return redirect(url_for("franchises_page"))


@app.post("/franchises/delete/<franchise_id>")
def franchises_delete(franchise_id: str):
    with _config_lock:
        franchises = [f for f in load_franchises() if f["id"] != franchise_id]
        save_franchises(franchises)
    return redirect(url_for("franchises_page"))


if __name__ == "__main__":
    threading.Thread(target=scan_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8420)
