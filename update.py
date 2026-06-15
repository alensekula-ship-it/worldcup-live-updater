import json
from datetime import datetime

data = {
    "lastUpdate": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    "live": [],
    "finished": [],
    "upcoming": []
}

with open("live-results.json", "w") as f:
    json.dump(data, f, indent=2)

print("Live results updated")
