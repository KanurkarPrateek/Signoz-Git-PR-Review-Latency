#!/usr/bin/env python3
"""
Build the PR-review dashboards in SigNoz from the backfilled git traces.

Auth uses a SigNoz API key (Settings -> API Keys, Admin role) via the
SIGNOZ-API-KEY header -- not an email/password login, whose accessToken is a
short-lived JWT that expires out from under you.

    SIGNOZ_URL=http://<ip>:8080 SIGNOZ_API_KEY=$(cat .signoz-key) \
      python3 make_git_dashboards.py

NOTE: these dashboards cover 5.5 years of history. SigNoz's default time range
is the last 15 minutes, so they look empty until you widen it.
"""
import json
import os
import sys
import uuid
import urllib.error
import urllib.request

URL = os.environ["SIGNOZ_URL"].rstrip("/")
KEY = os.environ["SIGNOZ_API_KEY"].strip()
SERVICE = os.environ.get("SIGNOZ_SERVICE", "signoz-signoz")


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("SIGNOZ-API-KEY", KEY)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


# ------------------------------------------------------------- query helpers

def tag(key, dtype="string"):
    return {"key": key, "dataType": dtype, "type": "tag", "isColumn": False}


def col(key, dtype="float64"):
    return {"key": key, "dataType": dtype, "type": "", "isColumn": True}


def filt(key, op, value, dtype="string", is_col=False):
    k = (col(key, dtype) if is_col else tag(key, dtype))
    return {"key": k, "op": op, "value": value}


SVC = filt("service.name", "=", SERVICE, "string", True)
DUR = col("durationNano")

# Only root spans carry pr.number, so "exists" cleanly selects one span per PR.
ROOT = filt("pr.number", "exists", "", "float64")
AWAIT_REVIEW = filt("name", "=", "awaiting first review", "string", True)
AWAIT_MERGE = filt("name", "=", "awaiting merge", "string", True)
NO_REVIEW = filt("name", "=", "merged without review", "string", True)

# ellipsis-dev is an AI review bot and the single largest "reviewer" in the
# repo: 1576 of 6243 first reviews, at a p50 of 0 hours. Leaving it in drags
# the global p50 from 4.8h down to 1.0h -- i.e. the headline number becomes a
# fact about a bot, not about the humans waiting on review.
BOTS = ["ellipsis-dev", "codecov", "dependabot", "github-actions[bot]"]
HUMANS = filt("pr.reviewer", "nin", BOTS)
IS_BOT = filt("pr.reviewer", "in", BOTS)

REVIEWER = tag("pr.reviewer")
AUTHOR = tag("pr.author")
STATE = tag("pr.state")


def qd(name, agg_op, agg_attr=None, filters=None, group=None, legend="",
       expr=None, order=None, disabled=False, reduce_to=None):
    # SigNoz computes the aggregation per stepInterval bucket and then reduces
    # across buckets. For a percentile that means a "value" panel shows an
    # average-of-daily-p50s, NOT the p50 over the whole window -- a quiet day
    # with 1 PR weighs the same as a busy one with 40. Summing percentiles
    # (the old default of "sum") is worse still: it is meaningless.
    #
    # The true p50 over 5.5 years only comes from a single-bucket query. The
    # headline numbers in the writeup are computed in ClickHouse for this
    # reason; these panels are for TRENDS, not for quoting.
    if reduce_to is None:
        reduce_to = "sum" if agg_op == "count" else "avg"
    return {
        "dataSource": "traces",
        "queryName": name,
        "aggregateOperator": agg_op,
        "aggregateAttribute": agg_attr or {},
        "timeAggregation": "rate" if agg_op == "count" else agg_op,
        "spaceAggregation": "sum",
        "functions": [],
        "filters": {"op": "AND", "items": filters or []},
        "groupBy": group or [],
        "expression": expr or name,
        "disabled": disabled,
        "stepInterval": 86400,
        "having": [],
        "limit": None,
        "orderBy": order or [],
        "reduceTo": reduce_to,
        "legend": legend,
    }


def widget(title, panel, queries, formulas=None, desc="", yunit=None):
    return {
        "id": str(uuid.uuid4()),
        "title": title,
        "description": desc,
        "panelTypes": panel,
        "yAxisUnit": yunit or "",
        "query": {
            "queryType": "builder",
            "promql": [],
            "clickhouse_sql": [],
            "builder": {"queryData": queries, "queryFormulas": formulas or []},
        },
        "timePreferance": "GLOBAL",
        "softMin": 0,
        "softMax": 0,
        "fillSpans": False,
    }


