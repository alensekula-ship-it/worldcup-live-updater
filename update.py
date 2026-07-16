import copy
import html as html_lib
import json
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

API_TOKEN = os.getenv("API_TOKEN")
if not API_TOKEN:
    raise RuntimeError("API_TOKEN is missing")

BASE_URL = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": API_TOKEN}
PUBLIC_HEADERS = {
    # Use a normal browser identity because some official public pages reject
    # bot-like user agents even though the same pages are publicly accessible.
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "hr-HR,hr;q=0.9,en-US;q=0.7,en;q=0.6",
    "Cache-Control": "no-cache",
}
OUTPUT_FILE = Path("live-results.json")

PUBLIC_SESSION = requests.Session()
PUBLIC_SESSION.headers.update(PUBLIC_HEADERS)
PUBLIC_SESSION.mount(
    "https://",
    HTTPAdapter(max_retries=Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )),
)

# Competitions available in the user's football-data.org package.
CLUB_CODES = ["CL", "BL1", "DED", "BSA", "PD", "FL1", "ELC", "PPL", "SA", "PL"]
NATIONAL_CODES = ["WC", "EC"]
FOOTBALL_DATA_CODES = NATIONAL_CODES + CLUB_CODES
ALL_CODES = ["HNL"] + FOOTBALL_DATA_CODES
LIVE_STATUSES = {"LIVE", "IN_PLAY", "PAUSED", "EXTRA_TIME", "PENALTY_SHOOTOUT"}
FINISHED_STATUSES = {"FINISHED", "AWARDED"}
UPCOMING_STATUSES = {"TIMED", "SCHEDULED"}

# Official public SuperSport HNL / HNS pages. HNS Semafor is the primary
# source because it exposes the full 2026/27 schedule and table on one page.
# hnl.hr remains a verified fallback in case Semafor is temporarily unavailable.
HNL_SEMAFOR_URL = "https://semafor.hns.family/natjecanja/114137140/supersport-hrvatska-nogometna-liga-20262027/"
HNL_RESULTS_URL = "https://hnl.hr/statistika/rezultati/"
HNL_STANDINGS_URL = "https://hnl.hr/statistika/ljestvica/"
HNL_TIMEZONE = ZoneInfo("Europe/Zagreb")
HNL_MIN_MATCHES = 10
HNL_EXPECTED_TEAMS = 10
HNL_SEASON_ID = 202627
HNL_SEASON_START = "2026-08-01"
HNL_SEASON_END = "2027-05-22"

HNL_TEAM_ALIASES = {
    "Dinamo": "Dinamo Zagreb",
    "GNK Dinamo": "Dinamo Zagreb",
    "GNK Dinamo Zagreb": "Dinamo Zagreb",
    "Hajduk": "Hajduk Split",
    "HNK Hajduk": "Hajduk Split",
    "HNK Hajduk Split": "Hajduk Split",
    "Gorica": "Gorica",
    "HNK Gorica": "Gorica",
    "HNK Gorica s.d.d.": "Gorica",
    "HNK Gorica sdd": "Gorica",
    "Istra": "Istra 1961",
    "Istra 1961": "Istra 1961",
    "NK Istra 1961": "Istra 1961",
    "Lokomotiva": "Lokomotiva Zagreb",
    "Lokomotiva Zagreb": "Lokomotiva Zagreb",
    "NK Lokomotiva": "Lokomotiva Zagreb",
    "NK Lokomotiva (Z)": "Lokomotiva Zagreb",
    "Osijek": "Osijek",
    "NK Osijek": "Osijek",
    "Rijeka": "Rijeka",
    "HNK Rijeka": "Rijeka",
    "Rudeš": "Rudeš",
    "Rudes": "Rudeš",
    "NK Rudeš": "Rudeš",
    "NK Rudes": "Rudeš",
    "Slaven Belupo": "Slaven Belupo",
    "NK Slaven Belupo": "Slaven Belupo",
    "Varaždin": "Varaždin",
    "Varazdin": "Varaždin",
    "NK Varaždin": "Varaždin",
    "NK Varazdin": "Varaždin",
}
HNL_SITE_TEAM_NAMES = sorted(HNL_TEAM_ALIASES.keys(), key=len, reverse=True)
HNL_TEAM_PATTERN = "(?:" + "|".join(re.escape(name) for name in HNL_SITE_TEAM_NAMES) + ")"

