# AGENTS.md

## Project boundary

- `sibot` is a NoneBot 2 plugin using the OneBot V11 adapter. It provides the `/ai` command surface and background notifications for the configured group.
- CPA Portal is the only application API boundary for the Mzk1 AI plugin. Use its authenticated Admin API; do not give the bot Keeper passwords, CPA management keys, or direct access to either upstream service.
- CPA and Keeper remain responsible for model routing, quota collection, quota history, and usage aggregation. The bot presents those results and must not implement a second quota database or a hidden rate limiter.

## Structure and conventions

- `sibot/plugins/mzk1_ai/commands.py` parses `/ai` arguments and should remain free of network calls.
- `portal.py` owns the async Portal client and its protocol/error mapping. `models.py` owns validated upstream models and small immutable domain values.
- `quota.py` extracts the supported Codex weekly window. `forecast.py` computes shared-pool forecasts from reset-separated quota history; it must not predict individual-account exhaustion or simulate CPA's routing weights.
- `formatting.py` owns compact Chinese output suitable for narrow QQ clients. Keep user-facing wording there rather than in request or calculation code.
- `monitor.py` owns periodic quota polling, persisted alert state, retry, and notification delivery. On-demand commands must not mutate monitor state or enqueue notifications.
- Persistent plugin state belongs under `nonebot_plugin_localstore`; keep state writes atomic and do not store credentials or upstream secrets.
- Target-group filtering, environment configuration, and OneBot sending behavior should follow the existing plugin helpers rather than introducing parallel abstractions.

## Rules

- Keep all external calls asynchronous and bounded by the existing client timeouts/concurrency limits. Handle Portal protocol, authentication, upstream, and availability errors without leaking response bodies or secrets.
- Treat upstream quota JSON as a compatibility boundary: validate the fields needed by the current feature, tolerate additive fields, and fail closed when the data is stale, incomplete, mixed-plan, or from mismatched reset cycles.
- Shared-pool forecasts use observed quota-percentage changes, not token counts as a substitute for subscription quota. Separate natural reset cycles before calculating burn rates; never turn a reset replenishment into negative consumption.
- Account-level CPA `unavailable` state may exclude balance from current runway, but it must not be interpreted as reduced historical demand. The bot must not clear cooldowns, reset accounts, alter routing, or apply throttling automatically.
- Keep configuration names compatible with the `MZK1_AI_` environment convention. Adding a setting requires updating `Config` and its validation together; do not hard-code deployment-specific secrets or group IDs.
- Do not write tests. Do not add test files or test-only abstractions.
- Do not modify `README.md`. Keep this file as the project-level agent guidance; product or deployment documentation changes require a separate explicit request.

## Verification

- For Python source changes, use the checks already defined in `.github/workflows/ci.yml`: `uv run ruff format --check .`, `uv run ruff check .`, and `uv run basedpyright`.
- For command, formatting, Portal-client, or forecast changes, keep verification within the existing project checks unless the user explicitly requests another procedure.
- Do not deploy the bot, change production configuration, or send real OneBot messages unless the user explicitly authorizes that operation.
