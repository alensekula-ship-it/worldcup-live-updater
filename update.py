import copy
import hashlib
import json
import os
import time
from datetime import datetime, timezone, timedelta
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
ESPN_UCL_QUALIFIERS = "https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.champions_qual/scoreboard"


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



def espn_get_ucl_qualifiers():
    """Fetch the official ESPN scoreboard feed dedicated to UCL qualifying.

    football-data.org exposes UEFA Champions League on the free plan, but its
    current CL resource may omit the separate qualifying competition.  The
    dedicated ESPN qualifier feed fills only that gap and is normalized into
    the exact same central match schema before Android ever sees the payload.
    """
    now = datetime.now(timezone.utc)
    date_from = (now - timedelta(days=35)).strftime("%Y%m%d")
    date_to = (now + timedelta(days=55)).strftime("%Y%m%d")
    params = {"dates": f"{date_from}-{date_to}", "limit": 1000}
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = SESSION.get(ESPN_UCL_QUALIFIERS, params=params, timeout=(10, 25))
            print("ESPN UCL qualifiers", response.status_code, f"attempt {attempt}/{MAX_ATTEMPTS}")
            if response.status_code == 200:
                return response.json()
            last_error = RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt < MAX_ATTEMPTS:
                    time.sleep(min(12, 3 * attempt))
                    continue
            break
        except requests.RequestException as exc:
            last_error = exc
            if attempt < MAX_ATTEMPTS:
                time.sleep(min(12, 3 * attempt))
                continue
    print("ESPN qualifier request failed:", str(last_error)[:300])
    return None


def espn_team(competitor):
    team = competitor.get("team") or {}
    name = (team.get("displayName") or team.get("shortDisplayName") or team.get("name") or competitor.get("displayName") or "").strip()
    short_name = (team.get("shortDisplayName") or name).strip()
    tla = (team.get("abbreviation") or "").strip()
    crest = ""
    for logo in team.get("logos") or []:
        href = (logo.get("href") or "").strip()
        if href.startswith("//"):
            href = "https:" + href
        if href.startswith("https://"):
            crest = href
            lower = href.lower()
            if any(x in lower for x in (".png", ".jpg", ".jpeg", ".webp", "format=png")):
                break
    if not crest:
        team_id = str(team.get("id") or "").strip()
        if team_id:
            crest = f"https://a.espncdn.com/i/teamlogos/soccer/500/{team_id}.png"
    return name, {"name": name, "shortName": short_name, "tla": tla, "crest": crest}


def qualifier_stage(event, competition):
    chunks = []
    def collect(obj):
        if not isinstance(obj, dict):
            return
        for key in ("name", "shortName", "displayName", "description", "headline", "slug", "abbreviation"):
            value = str(obj.get(key) or "").strip()
            if value:
                chunks.append(value)
        for key in ("type", "round", "week", "phase", "stage", "season"):
            child = obj.get(key)
            if isinstance(child, dict):
                collect(child)
                number = child.get("number")
                if isinstance(number, int) and number > 0:
                    chunks.append(f"round {number}")
    collect(event)
    collect(competition)
    notes = []
    for note in (competition or {}).get("notes") or []:
        if isinstance(note, dict):
            headline = str(note.get("headline") or "").strip()
            if headline:
                notes.append(headline)
                chunks.append(headline)
    text = " ".join(chunks).lower()
    if any(v in text for v in ("playoff", "play-off", "play off")):
        stage = "PLAYOFFS"
    elif any(v in text for v in ("third qualifying", "third qualification", "3rd qualifying", "qualifying round 3", "round 3")):
        stage = "QUALIFICATION_ROUND_3"
    elif any(v in text for v in ("second qualifying", "second qualification", "2nd qualifying", "qualifying round 2", "round 2")):
        stage = "QUALIFICATION_ROUND_2"
    elif any(v in text for v in ("first qualifying", "first qualification", "1st qualifying", "qualifying round 1", "round 1")):
        stage = "QUALIFICATION_ROUND_1"
    else:
        stage = "QUALIFICATION"
    stage_label = {
        "QUALIFICATION_ROUND_1": "1. pretkolo",
        "QUALIFICATION_ROUND_2": "2. pretkolo",
        "QUALIFICATION_ROUND_3": "3. pretkolo",
        "PLAYOFFS": "Play-off",
        "QUALIFICATION": "Kvalifikacije",
    }.get(stage, "Kvalifikacije")
    notes_text = " ".join(notes).lower()
    if any(v in notes_text for v in ("2nd leg", "second leg", "leg 2")):
        leg = "Uzvrat"
    elif any(v in notes_text for v in ("1st leg", "first leg", "leg 1")):
        leg = "Prva utakmica"
    else:
        leg = ""
    round_name = stage_label + (" • " + leg if leg else "")
    return stage, round_name


