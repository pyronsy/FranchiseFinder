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
from flask import Flask, abort, redirect, render_template, request, url_for

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

VALID_FILTER_TYPES = {"company", "keyword", "network"}


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
    fid = tmdb_filter["id"]
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


# --------------------------------------------------------------------------
# MDBList
# --------------------------------------------------------------------------


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


def add_item_to_mdblist(list_id: str, media_type: str, tmdb_id: str, mdblist_api_key: str) -> bool:
    """
    NOTE: payload shape follows MDBList's documented static-list add
    pattern as of writing. If this fails, check the logged response body
    against https://api.mdblist.com/docs/.
    """
    key = "movies" if media_type == "movie" else "shows"
    payload = {key: [{"tmdb": int(tmdb_id)}]}
    resp = requests.post(
        f"{MDBLIST_BASE}/lists/{list_id}/items/add",
        params={"apikey": mdblist_api_key},
        json=payload,
        timeout=30,
    )
    if not resp.ok:
        log.error(
            "MDBList add failed (%s) for list=%s %s:%s -> %s",
            resp.status_code,
            list_id,
            media_type,
            tmdb_id,
            resp.text[:500],
        )
        return False
    log.info("Added %s:%s to MDBList list %s", media_type, tmdb_id, list_id)
    return True


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
        pending[key] = item
        new_finds.append(item)
        filter_matched.append(item)

    # --- LLM pass: annotate filter matches + find gaps ---------------------
    if load_llm_settings().get("provider", "none") != "none":
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
        raise ValueError("Filter type must be company, keyword, or network.")

    filter_id_raw = form.get("filter_id", "").strip()
    if not filter_id_raw.isdigit():
        raise ValueError("Filter ID must be a number (TMDB company/keyword/network ID).")

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
        "tmdb_filter": {"type": filter_type, "id": int(filter_id_raw)},
        "exclude_title_keywords": exclude_keywords,
        "exclude_tmdb_ids": exclude_ids,
        "llm_hint": llm_hint,
    }


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