# Standings change far less often than live scores. Refresh only two stale
# football-data.org competitions per run so the updater stays safely inside
# the free-plan 10 requests/minute limit even when match details are needed.
STANDINGS_BATCH_SIZE = 2
STANDINGS_MAX_AGE = timedelta(hours=6)

# football-data.org limits the free live-score package to a small number of
# requests per minute and the generic /matches endpoint to date periods of at
# most 10 days. The updater therefore spaces requests automatically and uses
# competition season endpoints only when a league has no future fixtures in
# the current cache.
API_MIN_INTERVAL_SECONDS = 6.2
SCHEDULE_HORIZON_DAYS = 150
SCHEDULE_CACHE_PAST_DAYS = 30
SCHEDULE_RECHECK_AFTER = timedelta(hours=24)
_API_LAST_REQUEST_AT = 0.0


def api_get(path: str, params: dict | None = None) -> dict:
    global _API_LAST_REQUEST_AT

    # Keep every football-data.org request far enough apart to stay below the
    # free-plan request ceiling. This also makes a first full season bootstrap
    # reliable instead of intermittently failing with HTTP 429.
    elapsed = time.monotonic() - _API_LAST_REQUEST_AT
    if _API_LAST_REQUEST_AT > 0 and elapsed < API_MIN_INTERVAL_SECONDS:
        time.sleep(API_MIN_INTERVAL_SECONDS - elapsed)

    response = requests.get(f"{BASE_URL}{path}", headers=HEADERS, params=params, timeout=40)
    _API_LAST_REQUEST_AT = time.monotonic()

    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        try:
            wait_seconds = max(65, int(retry_after or "65"))
        except ValueError:
            wait_seconds = 65
        print(f"football-data.org rate limit reached; retrying in {wait_seconds}s")
        time.sleep(wait_seconds)
        response = requests.get(f"{BASE_URL}{path}", headers=HEADERS, params=params, timeout=40)
        _API_LAST_REQUEST_AT = time.monotonic()

    if response.status_code != 200:
        raise RuntimeError(f"football-data.org error {response.status_code}: {response.text[:500]}")
    return response.json()


def public_get(url: str) -> str:
    response = PUBLIC_SESSION.get(url, timeout=45, allow_redirects=True)
    if response.status_code != 200:
        raise RuntimeError(f"Public source error {response.status_code}: {url}")
    if len(response.text or "") < 1000:
        raise RuntimeError(f"Public source returned an unexpectedly short page: {url}")
    return response.text


def competition_code(match: dict) -> str:
    competition = match.get("competition") or {}
    if isinstance(competition, dict):
        return str(competition.get("code") or competition.get("name") or "").upper()
    return str(match.get("competitionName") or competition or "").upper()


def merge_matches(*collections: list[dict]) -> list[dict]:
    by_id: dict[int | str, dict] = {}
    for collection in collections:
        for match in collection or []:
            key = match.get("id") or (
                f"{match.get('utcDate') or match.get('date')}:"
                f"{match.get('homeTeam', {}).get('name')}:{match.get('awayTeam', {}).get('name')}"
            )
            by_id[key] = match
    return sorted(by_id.values(), key=lambda m: m.get("utcDate") or m.get("date") or "")


def match_datetime_utc(match: dict) -> datetime | None:
    value = match.get("utcDate") or match.get("date")
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def existing_matches_for_code(existing: dict | None, code: str) -> list[dict]:
    if not existing:
        return []
    return [m for m in existing.get("matches") or [] if competition_code(m) == code]


def preserve_cached_football_matches(existing: dict | None, now: datetime) -> list[dict]:
    if not existing:
        return []
    lower = now - timedelta(days=SCHEDULE_CACHE_PAST_DAYS)
    upper = now + timedelta(days=SCHEDULE_HORIZON_DAYS)
    kept: list[dict] = []
    for match in existing.get("matches") or []:
        code = competition_code(match)
        if code in {"HNL", "WC"}:
            continue
        kickoff = match_datetime_utc(match)
        if kickoff is not None and lower <= kickoff <= upper:
            kept.append(match)
    return kept


def has_future_fixture(matches: list[dict], code: str, now: datetime) -> bool:
    for match in matches:
        if competition_code(match) != code:
            continue
        kickoff = match_datetime_utc(match)
        status = str(match.get("status") or "").upper()
        if kickoff is not None and kickoff >= now - timedelta(hours=2) and status not in FINISHED_STATUSES:
            return True
    return False


