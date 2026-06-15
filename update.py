import os
import requests

API_TOKEN = os.getenv("API_TOKEN")

headers = {
    "X-Auth-Token": API_TOKEN
}

url = "https://api.football-data.org/v4/competitions/WC/matches"

response = requests.get(url, headers=headers)

print(response.status_code)
print(response.text[:500])
