# Signoz-Git-PR-Review-Latency

Replay a GitHub repo's pull-request history into SigNoz as OpenTelemetry traces,
so review latency becomes a p95 instead of a feeling.

Built while backfilling 5.5 years of [SigNoz's own](https://github.com/SigNoz/signoz)
PR history — 7,345 PRs, 91,532 spans, 2021-01-07 → 2026-07-15.

## The thing you need to know first

**SigNoz's collector silently discards any span older than 15 days.** Not a TTL,
not a config — a hardcoded constant in the traces exporter
([`clickhouse_exporter.go:144`](https://github.com/SigNoz/signoz-otel-collector/blob/v0.144.5/exporter/clickhousetracesexporter/clickhouse_exporter.go#L144)):

```go
maxAllowedDataAgeDays: 15,
```

The drop is invisible: it's logged at `Debug` (collectors run at `Info`) and
increments no metric, so OTLP returns `200 partialSuccess:{}` and
`send_failed_spans` stays at zero while your data ceases to exist.

The **logs** exporter has the identical constant but
[exposes it](https://github.com/SigNoz/signoz-otel-collector/blob/v0.144.5/exporter/clickhouselogsexporter/config.go#L55)
as `max_allowed_data_age_days`, added in
[#746](https://github.com/SigNoz/signoz-otel-collector/pull/746) to fix
[#750](https://github.com/SigNoz/signoz-otel-collector/issues/750) — the same
complaint, for logs, in January 2026. Traces never got the same twenty lines.

Backfilling therefore needs a one-line patch to the collector:

```diff
- maxAllowedDataAgeDays: 15,
+ maxAllowedDataAgeDays: 3650,
```

Verified on SigNoz v0.131.0 / signoz-otel-collector v0.144.5. Constant unchanged
on `main` and v0.144.6.

## Tools

| file | what it does |
|---|---|
| `probe.py` | Sends one span of a chosen age. `--sweep` finds the age wall on **any** OTLP endpoint. |
| `backfill.py` | `fetch` PR history → `preflight` → `send` over OTLP → `verify` what actually landed. |
| `make_git_dashboards.py` | Builds the review-latency dashboard via the SigNoz API. |
| `make_alert.py` | Canary alert rule (see caveat below). |
| `canary.yaml` | Heartbeat deployment that emits one span/minute. |
| `make_pdf.py` | Renders `BLOG-DRAFT.md` to PDF. |

## Quickstart

```bash
# 1. Is your endpoint eating old spans? Find the wall.
kubectl port-forward -n signoz svc/signoz-otel-collector 14318:4318 &
python3 probe.py --sweep --endpoint http://localhost:14318
# Every line says 200. Now go check which ones actually exist.

# 2. Pull PR history
python3 backfill.py fetch --repo SigNoz/signoz --limit 8000

# 3. Refuse to send data the pipeline would silently eat
python3 backfill.py preflight --repo SigNoz/signoz

# 4. Send, then VERIFY — HTTP 200 does not mean stored
python3 backfill.py send --repo SigNoz/signoz --endpoint http://localhost:14319
python3 backfill.py verify --repo SigNoz/signoz
```

Requires `gh` (authenticated) for the GitHub GraphQL API, and `kubectl` access
to the ClickHouse pod for `preflight`/`verify`.

## Span model

Every span's *duration* is a real wait, which is what lets SigNoz's built-in
aggregations do the analysis for free:

```
PR #123 (first commit → merge)
├── authoring              first commit → PR opened
│   └── commit abc1234
├── awaiting first review  opened → first review     ← the human latency
├── review round 1 by alice (CHANGES_REQUESTED)
└── awaiting merge         last approval → merged
```

## Two caveats worth your time

**Retry is at-least-once.** Writing through a `kubectl port-forward` is slow
enough to time out; the exporter retries and rewrites rows that already landed.
The first full run produced **8.1× duplicates** (445,038 rows for 52,212 real
spans) while reporting `sent 91536/91536 http=200`. Hence
`retry_on_failure: false` and a long timeout — a visible gap beats silent
duplication. `verify` counts distinct span IDs for the same reason.

**The canary alert can't fire.** A threshold rule on "canary spans below 1"
stays `inactive` when there are no canary spans, because *no data is the absence
of a series*, not a low value. It goes quiet exactly when it should scream. A
dead man's switch has to live outside the system it watches.

## Findings

Time to first review across 5.5 years of SigNoz PRs:

| reviewer | n | p50 | p95 |
|---|---|---|---|
| bots (`ellipsis-dev`) | 1,576 | 1.85 min | 32.8 h |
| humans | 4,667 | **4.76 h** | **15.33 days** |

A quarter of "first reviews" are an AI bot. Blend them and the median reads
1.0h — a fact about a bot, not about anyone waiting on review.

Note there is deliberately **no "slowest reviewer" metric** here.
`awaiting first review` grouped by reviewer measures the wait *before that
person showed up* — they ended the wait, they didn't cause it. The data cannot
answer that question.