def competition_season_start_year(code: str, now: datetime) -> int:
    # Brazil plays a calendar-year season. The supported European competitions
    # use the year in which the season begins (for example 2026 for 2026/27).
    if code == "BSA":
        return now.year
    return now.year if now.month >= 6 else now.year - 1


def schedule_checks_from_existing(existing: dict | None) -> dict:
    sources = (existing or {}).get("sources") or {}
    football_source = sources.get("football-data.org") or {}
    checks = football_source.get("scheduleChecks") or {}
    return copy.deepcopy(checks) if isinstance(checks, dict) else {}


def schedule_check_is_fresh(checks: dict, code: str, now: datetime) -> bool:
    entry = checks.get(code) or {}
    checked_at = parse_utc(entry.get("checkedAt"))
    return checked_at is not None and now - checked_at < SCHEDULE_RECHECK_AFTER


def trim_schedule_matches(matches: list[dict], now: datetime) -> list[dict]:
    lower = now - timedelta(days=SCHEDULE_CACHE_PAST_DAYS)
    upper = now + timedelta(days=SCHEDULE_HORIZON_DAYS)
    trimmed: list[dict] = []
    for match in matches or []:
        kickoff = match_datetime_utc(match)
        if kickoff is not None and lower <= kickoff <= upper:
            trimmed.append(match)
    return trimmed


def fetch_nearby_matches(now: datetime) -> list[dict]:
    # Two legal 10-day windows: recent/today and the following ten days.
    # dateTo is exclusive in football-data.org v4.
    windows = [
        (now - timedelta(days=9), now + timedelta(days=1)),
        (now + timedelta(days=1), now + timedelta(days=11)),
    ]
    codes = ",".join(code for code in FOOTBALL_DATA_CODES if code != "WC")
    collected: list[dict] = []
    for start, end in windows:
        try:
            payload = api_get(
                "/matches",
                params={
                    "competitions": codes,
                    "dateFrom": start.date().isoformat(),
                    "dateTo": end.date().isoformat(),
                },
            )
            collected.extend(payload.get("matches") or [])
        except Exception as exc:
            print(f"Nearby match window failed ({start.date()} to {end.date()}): {exc}")
    return merge_matches(collected)


def fetch_missing_competition_schedules(
    base_matches: list[dict], existing: dict | None, now: datetime
) -> tuple[list[dict], dict]:
    checks = schedule_checks_from_existing(existing)
    collected: list[dict] = []

    for code in CLUB_CODES:
        if has_future_fixture(base_matches + collected, code, now):
            continue
        if schedule_check_is_fresh(checks, code, now):
            print(f"{code}: no future fixture cached; schedule was checked less than 24h ago")
            continue

        season = competition_season_start_year(code, now)
        try:
            payload = api_get(f"/competitions/{code}/matches", params={"season": season})
            season_matches = trim_schedule_matches(payload.get("matches") or [], now)
            collected.extend(season_matches)
            checks[code] = {
                "checkedAt": now.isoformat().replace("+00:00", "Z"),
                "season": season,
                "matchesInHorizon": len(season_matches),
                "status": "available" if season_matches else "not_published",
            }
            print(f"{code} season {season}: {len(season_matches)} matches inside the app horizon")
        except Exception as exc:
            checks[code] = {
                "checkedAt": now.isoformat().replace("+00:00", "Z"),
                "season": season,
                "matchesInHorizon": 0,
                "status": "error",
                "error": str(exc)[:180],
            }
            print(f"{code} season schedule fetch failed: {exc}")

    return merge_matches(collected), checks


def should_fetch_details(match: dict, now: datetime) -> bool:
    # HNL is read from its own official public source and has no football-data.org ID.
    if competition_code(match) == "HNL":
        return False

    status = str(match.get("status") or "").upper()
    if status in LIVE_STATUSES:
        return True

    utc_date = match.get("utcDate")
    if not utc_date:
        return False
    try:
        kickoff = datetime.fromisoformat(utc_date.replace("Z", "+00:00"))
    except ValueError:
        return False

    if status in FINISHED_STATUSES and kickoff.date() == now.date():
        return True
    if status in UPCOMING_STATUSES and timedelta(hours=-1) <= kickoff - now <= timedelta(hours=4):
        return True
    return False


