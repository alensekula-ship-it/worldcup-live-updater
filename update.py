import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

API_BASE = "https://api.football-data.org/v4"
COMPETITION_CODE = os.getenv("COMPETITION_CODE", "WC").strip().upper() or "WC"
PROFILE_COMPETITIONS = [
    code.strip().upper()
    for code in os.getenv(
        "PROFILE_COMPETITIONS",
        "WC,PL,PD,BL1,SA,FL1,DED,PPL,CL,ELC",
    ).split(",")
    if code.strip()
]
OUTPUT_FILE = Path(os.getenv("OUTPUT_FILE", "live-results.json"))
PROFILE_BATCH = max(0, min(48, int(os.getenv("PROFILE_BATCH", "7"))))
INITIAL_PROFILE_BATCH = max(0, min(48, int(os.getenv("INITIAL_PROFILE_BATCH", "48"))))
TIMEOUT_SECONDS = 30
MAX_REQUESTS_PER_MINUTE = 9
_REQUEST_TIMES: list[float] = []


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def wait_for_request_slot() -> None:
    now = time.monotonic()

    while _REQUEST_TIMES and now - _REQUEST_TIMES[0] >= 60:
        _REQUEST_TIMES.pop(0)

    if len(_REQUEST_TIMES) >= MAX_REQUESTS_PER_MINUTE:
        sleep_for = max(1.0, 61.0 - (now - _REQUEST_TIMES[0]))
        print(f"Rate-limit safety pause: {sleep_for:.1f}s")
        time.sleep(sleep_for)

        now = time.monotonic()

        while _REQUEST_TIMES and now - _REQUEST_TIMES[0] >= 60:
            _REQUEST_TIMES.pop(0)

    _REQUEST_TIMES.append(time.monotonic())


def fetch_json(path: str, headers: dict[str, str]) -> dict[str, Any]:
    wait_for_request_slot()

    url = f"{API_BASE}{path}"
    response = requests.get(
        url,
        headers=headers,
        timeout=TIMEOUT_SECONDS,
    )

    print(f"GET {path}: {response.status_code}")

    if response.status_code != 200:
        raise RuntimeError(
            f"football-data.org error for {path}: "
            f"{response.status_code} {response.text[:500]}"
        )

    payload = response.json()

    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected response type for {path}")

    return payload


def load_previous() -> dict[str, Any]:
    if not OUTPUT_FILE.exists():
        return {}

    try:
        with OUTPUT_FILE.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)

        return payload if isinstance(payload, dict) else {}

    except (OSError, json.JSONDecodeError):
        return {}


def team_id(team: dict[str, Any]) -> int:
    value = team.get("id", -1)

    try:
        return int(value)

    except (TypeError, ValueError):
        return -1


