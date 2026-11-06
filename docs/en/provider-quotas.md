# Request quota and provider cooldowns

`PROVIDER_DAILY_LIMIT` is Phonoteka's local per-provider rolling 24-hour quota, not an API allowance or an IP block. The default is 2500; `0` disables the daily quota. Negative values are rejected. Redeploy app and worker through the normal release process after changing it. Re-enabling a positive value immediately accounts for requests already recorded in the current window.

Disabling this quota preserves caching, durable attempt accounting, concurrency and rate limits, no automatic replay of sent requests with an unknown outcome, and the per-recording fingerprint budget. Counters are not reset and existing jobs are not recreated.

LRCLIB requests are sequential, with at least one second after each response. HTTP 429 and 503 set a shared provider cooldown using `Retry-After` (seconds or HTTP-date), plus bounded random jitter. Waiting requests check that cooldown immediately before admission. For higher remote allowances, LRCLIB offers application registration: [official requirements](https://lrclib.net/docs.txt). Changing IP addresses does not affect Phonoteka's local quota.

Provider settings explicitly label a disabled quota. `/api/providers` returns `daily_limit: 0` and `daily_limit_enabled: false`. The `phonoteka_provider_daily_limit_enabled` metric is 0 and `phonoteka_provider_budget_remaining` is `+Inf`; `phonoteka_provider_requests_24h` still reports usage. This does not promise unlimited remote service capacity.

The streaming deadline test expires the real timeout after the first response chunk. It verifies closure, an unknown outcome and exactly one durable request without depending on SQLite admission finishing within 200 ms. Production deadlines are unchanged.