def enrich_with_match_details(matches: list[dict], now: datetime) -> list[dict]:
    enriched: list[dict] = []
    detailed_requests = 0

    for match in matches:
        item = match
        match_id = match.get("id")
        if isinstance(match_id, int) and should_fetch_details(match, now) and detailed_requests < 5:
            try:
                item = api_get(f"/matches/{match_id}")
                detailed_requests += 1
            except Exception as exc:
                print(f"Detail fetch failed for match {match_id}: {exc}")
        enriched.append(item)

    print(f"Detailed match requests: {detailed_requests}")
    return enriched


def without_timestamps(payload: dict) -> dict:
    cleaned = copy.deepcopy(payload)
    cleaned.pop("generatedAt", None)
    cleaned.pop("lastUpdate", None)
    return cleaned


def load_existing() -> dict | None:
    if not OUTPUT_FILE.exists():
        return None
    try:
        return json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def standings_age(entry: dict | None, now: datetime) -> timedelta:
    if not isinstance(entry, dict):
        return timedelta.max
    stamp = parse_utc(entry.get("updatedAt"))
    if stamp is None:
        return timedelta.max
    return now - stamp


def refresh_football_data_standings(existing: dict | None, now: datetime) -> dict:
    standings: dict = copy.deepcopy((existing or {}).get("standings") or {})

    ordered = sorted(
        FOOTBALL_DATA_CODES,
        key=lambda code: standings_age(standings.get(code), now),
        reverse=True,
    )
    candidates = [
        code for code in ordered
        if standings_age(standings.get(code), now) >= STANDINGS_MAX_AGE
    ]

    refreshed = 0
    for code in candidates[:STANDINGS_BATCH_SIZE]:
        try:
            payload = api_get(f"/competitions/{code}/standings")
            tables = payload.get("standings") or []
            if tables:
                standings[code] = {
                    "updatedAt": now.isoformat().replace("+00:00", "Z"),
                    "competition": payload.get("competition") or {},
                    "season": payload.get("season") or {},
                    "standings": tables,
                }
                refreshed += 1
                print(f"Standings refreshed: {code}")
            else:
                print(f"Standings unavailable/empty: {code}")
        except Exception as exc:
            # Cups may legitimately return 404. Keep any previous verified table.
            print(f"Standings fetch skipped for {code}: {exc}")

    print(f"Standings requests: {min(len(candidates), STANDINGS_BATCH_SIZE)}; refreshed: {refreshed}")
    return standings


def normalize_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(value or "")).strip()


def canonical_hnl_team(value: str) -> str:
    clean = normalize_spaces(value).strip(" ,-–")
    clean = re.sub(r"\s+s\.d\.d\.?$", " s.d.d.", clean, flags=re.IGNORECASE)
    return HNL_TEAM_ALIASES.get(clean, clean)


def hnl_match_id(matchday: int, home: str, away: str, date_value: str) -> str:
    raw = unicodedata.normalize("NFKD", f"{home}-{away}").encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    return f"HNL-2026-27-{matchday:02d}-{date_value}-{slug}"