def layout(widgets, per_row=2, h=6, w=6):
    return [{"i": w_["id"], "x": (i % per_row) * w, "y": (i // per_row) * h,
             "w": w, "h": h} for i, w_ in enumerate(widgets)]


def dashboard(title, desc, tags, widgets):
    return {"title": title, "description": desc, "tags": tags,
            "layout": layout(widgets), "widgets": widgets,
            "variables": {}, "version": "v4"}


# ---------------------------------------------------------------- dashboards

def dash_review():
    w = []
    w.append(widget("Pull requests", "value",
        [qd("A", "count", filters=[SVC, ROOT], legend="PRs")],
        desc="Root spans, one per PR"))

    w.append(widget("p50 time to first HUMAN review", "value",
        [qd("A", "p50", agg_attr=DUR, filters=[SVC, AWAIT_REVIEW, HUMANS],
            legend="p50")],
        yunit="ns", desc="Bots excluded. Including them reports 1.0h instead "
                         "of 4.8h — a fact about a bot, not about people."))

    w.append(widget("p95 time to first HUMAN review", "value",
        [qd("A", "p95", agg_attr=DUR, filters=[SVC, AWAIT_REVIEW, HUMANS],
            legend="p95")],
        yunit="ns", desc="The tail is where the pain lives"))

    w.append(widget("p50 time to first BOT review", "value",
        [qd("A", "p50", agg_attr=DUR, filters=[SVC, AWAIT_REVIEW, IS_BOT],
            legend="p50")],
        yunit="ns", desc="ellipsis-dev et al. Instant, and 25% of all first "
                         "reviews — which is exactly why it must be split out."))

    # abandonment rate = closed-unmerged / all PRs
    ab = widget("PR abandonment rate", "value", [
        qd("A", "count", filters=[SVC, ROOT, filt("pr.state", "=", "CLOSED")],
           disabled=True),
        qd("B", "count", filters=[SVC, ROOT], disabled=True),
    ], formulas=[{"queryName": "F1", "expression": "A*100/B",
                  "legend": "abandoned %", "disabled": False}],
        yunit="percent", desc="Closed without merging")
    w.append(ab)

    w.append(widget("Merged without any review", "value",
        [qd("A", "count", filters=[SVC, NO_REVIEW], legend="PRs")],
        desc="Straight to main"))

    # There is deliberately NO "slowest reviewer" widget here.
    #
    # Grouping `awaiting first review` by pr.reviewer looks like it measures
    # reviewer speed. It does not. The span measures how long the PR waited
    # before someone reviewed it -- and that person did not cause the wait,
    # they ended it. Whoever finally reviews a 616-day-old PR is the one who
    # rescued it. The metric ranks rescuers as culprits.
    #
    # It is also statistically empty: the "slowest" reviewers had n=1, n=2,
    # n=3. A p95 over one sample is not a number, and these are real named
    # people.
    #
    # Review VOLUME is honest -- it measures work actually done.
    w.append(widget("Review volume by reviewer", "table",
        [qd("A", "count", filters=[SVC, AWAIT_REVIEW, HUMANS],
            group=[REVIEWER], legend="first reviews",
            order=[{"columnName": "#SIGNOZ_VALUE", "order": "desc"}])],
        desc="Who does the reviewing. NOT who is slow — this data cannot "
             "answer that."))

    w.append(widget("Time to first human review over time (p50 vs p95)", "graph",
        [qd("A", "p50", agg_attr=DUR, filters=[SVC, AWAIT_REVIEW, HUMANS],
            legend="p50"),
         qd("B", "p95", agg_attr=DUR, filters=[SVC, AWAIT_REVIEW, HUMANS],
            legend="p95")],
        yunit="ns", desc="Did review latency get worse as the project grew?"))

    w.append(widget("PR throughput", "graph",
        [qd("A", "count", filters=[SVC, ROOT], legend="PRs opened")],
        desc="5.5 years of project growth"))

    w.append(widget("PR outcomes", "table",
        [qd("A", "count", filters=[SVC, ROOT], group=[STATE], legend="PRs")]))

    w.append(widget("p95 time from approval to merge", "value",
        [qd("A", "p95", agg_attr=DUR, filters=[SVC, AWAIT_MERGE], legend="p95")],
        yunit="ns", desc="Approved and still sitting there"))

    w.append(widget("Busiest authors", "table",
        [qd("A", "count", filters=[SVC, ROOT], group=[AUTHOR], legend="PRs",
            order=[{"columnName": "#SIGNOZ_VALUE", "order": "desc"}])]))

    return dashboard(
        "Git — PR Review Latency (5.5 years, bots split out)",
        "SigNoz's own PR history replayed as traces. Every span duration is a "
        "real wait. NOTE: set the time range to 5 years or these look empty.",
        ["git", "backfill", "review"], w)


def main():
    d = dash_review()
    st, resp = api("POST", "/api/v1/dashboards", body=d)
    if st in (200, 201):
        data = resp.get("data", resp)
        uid = data.get("uuid") or data.get("id")
        print(f"created: {d['title']}")
        print(f"  -> {URL}/dashboard/{uid}")
        print("\nSet the time range to 5 years — the default 15m window is empty.")
        return 0
    print(f"FAILED {st}")
    print(str(resp)[:600])
    return 1


if __name__ == "__main__":
    sys.exit(main())