def espn_status(event):
    typ = ((event.get("status") or {}).get("type") or {})
    state = str(typ.get("state") or "").lower()
    completed = bool(typ.get("completed"))
    if completed:
        return "FINISHED"
    if state == "in":
        return "IN_PLAY"
    return "SCHEDULED"


def espn_score(competitor):
    raw = competitor.get("score")
    if isinstance(raw, dict):
        raw = raw.get("value") or raw.get("displayValue")
    try:
        return int(float(str(raw)))
    except Exception:
        return None


def normalize_espn_ucl(scoreboard):
    rows = []
    for event in (scoreboard or {}).get("events") or []:
        if not isinstance(event, dict):
            continue
        competitions = event.get("competitions") or []
        competition = competitions[0] if competitions and isinstance(competitions[0], dict) else {}
        competitors = competition.get("competitors") or []
        home = next((c for c in competitors if isinstance(c, dict) and str(c.get("homeAway") or "").lower() == "home"), None)
        away = next((c for c in competitors if isinstance(c, dict) and str(c.get("homeAway") or "").lower() == "away"), None)
        if home is None or away is None:
            continue
        home_name, home_team = espn_team(home)
        away_name, away_team = espn_team(away)
        utc_date = str(event.get("date") or competition.get("date") or "").strip()
        if not home_name or not away_name or not utc_date:
            continue
        stage, round_name = qualifier_stage(event, competition)
        event_id = str(event.get("id") or "").strip()
        try:
            numeric_id = 1500000000 + (int(event_id) % 400000000)
        except Exception:
            digest = hashlib.sha256((event_id + home_name + away_name + utc_date).encode("utf-8")).hexdigest()
            numeric_id = 1500000000 + (int(digest[:12], 16) % 400000000)
        status = espn_status(event)
        row = {
            "id": numeric_id,
            "externalId": f"espn-ucl-{event_id}" if event_id else "",
            "competition": {"name": "UEFA Champions League", "code": "CL", "type": "CUP"},
            "competitionCode": "CL",
            "competitionName": "UEFA Champions League",
            "utcDate": utc_date,
            "date": utc_date,
            "status": status,
            "stage": stage,
            "roundName": round_name,
            "group": "",
            "homeTeam": home_team,
            "awayTeam": away_team,
            "home": home_name,
            "away": away_name,
            "dataProvider": "ESPN UEFA Champions League Qualifying",
        }
        venue = competition.get("venue") or {}
        if isinstance(venue, dict) and venue.get("fullName"):
            row["venue"] = venue.get("fullName")
        hg, ag = espn_score(home), espn_score(away)
        if hg is not None and ag is not None and status in {"FINISHED", "IN_PLAY"}:
            row["homeGoals"] = hg
            row["awayGoals"] = ag
            row["score"] = {"fullTime": {"home": hg, "away": ag}}
        rows.append(row)
    rows.sort(key=stable_match_sort_key)
    return rows

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
            print(f"{code}: {len(incoming)} football-data matches")
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

    # Champions League qualifying is a separate competition in provider coverage.
    # Merge its dedicated ESPN scoreboard into the same CL namespace so Android
    # remains single-source and never needs a second network provider itself.
    espn_payload = espn_get_ucl_qualifiers()
    if espn_payload is not None:
        qualifier_rows = normalize_espn_ucl(espn_payload)
        print("CL qualifiers:", len(qualifier_rows), "ESPN matches")
        if qualifier_rows:
            old_cl = [x for x in all_matches if competition_code(x) == "CL"]
            non_cl = [x for x in all_matches if competition_code(x) != "CL"]
            merged_cl = merge_matches(old_cl, qualifier_rows)
            merged_cl.sort(key=stable_match_sort_key)
            all_matches = non_cl + merged_cl
            source = copy.deepcopy(sources.get("CL") or {})
            source["qualifyingProvider"] = "ESPN UEFA Champions League Qualifying"
            source["qualifyingMatchCount"] = len(qualifier_rows)
            source["status"] = source.get("status") or "connected"
            if canonical(old_cl) != canonical(merged_cl):
                source["lastSuccessfulUpdate"] = utc_now()
            sources["CL"] = source

    if successful_requests == 0 and espn_payload is None:
        raise RuntimeError("No football provider request succeeded; live-results.json left unchanged")

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