def build_hnl_match(
    matchday: int,
    date_raw: str,
    time_raw: str,
    home_raw: str,
    away_raw: str,
    home_score_raw: str | None,
    away_score_raw: str | None,
    venue_raw: str,
    now_utc: datetime,
) -> dict | None:
    home = canonical_hnl_team(home_raw)
    away = canonical_hnl_team(away_raw)
    if home == away or home not in HNL_TEAM_ALIASES.values() or away not in HNL_TEAM_ALIASES.values():
        return None

    try:
        match_date = datetime.strptime(date_raw, "%d.%m.%Y.")
    except ValueError:
        return None

    def score_value(raw: str | None) -> int | None:
        raw = normalize_spaces(raw or "")
        return int(raw) if raw.isdigit() else None

    hg = score_value(home_score_raw)
    ag = score_value(away_score_raw)
    has_score = hg is not None and ag is not None
    date_key = match_date.strftime("%Y-%m-%d")
    venue = normalize_spaces(venue_raw).strip(" ,-–")
    now_local = now_utc.astimezone(HNL_TIMEZONE)

    utc_date = None
    if time_raw:
        try:
            local_dt = datetime.strptime(f"{date_raw} {time_raw}", "%d.%m.%Y. %H:%M").replace(tzinfo=HNL_TIMEZONE)
            utc_date = local_dt.astimezone(timezone.utc)
        except ValueError:
            local_dt = match_date.replace(tzinfo=HNL_TIMEZONE)
        if has_score and local_dt - timedelta(minutes=20) <= now_local <= local_dt + timedelta(hours=3):
            status = "IN_PLAY"
        elif has_score and now_local > local_dt + timedelta(hours=3):
            status = "FINISHED"
        else:
            status = "SCHEDULED"
    else:
        # The official 2026/27 schedule initially publishes dates before exact
        # kickoff times. Never invent a time or a live window.
        status = "FINISHED" if has_score else "SCHEDULED"

    return {
        "id": hnl_match_id(matchday, home, away, date_key),
        "competition": {"id": 1001, "name": "HNL", "code": "HNL", "type": "LEAGUE"},
        "season": {
            "id": HNL_SEASON_ID,
            "startDate": HNL_SEASON_START,
            "endDate": HNL_SEASON_END,
            "currentMatchday": matchday,
        },
        "utcDate": utc_date.isoformat().replace("+00:00", "Z") if utc_date else None,
        "date": None if utc_date else date_key,
        "status": status,
        "matchday": matchday,
        "stage": "REGULAR_SEASON",
        "group": None,
        "homeTeam": {"name": home, "shortName": home},
        "awayTeam": {"name": away, "shortName": away},
        "score": {
            "winner": None,
            "duration": "REGULAR",
            "fullTime": {"home": hg, "away": ag},
            "halfTime": {"home": None, "away": None},
        },
        "venue": venue,
        "dataSource": "HNS Semafor / hnl.hr",
    }


def parse_hnl_semafor_matches(page_html: str, now_utc: datetime) -> list[dict]:
    soup = BeautifulSoup(page_html, "html.parser")
    text = normalize_spaces(soup.get_text(" ", strip=True))
    marker = "2026/2027"
    if marker in text:
        text = text.split(marker, 1)[1]
    if "Ljestvica" in text:
        text = text.split("Ljestvica", 1)[0]

    # Parse one round at a time. This avoids duplicate team-fixture lists that
    # Semafor renders again below the main competition schedule.
    round_matches = list(re.finditer(r"(?P<round>\d{1,2})\.\s*kolo", text, re.IGNORECASE))
    parsed: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    match_pattern = re.compile(
        rf"(?P<date>\d{{2}}\.\d{{2}}\.\d{{4}}\.)(?:\s+(?P<time>\d{{2}}:\d{{2}}))?\s+"
        rf"(?P<home>{HNL_TEAM_PATTERN})\s+"
        rf"(?P<hg>-|\d+)\s*:\s*(?P<ag>-|\d+)\s+"
        rf"(?P<away>{HNL_TEAM_PATTERN})\s+"
        rf"(?P<venue>.*?)"
        rf"(?=(?:\d{{2}}\.\d{{2}}\.\d{{4}}\.|$))",
        re.IGNORECASE,
    )

    for index, round_found in enumerate(round_matches):
        matchday = int(round_found.group("round"))
        segment_start = round_found.end()
        segment_end = round_matches[index + 1].start() if index + 1 < len(round_matches) else len(text)
        segment = text[segment_start:segment_end]
        for found in match_pattern.finditer(segment):
            item = build_hnl_match(
                matchday,
                found.group("date"),
                found.group("time") or "",
                found.group("home"),
                found.group("away"),
                found.group("hg"),
                found.group("ag"),
                found.group("venue"),
                now_utc,
            )
            if item is None:
                continue
            key = (
                item.get("date") or item.get("utcDate") or "",
                item["homeTeam"]["name"],
                item["awayTeam"]["name"],
            )
            if key in seen:
                continue
            seen.add(key)
            parsed.append(item)

    return sorted(
        parsed,
        key=lambda m: (
            m.get("matchday") or 0,
            m.get("utcDate") or m.get("date") or "",
            m["homeTeam"]["name"],
        ),
    )


