import copy
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

API_TOKEN = os.getenv("API_TOKEN")
if not API_TOKEN:
    raise RuntimeError("API_TOKEN is missing")

BASE = "https://api.football-data.org/v4"
OUT = Path("live-results.json")
COMPETITIONS = ["CL", "PL", "ELC", "PD", "BL1", "SA", "FL1", "DED", "PPL", "BSA", "WC", "EC"]
STANDINGS_CODES = {"CL", "PL", "ELC", "PD", "BL1", "SA", "FL1", "DED", "PPL", "BSA"}
REQUEST_SPACING_SECONDS = 8
MAX_ATTEMPTS = 3
HEADERS = {"X-Auth-Token": API_TOKEN}
SESSION = requests.Session()


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def load_previous():
    if not OUT.exists():
        return {}
    try:
        value = json.loads(OUT.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def api_get(path):
    url = BASE + path
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = SESSION.get(url, headers=HEADERS, timeout=(10, 25))
            print(path, response.status_code, f"attempt {attempt}/{MAX_ATTEMPTS}")
            if response.status_code == 200:
                return response.json()
            if response.status_code == 429 or 500 <= response.status_code < 600:
                retry_after = response.headers.get("Retry-After", "").strip()
                try:
                    delay = min(20, max(3, int(retry_after))) if retry_after else min(15, 3 * attempt)
                except ValueError:
                    delay = min(15, 3 * attempt)
                last_error = RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
                if attempt < MAX_ATTEMPTS:
                    time.sleep(delay)
                    continue
            else:
                last_error = RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
            break
        except requests.RequestException as exc:
            last_error = exc
            if attempt < MAX_ATTEMPTS:
                time.sleep(min(15, 3 * attempt))
                continue
    print("Request failed:", path, str(last_error)[:300])
    return None


def team_name(team):
    if not isinstance(team, dict):
        return ""
    return (team.get("name") or team.get("shortName") or team.get("tla") or "").strip()


def competition_code(match):
    if not isinstance(match, dict):
        return ""
    comp = match.get("competition")
    if isinstance(comp, dict):
        value = comp.get("code") or comp.get("name") or ""
    else:
        value = comp or ""
    value = match.get("competitionCode") or value
    return str(value).strip().upper()


def match_key(match):
    mid = match.get("id") if isinstance(match, dict) else None
    if mid is not None:
        return "id:" + str(mid)
    date = match.get("utcDate") or match.get("date") or ""
    return "|".join([
        competition_code(match), str(date),
        team_name(match.get("homeTeam")), team_name(match.get("awayTeam")),
    ])


def merge_matches(existing, incoming):
    merged = {}
    order = []
    for row in existing or []:
        if not isinstance(row, dict):
            continue
        key = match_key(row)
        if key not in merged:
            order.append(key)
        merged[key] = copy.deepcopy(row)
    for row in incoming or []:
        if not isinstance(row, dict):
            continue
        key = match_key(row)
        old = merged.get(key, {})
        combined = copy.deepcopy(old)
        combined.update(copy.deepcopy(row))
        if key not in merged:
            order.append(key)
        merged[key] = combined
    return [merged[key] for key in order]


def season_info(match_data):
    matches = match_data.get("matches") or [] if isinstance(match_data, dict) else []
    season = None
    for row in matches:
        if isinstance(row, dict) and isinstance(row.get("season"), dict):
            season = row.get("season")
            break
    if season is None and isinstance(match_data, dict) and isinstance(match_data.get("season"), dict):
        season = match_data.get("season")
    season = season or {}
    return {
        "seasonId": season.get("id"),
        "seasonStart": season.get("startDate") or "",
        "seasonEnd": season.get("endDate") or "",
    }


def existing_season_ids(rows):
    ids = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        season = row.get("season")
        if isinstance(season, dict) and season.get("id") is not None:
            ids.add(str(season.get("id")))
    return ids


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def status_of(match):
    return str(match.get("status") or "").upper()


def stable_match_sort_key(match):
    return (
        str(match.get("utcDate") or match.get("date") or ""),
        competition_code(match),
        str(match.get("id") or ""),
        team_name(match.get("homeTeam")),
        team_name(match.get("awayTeam")),
    )


def rebuild_derived(root):
    matches = root.get("matches") or []
    root["live"] = [m for m in matches if status_of(m) in {"LIVE", "IN_PLAY", "PAUSED"}]
    root["finished"] = [m for m in matches if status_of(m) == "FINISHED"]
    root["upcoming"] = [m for m in matches if status_of(m) in {"TIMED", "SCHEDULED"}]


def semantic_snapshot(root):
    value = copy.deepcopy(root or {})
    value.pop("generatedAt", None)
    value.pop("lastUpdate", None)
    return canonical(value)


def main():
    previous = load_previous()
    root = copy.deepcopy(previous)
    root.setdefault("source", "football-data.org")
    root.setdefault("matches", [])
    root.setdefault("standings", {})
    root.setdefault("sources", {})

    all_matches = [copy.deepcopy(x) for x in (root.get("matches") or []) if isinstance(x, dict)]
    standings = copy.deepcopy(root.get("standings") or {})
    sources = copy.deepcopy(root.get("sources") or {})
    successful_requests = 0
    request_number = 0

    for code in COMPETITIONS:
        if request_number:
            time.sleep(REQUEST_SPACING_SECONDS)
        match_data = api_get(f"/competitions/{code}/matches")
        request_number += 1

        comp_changed = False
        if match_data is not None:
            successful_requests += 1
            incoming = [copy.deepcopy(x) for x in (match_data.get("matches") or []) if isinstance(x, dict)]
            old_comp = [x for x in all_matches if competition_code(x) == code]
            non_comp = [x for x in all_matches if competition_code(x) != code]
            season = season_info(match_data)
            old_source = copy.deepcopy(sources.get(code) or {})
            old_season = old_source.get("seasonId")
            incoming_season = season.get("seasonId")
            row_seasons = existing_season_ids(old_comp)
            rollover = False
            if incoming_season is not None:
                incoming_season_text = str(incoming_season)
                if old_season is not None and str(old_season) != incoming_season_text:
                    rollover = True
                elif row_seasons and incoming_season_text not in row_seasons:
                    rollover = True

            new_comp = incoming if rollover else merge_matches(old_comp, incoming)
            new_comp.sort(key=stable_match_sort_key)
            comp_changed = canonical(old_comp) != canonical(new_comp)
            all_matches = non_comp + new_comp

            new_source = copy.deepcopy(old_source)
            new_source["status"] = "connected"
            new_source["matchCount"] = len(incoming)
            if incoming_season is not None:
                new_source["seasonId"] = incoming_season
            if season.get("seasonStart"):
                new_source["seasonStart"] = season["seasonStart"]
            if season.get("seasonEnd"):
                new_source["seasonEnd"] = season["seasonEnd"]
            if comp_changed or canonical(old_source) != canonical(new_source):
                new_source["lastSuccessfulUpdate"] = utc_now()
            sources[code] = new_source
        else:
            print(f"{code}: keeping previously persisted match data unchanged")

        if code in STANDINGS_CODES:
            time.sleep(REQUEST_SPACING_SECONDS)
            standing_data = api_get(f"/competitions/{code}/standings")
            request_number += 1
            if standing_data is not None:
                successful_requests += 1
                incoming_standings = standing_data if standing_data.get("standings") else None
                if incoming_standings is not None:
                    old_standing = standings.get(code)
                    if canonical(old_standing) != canonical(incoming_standings):
                        standings[code] = copy.deepcopy(incoming_standings)
                        source = copy.deepcopy(sources.get(code) or {})
                        source["status"] = source.get("status") or "connected"
                        source["lastSuccessfulUpdate"] = utc_now()
                        sources[code] = source

    if successful_requests == 0:
        raise RuntimeError("No football-data.org request succeeded; live-results.json left unchanged")

    all_matches.sort(key=stable_match_sort_key)
    root["matches"] = all_matches
    root["standings"] = standings
    root["sources"] = sources
    root["competitionCodes"] = COMPETITIONS
    rebuild_derived(root)

    if semantic_snapshot(root) == semantic_snapshot(previous):
        print("No football data changes detected; live-results.json left untouched")
        print("Persisted matches:", len(root["matches"]))
        return

    stamp = utc_now()
    root["generatedAt"] = stamp
    root["lastUpdate"] = stamp
    OUT.write_text(json.dumps(root, ensure_ascii=False, indent=2), encoding="utf-8")
    print("live-results.json updated")
    print("Data changed at:", stamp)
    print("Total persisted matches:", len(root["matches"]))
    print("Standings cached:", sorted(root["standings"].keys()))


if __name__ == "__main__":
    main()
