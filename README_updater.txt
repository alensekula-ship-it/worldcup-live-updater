Football Fun updater - incremental/persistent revision

- Existing current-season matches are preserved and enriched by match ID.
- A season rollover is detected and the old season for that competition is replaced by the new current season.
- Failed competition/API calls never erase previously persisted data.
- 429/5xx/network errors use bounded retry/backoff.
- Per-competition lastSuccessfulUpdate is changed only when that competition data/standings actually changes.
- Global generatedAt/lastUpdate changes only when football data really changes.
- If nothing changed, live-results.json is not rewritten and GitHub creates no commit.