def parse_hnl_hr_matches(page_html: str, now_utc: datetime) -> list[dict]:
    soup = BeautifulSoup(page_html, "html.parser")
    text = normalize_spaces(soup.get_text(" ", strip=True))
    round_matches = list(re.finditer(r"(?P<round>\d{1,2})\.\s*kolo", text, re.IGNORECASE))
    parsed: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    pattern = re.compile(
        rf"(?P<home>{HNL_TEAM_PATTERN})\s*"
        rf"(?:(?P<hg>\d+)\s*)?:\s*(?:(?P<ag>\d+)\s*)?"
        rf"(?P<away>{HNL_TEAM_PATTERN})\s+"
        rf"(?P<date>\d{{2}}\.\d{{2}}\.\d{{4}}\.)(?:\s+(?P<time>\d{{2}}:\d{{2}}))?\s*,\s*"
        rf"(?P<venue>.*?)"
        rf"(?=(?:\d{{2}}\.\d{{2}}\.\d{{4}}\.|\d{{1,2}}\.\s*kolo|Izvještaj|$))",
        re.IGNORECASE,
    )

    for index, round_found in enumerate(round_matches):
        matchday = int(round_found.group("round"))
        segment_start = round_found.end()
        segment_end = round_matches[index + 1].start() if index + 1 < len(round_matches) else len(text)
        segment = text[segment_start:segment_end]
        for found in pattern.finditer(segment):
            item = build_hnl_match(
                matchday,
                found.group("date"),
                found.group("time") or "",
                found.group("home"),
                found.group("away"),
                found.group("hg"),
                found.group("ag"),
                found.group("venue"),
                now_utc,
            )
            if item is None:
                continue
            key = (
                item.get("date") or item.get("utcDate") or "",
                item["homeTeam"]["name"],
                item["awayTeam"]["name"],
            )
            if key in seen:
                continue
            seen.add(key)
            parsed.append(item)

    return sorted(
        parsed,
        key=lambda m: (
            m.get("matchday") or 0,
            m.get("utcDate") or m.get("date") or "",
            m["homeTeam"]["name"],
        ),
    )


def parse_hnl_matches(page_html: str, now_utc: datetime) -> list[dict]:
    semafor = parse_hnl_semafor_matches(page_html, now_utc)
    if len(semafor) >= HNL_MIN_MATCHES:
        return semafor
    return parse_hnl_hr_matches(page_html, now_utc)


def parse_hnl_standings(page_html: str, now_utc: datetime) -> dict | None:
    soup = BeautifulSoup(page_html, "html.parser")
    text = normalize_spaces(soup.get_text(" ", strip=True))
    if "Ljestvica" in text:
        # Use the first full table after the competition's standings heading.
        text = text.split("Ljestvica", 1)[1]

    pattern = re.compile(
        rf"(?P<position>\d{{1,2}})\.?\s*(?P<team>{HNL_TEAM_PATTERN})\s+"
        rf"(?P<played>\d+)\s+(?P<wins>\d+)\s+(?P<draws>\d+)\s+(?P<losses>\d+)\s+"
        rf"(?P<gf>\d+)\s+(?P<ga>\d+)\s*(?P<gd>[+-]?\d+)\s+(?P<points>\d+)",
        re.IGNORECASE,
    )

    rows: list[dict] = []
    seen_teams: set[str] = set()
    for found in pattern.finditer(text):
        team = canonical_hnl_team(found.group("team"))
        if team in seen_teams or team not in HNL_TEAM_ALIASES.values():
            continue
        seen_teams.add(team)
        position = int(found.group("position"))
        rows.append({
            "position": position,
            "team": {
                "id": 2000 + position,
                "name": team,
                "shortName": team,
                "tla": "",
            },
            "playedGames": int(found.group("played")),
            "form": None,
            "won": int(found.group("wins")),
            "draw": int(found.group("draws")),
            "lost": int(found.group("losses")),
            "points": int(found.group("points")),
            "goalsFor": int(found.group("gf")),
            "goalsAgainst": int(found.group("ga")),
            "goalDifference": int(found.group("gd")),
        })
        if len(rows) == HNL_EXPECTED_TEAMS:
            break

    if len(rows) != HNL_EXPECTED_TEAMS:
        return None

    rows.sort(key=lambda row: row["position"])
    current_matchday = max((row["playedGames"] for row in rows), default=0)
    return {
        "updatedAt": now_utc.isoformat().replace("+00:00", "Z"),
        "competition": {
            "id": 1001,
            "name": "HNL",
            "code": "HNL",
            "type": "LEAGUE",
        },
        "season": {
            "id": HNL_SEASON_ID,
            "startDate": HNL_SEASON_START,
            "endDate": HNL_SEASON_END,
            "currentMatchday": current_matchday,
        },
        "standings": [{
            "stage": "REGULAR_SEASON",
            "type": "TOTAL",
            "group": None,
            "table": rows,
        }],
    }