def merge_team(
    base: dict[str, Any],
    detail: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(base)

    for key, value in detail.items():
        if value not in (None, "", [], {}):
            merged[key] = value

    return merged


def profile_sort_key(
    team: dict[str, Any],
) -> tuple[int, str, int]:
    has_squad = bool(team.get("squad"))
    fetched = str(team.get("_profileFetchedAt", ""))

    return (
        1 if has_squad else 0,
        fetched,
        team_id(team),
    )


def sync_team_profiles(
    team_list: list[dict[str, Any]],
    previous_teams: list[dict[str, Any]],
    headers: dict[str, str],
    profile_competition: str,
) -> tuple[list[dict[str, Any]], int, int]:
    merged_by_id = {
        team_id(team): dict(team)
        for team in previous_teams
        if isinstance(team, dict) and team_id(team) > 0
    }

    target_ids: set[int] = set()

    for basic in team_list:
        if not isinstance(basic, dict):
            continue

        identifier = team_id(basic)

        if identifier <= 0:
            continue

        target_ids.add(identifier)

        tagged = dict(basic)
        tagged["_profileCompetitionCode"] = profile_competition

        merged_by_id[identifier] = merge_team(
            merged_by_id.get(identifier, {}),
            tagged,
        )

    candidates = sorted(
        [
            merged_by_id[identifier]
            for identifier in target_ids
        ],
        key=profile_sort_key,
    )

    missing_profiles = sum(
        1
        for team in candidates
        if not team.get("squad")
    )

    batch_limit = (
        INITIAL_PROFILE_BATCH
        if missing_profiles
        else PROFILE_BATCH
    )

    refreshed = 0

    for candidate in candidates:
        if refreshed >= batch_limit:
            break

        identifier = team_id(candidate)

        if identifier <= 0:
            continue

        try:
            detail = fetch_json(
                f"/teams/{identifier}",
                headers,
            )

        except RuntimeError as error:
            print(
                f"Profile refresh stopped for team "
                f"{identifier}: {error}"
            )
            break

        detail["_profileFetchedAt"] = utc_stamp()
        detail["_profileCompetitionCode"] = profile_competition

        merged_by_id[identifier] = merge_team(
            candidate,
            detail,
        )

        refreshed += 1

    profiles = sorted(
        merged_by_id.values(),
        key=lambda team: (
            (
                str(team.get("area", {}).get("name", ""))
                if isinstance(team.get("area"), dict)
                else ""
            ),
            str(
                team.get("shortName")
                or team.get("name")
                or ""
            ),
        ),
    )

    target_complete = sum(
        1
        for identifier in target_ids
        if merged_by_id.get(identifier, {}).get("squad")
    )

    print(
        f"{profile_competition} profiles: "
        f"{target_complete}/{len(target_ids)} "
        f"with squad; refreshed {refreshed} "
        f"(batch {batch_limit})"
    )

    return profiles, target_complete, len(target_ids)


def classify_matches(
    matches: list[dict[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    live: list[dict[str, Any]] = []
    finished: list[dict[str, Any]] = []
    upcoming: list[dict[str, Any]] = []

    for match in matches:
        if not isinstance(match, dict):
            continue

        status = str(match.get("status", "")).upper()

        if status in {
            "LIVE",
            "IN_PLAY",
            "PAUSED",
        }:
            live.append(match)

        elif status == "FINISHED":
            finished.append(match)

        elif status in {
            "TIMED",
            "SCHEDULED",
        }:
            upcoming.append(match)

    return live, finished, upcoming


def main() -> None:
    api_token = os.getenv("API_TOKEN")

    if not api_token:
        raise RuntimeError("API_TOKEN is missing")

    headers = {
        "X-Auth-Token": api_token,
    }

    previous = load_previous()

    match_data = fetch_json(
        f"/competitions/{COMPETITION_CODE}/matches",
        headers,
    )

    matches = match_data.get("matches", [])

    if not isinstance(matches, list):
        matches = []

    previous_teams = previous.get("teams", [])

    if not isinstance(previous_teams, list):
        previous_teams = []

    previous_sync = previous.get("profileSync", {})

    if not isinstance(previous_sync, dict):
        previous_sync = {}

    profile_codes = (
        PROFILE_COMPETITIONS
        or [COMPETITION_CODE]
    )

    profile_index = (
        int(previous_sync.get("nextCompetitionIndex", 0))
        % len(profile_codes)
    )

    profile_competition = profile_codes[profile_index]

    next_profile_index = (
        profile_index + 1
    ) % len(profile_codes)

    target_complete = 0
    target_count = 0

    try:
        teams_data = fetch_json(
            f"/competitions/{profile_competition}/teams",
            headers,
        )

        team_list = teams_data.get("teams", [])

        if not isinstance(team_list, list):
            team_list = []

        teams, target_complete, target_count = (
            sync_team_profiles(
                team_list,
                previous_teams,
                headers,
                profile_competition,
            )
        )

    except RuntimeError as error:
        print(
            f"{profile_competition} team refresh failed; "
            f"preserving cached profiles: {error}"
        )

        teams = previous_teams

    live, finished, upcoming = classify_matches(
        matches
    )

    complete_profiles = sum(
        1
        for team in teams
        if isinstance(team, dict)
        and team.get("squad")
    )

    output = {
        "source": "football-data.org",
        "generatedAt": utc_stamp(),
        "lastUpdate": utc_stamp(),
        "competition": match_data.get(
            "competition",
            {},
        ),
        "filters": match_data.get(
            "filters",
            {},
        ),
        "resultSet": match_data.get(
            "resultSet",
            {},
        ),
        "matches": matches,
        "live": live,
        "finished": finished,
        "upcoming": upcoming,
        "teams": teams,
        "profileSync": {
            "matchCompetitionCode": COMPETITION_CODE,
            "profileCompetitionProcessed": (
                profile_competition
            ),
            "profileCompetitionTeamCount": (
                target_count
            ),
            "profileCompetitionSquads": (
                target_complete
            ),
            "profileCompetitionCodes": (
                profile_codes
            ),
            "nextCompetitionIndex": (
                next_profile_index
            ),
            "teamCount": len(teams),
            "profilesWithSquad": (
                complete_profiles
            ),
            "profilesPending": max(
                0,
                len(teams) - complete_profiles,
            ),
            "regularBatchLimit": PROFILE_BATCH,
            "initialBatchLimit": (
                INITIAL_PROFILE_BATCH
            ),
        },
    }

    with OUTPUT_FILE.open(
        "w",
        encoding="utf-8",
    ) as handle:
        json.dump(
            output,
            handle,
            ensure_ascii=False,
            indent=2,
        )

    print(f"{OUTPUT_FILE} updated")
    print("Generated at:", output["generatedAt"])
    print("Matches:", len(matches))
    print("Live:", len(live))
    print("Finished:", len(finished))
    print("Upcoming:", len(upcoming))
    print(
        "Profile competition:",
        profile_competition,
    )
    print("Teams cached:", len(teams))
    print(
        "Squads cached:",
        complete_profiles,
    )


if __name__ == "__main__":
    main()
