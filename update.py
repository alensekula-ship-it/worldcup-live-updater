import os
import requests

API_TOKEN = os.getenv("FOOTBALL_DATA_API_KEY")

headers = {
    "X-Auth-Token": API_TOKEN
}

url = "https://api.football-data.org/v4/competitions/WC"

response = requests.get(url, headers=headers)

print(response.status_code)
print(response.text)
