import os
import json
import requests
from datetime import datetime, timezone

API_TOKEN = os.getenv("API_TOKEN")

if not API_TOKEN:
    raise Exception("API_TOKEN is missing")

headers = {
    "X-Auth-Token": API_TOKEN
}

url = "https://api.football-data.org/v4/competitions/WC/matches"

response = requests.get(url, headers=headers, timeout=30)

print(response.status_code)
print(response.text[:1000])

if response.status_code != 200:
    raise Exception(f"Football-data API error: {response.status_code} {response.text[:500]}")

api_data = response.json()

output = {
    "source": "football-data.org",
    "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    "lastUpdate": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    "competition": api_data.get("competition", {}),
    "filters": api_data.get("filters", {}),
    "resultSet": api_data.get("resultSet", {}),
    "matches": api_data.get("matches", []),
    "live": [],
    "finished": [],
    "upcoming": []
}

for match in output["matches"]:
    status = match.get("status")

    if status in ["LIVE", "IN_PLAY", "PAUSED"]:
        output["live"].append(match)
    elif status == "FINISHED":
        output["finished"].append(match)
    elif status in ["TIMED", "SCHEDULED"]:
        output["upcoming"].append(match)

with open("live-results.json", "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print("live-results.json updated")
print("Generated at:", output["generatedAt"])
print("Matches:", len(output["matches"]))
print("Live:", len(output["live"]))
print("Finished:", len(output["finished"]))
print("Upcoming:", len(output["upcoming"]))
