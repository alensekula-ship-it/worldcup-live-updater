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
# All competitions listed by football-data.org as Free Tier coverage.
COMPETITIONS = ["CL", "PL", "ELC", "PD", "BL1", "SA", "FL1", "DED", "PPL", "BSA", "WC", "EC"]
# Standings are useful for current league competitions and Champions League.
STANDINGS_CODES = {"CL", "PL", "ELC", "PD", "BL1", "SA", "FL1", "DED", "PPL", "BSA"}
# Free tier is 10 requests/minute. Eight seconds keeps the workflow safely below it.
REQUEST_SPACING_SECONDS = 8
HEADERS = {"X-Auth-Token": API_TOKEN}


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def load_previous():
    if not OUT.exists():
        return {}
    try:
        return json.loads(OUT.read_text(encoding="utf-8"))
    except Exception:
        return {}


def api_get(path):
    url = BASE + path
    response = requests.get(url, headers=HEADERS, timeout=30)
    print(path, response.status_code)
    if response.status_code == 200:
        return response.json()
    # A single unavailable/restricted competition must never erase older data.
    print(response.text[:300])
    return None


def team_name(team):
    if not isinstance(team, dict):
        return ""
    return (team.get("name") or team.get("shortName") or team.get("tla") or "").strip()


def match_key(match):
    mid = match.get("id")
    if mid is not None:
        return "id:" + str(mid)
    comp = match.get("competition") or {}
    code = comp.get("code") or match.get("competitionCode") or ""
    date = match.get("utcDate") or match.get("date") or ""
    return "|".join([
        str(code).upper(), str(date),
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
        merged[key] = dict(row)
    for row in incoming or []:
        if not isinstance(row, dict):
            continue
        key = match_key(row)
        old = merged.get(key, {})
        combined = dict(old)
        combined.update(row)
        if key not in merged:
            order.append(key)
        merged[key] = combined
    return [merged[key] for key in order]


def status_of(match):
    return str(match.get("status") or "").upper()


def rebuild_derived(root):
    matches = root.get("matches") or []
    root["live"] = [m for m in matches if status_of(m) in {"LIVE", "IN_PLAY", "PAUSED"}]
    root["finished"] = [m for m in matches if status_of(m) == "FINISHED"]
    root["upcoming"] = [m for m in matches if status_of(m) in {"TIMED", "SCHEDULED"}]


def main():
    root = load_previous()
    root.setdefault("source", "football-data.org")
    root.setdefault("matches", [])
    root.setdefault("standings", {})
    root.setdefault("sources", {})

    all_matches = list(root.get("matches") or [])
    standings = dict(root.get("standings") or {})
    sources = dict(root.get("sources") or {})
    successes = 0

    request_number = 0
    for code in COMPETITIONS:
        if request_number:
            time.sleep(REQUEST_SPACING_SECONDS)
        match_data = api_get(f"/competitions/{code}/matches")
        request_number += 1

        if match_data is not None:
            incoming = match_data.get("matches") or []
            all_matches = merge_matches(all_matches, incoming)
            sources[code] = {
                "status": "connected",
                "matchCount": len(incoming),
                "updatedAt": utc_now(),
            }
            successes += 1
        else:
            previous = dict(sources.get(code) or {})
            previous["status"] = previous.get("status") or "temporarily_unavailable"
            sources[code] = previous

        if code in STANDINGS_CODES:
            time.sleep(REQUEST_SPACING_SECONDS)
            standing_data = api_get(f"/competitions/{code}/standings")
            request_number += 1
            if standing_data is not None and standing_data.get("standings"):
                standings[code] = standing_data
                successes += 1

    if successes == 0:
        raise RuntimeError("No football-data.org request succeeded; keeping previous live-results.json unchanged")

    root["matches"] = all_matches
    root["standings"] = standings
    root["sources"] = sources
    root["generatedAt"] = utc_now()
    root["lastUpdate"] = root["generatedAt"]
    root["competitionCodes"] = COMPETITIONS
    rebuild_derived(root)

    OUT.write_text(json.dumps(root, ensure_ascii=False, indent=2), encoding="utf-8")
    print("live-results.json updated")
    print("Generated at:", root["generatedAt"])
    print("Total persisted matches:", len(root["matches"]))
    print("Standings cached:", sorted(root["standings"].keys()))


if __name__ == "__main__":
    main()