def payload_without_updated_at(value):
    cleaned = copy.deepcopy(value)
    if isinstance(cleaned, dict):
        cleaned.pop("updatedAt", None)
    return cleaned


def existing_hnl_matches(existing: dict | None) -> list[dict]:
    if not existing:
        return []
    return [
        match
        for match in existing.get("matches") or []
        if competition_code(match) == "HNL"
    ]


def refresh_hnl(
    existing: dict | None,
    standings: dict,
    now: datetime,
) -> tuple[list[dict], dict, dict]:
    old_matches = existing_hnl_matches(existing)
    old_standings = standings.get("HNL")
    old_source = copy.deepcopy(
        ((existing or {}).get("sources") or {}).get("HNL") or {}
    )

    hnl_matches = old_matches
    hnl_standing_entry = old_standings
    source = old_source or {
        "name": "SuperSport HNL / HNS",
        "status": "waiting",
        "matchesUrl": HNL_SEMAFOR_URL,
        "standingsUrl": HNL_SEMAFOR_URL,
    }
    source["lastAttempt"] = now.isoformat().replace("+00:00", "Z")

    matches_ok = False
    standings_ok = False
    matches_changed = False
    standings_changed = False
    errors: list[str] = []
    semafor_html: str | None = None

    try:
        semafor_html = public_get(HNL_SEMAFOR_URL)
        parsed_matches = parse_hnl_semafor_matches(semafor_html, now)
        if len(parsed_matches) >= HNL_MIN_MATCHES:
            matches_changed = parsed_matches != old_matches
            hnl_matches = (
                parsed_matches
                if matches_changed or not old_matches
                else old_matches
            )
            matches_ok = True
            source["activeMatchesUrl"] = HNL_SEMAFOR_URL
            print(f"HNL Semafor matches parsed: {len(parsed_matches)}")
        else:
            errors.append(
                f"Semafor schedule parser returned {len(parsed_matches)} matches"
            )
    except Exception as exc:
        errors.append(f"Semafor schedule: {exc}")

    if not matches_ok:
        try:
            parsed_matches = parse_hnl_hr_matches(
                public_get(HNL_RESULTS_URL),
                now,
            )
            if len(parsed_matches) >= HNL_MIN_MATCHES:
                matches_changed = parsed_matches != old_matches
                hnl_matches = (
                    parsed_matches
                    if matches_changed or not old_matches
                    else old_matches
                )
                matches_ok = True
                source["activeMatchesUrl"] = HNL_RESULTS_URL
                print(f"HNL hnl.hr matches parsed: {len(parsed_matches)}")
            else:
                errors.append(
                    f"hnl.hr schedule parser returned {len(parsed_matches)} matches"
                )
        except Exception as exc:
            errors.append(f"hnl.hr schedule: {exc}")

    # Prefer the same Semafor response for standings so a successful HNL refresh
    # requires only one public request. Fall back to hnl.hr if necessary.
    if semafor_html:
        try:
            parsed_standings = parse_hnl_standings(semafor_html, now)
            if parsed_standings is not None:
                standings_changed = (
                    not old_standings
                    or payload_without_updated_at(old_standings)
                    != payload_without_updated_at(parsed_standings)
                )
                hnl_standing_entry = (
                    parsed_standings
                    if standings_changed or not old_standings
                    else old_standings
                )
                standings_ok = True
                source["activeStandingsUrl"] = HNL_SEMAFOR_URL
                print("HNL Semafor standings parsed: 10 teams")
            else:
                errors.append(
                    "Semafor standings parser did not validate 10 teams"
                )
        except Exception as exc:
            errors.append(f"Semafor standings: {exc}")

    if not standings_ok:
        try:
            parsed_standings = parse_hnl_standings(
                public_get(HNL_STANDINGS_URL),
                now,
            )
            if parsed_standings is not None:
                standings_changed = (
                    not old_standings
                    or payload_without_updated_at(old_standings)
                    != payload_without_updated_at(parsed_standings)
                )
                hnl_standing_entry = (
                    parsed_standings
                    if standings_changed or not old_standings
                    else old_standings
                )
                standings_ok = True
                source["activeStandingsUrl"] = HNL_STANDINGS_URL
                print("HNL hnl.hr standings parsed: 10 teams")
            else:
                errors.append(
                    "hnl.hr standings parser did not validate 10 teams"
                )
        except Exception as exc:
            errors.append(f"hnl.hr standings: {exc}")

    if hnl_standing_entry is not None:
        standings["HNL"] = hnl_standing_entry

    has_verified_cache = bool(hnl_matches or hnl_standing_entry)
    if matches_ok or standings_ok:
        source["status"] = "connected"
        source["lastSuccessfulUpdate"] = (
            now.isoformat().replace("+00:00", "Z")
        )
        source.pop("lastError", None)
    elif has_verified_cache:
        source["status"] = "degraded"
        source["lastError"] = " | ".join(errors)[:600]
    else:
        source["status"] = "waiting"
        source["lastError"] = " | ".join(errors)[:600]

    source.update({
        "name": "SuperSport HNL / HNS",
        "matchesUrl": HNL_SEMAFOR_URL,
        "standingsUrl": HNL_SEMAFOR_URL,
        "matchCount": len(hnl_matches),
        "standingsTeamCount": (
            len(
                (
                    ((hnl_standing_entry or {}).get("standings") or [{}])[0]
                    .get("table") or []
                )
            )
            if hnl_standing_entry
            else 0
        ),
        "providerPriority": ["HNS Semafor", "hnl.hr"],
    })

    if errors:
        print("HNL diagnostics:", " | ".join(errors))

    return hnl_matches, standings, source


def main() -> None:
    now = datetime.now(timezone.utc)
    existing = load_existing()

    # Preserve the complete World Cup list so group tables and knockout history
    # remain available throughout the tournament. A temporary API failure must
    # never erase a previously downloaded tournament schedule.
    try:
        world_cup = api_get("/competitions/WC/matches").get("matches", [])
    except Exception as exc:
        print(f"World Cup refresh failed; keeping cached data: {exc}")
        world_cup = existing_matches_for_code(existing, "WC")

    cached = preserve_cached_football_matches(existing, now)
    nearby = fetch_nearby_matches(now)
    base_matches = merge_matches(world_cup, cached, nearby)

    # When a league is between seasons, the generic match feed can legitimately
    # contain nothing. In that case request the new season explicitly and keep
    # its upcoming fixtures in the shared JSON. This is what makes Bundesliga,
    # Premier League and the other summer-start leagues visible before round 1.
    season_schedules, schedule_checks = fetch_missing_competition_schedules(
        base_matches,
        existing,
        now,
    )

    standings = refresh_football_data_standings(existing, now)
    hnl_matches, standings, hnl_source = refresh_hnl(
        existing,
        standings,
        now,
    )

    matches = merge_matches(
        base_matches,
        season_schedules,
        hnl_matches,
    )
    matches = enrich_with_match_details(matches, now)

    sources = copy.deepcopy((existing or {}).get("sources") or {})
    sources["football-data.org"] = {
        "name": "football-data.org",
        "status": "connected",
        "competitionCodes": FOOTBALL_DATA_CODES,
        "scheduleHorizonDays": SCHEDULE_HORIZON_DAYS,
        "scheduleChecks": schedule_checks,
    }
    sources["HNL"] = hnl_source

    output = {
        "source": "Football Fun multi-source data service",
        "generatedAt": now.strftime("%Y-%m-%d %H:%M UTC"),
        "lastUpdate": now.strftime("%Y-%m-%d %H:%M UTC"),
        "competitionCodes": ALL_CODES,
        "sources": sources,
        "matches": matches,
        "live": [],
        "finished": [],
        "upcoming": [],
        "standings": standings,
    }

    for match in matches:
        status = str(match.get("status") or "").upper()
        if status in LIVE_STATUSES:
            output["live"].append(match)
        elif status in FINISHED_STATUSES:
            output["finished"].append(match)
        elif status in UPCOMING_STATUSES:
            output["upcoming"].append(match)

    if (
        existing is not None
        and without_timestamps(existing) == without_timestamps(output)
    ):
        print("No football data changes; live-results.json left unchanged")
        return

    OUTPUT_FILE.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("live-results.json updated")
    print("Matches:", len(matches))
    print("HNL matches:", len(hnl_matches))
    print("Live:", len(output["live"]))
    print("Finished:", len(output["finished"]))
    print("Upcoming:", len(output["upcoming"]))
    print("Standings available:", len(standings))


if __name__ == "__main__":
    main()
