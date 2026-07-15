#!/usr/bin/env python3
"""
Replay a GitHub repository's pull-request history into SigNoz as OTel traces.

Span model — one PR is one trace, and every span duration is a real wait:

    PR #123 <title>                      first commit (or open) -> merge/close
    |-- authoring                        first commit -> PR opened
    |   |-- commit abc1234               point-in-time marker
    |-- awaiting first review            opened -> first review submitted
    |-- review round 1 by alice          previous event -> this review
    |-- awaiting merge                   last review -> merged

Because the durations are real, SigNoz's built-in aggregations answer the
interesting questions without any extra work: p95 of "awaiting first review"
grouped by reviewer is time-to-first-review by reviewer.

Backfilled spans carry historical timestamps. ClickHouse applies the traces
TTL at INSERT time against the span's own timestamp, so anything older than
the retention window is accepted over OTLP (HTTP 200, partialSuccess:{}) and
then silently discarded. `preflight` refuses to run in that situation; it is
the whole reason this tool has a preflight at all.

Usage:
    ./backfill.py fetch     --repo SigNoz/signoz --limit 500
    ./backfill.py preflight --repo SigNoz/signoz
    ./backfill.py send      --repo SigNoz/signoz --endpoint http://localhost:14318
    ./backfill.py verify    --repo SigNoz/signoz
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")

CH_POD = "chi-signoz-clickhouse-cluster-0-0-0"
CH_NS = "signoz"
NS_PER_DAY = 86_400 * 1_000_000_000


# --------------------------------------------------------------- time helpers

def iso_to_ns(s):
    """GitHub ISO-8601 ('2021-03-04T09:15:00Z') -> unix nanoseconds."""
    if not s:
        return None
    dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1_000_000_000)


def now_ns():
    return int(time.time() * 1_000_000_000)


def human(ns):
    return datetime.fromtimestamp(ns / 1e9, timezone.utc).strftime("%Y-%m-%d")


# ------------------------------------------------------------- deterministic ids
# Derived from repo+PR so re-running produces identical ids instead of
# duplicating every trace.

def _hex(seed, n):
    return hashlib.md5(seed.encode()).hexdigest()[:n]


def trace_id(repo, pr):
    return _hex(f"{repo}#{pr}", 32)


def span_id(repo, pr, kind, idx=0):
    return _hex(f"{repo}#{pr}:{kind}:{idx}", 16)


# ------------------------------------------------------------------- GitHub

QUERY = """
query($owner:String!, $name:String!, $cursor:String, $page:Int!) {
  repository(owner:$owner, name:$name) {
    pullRequests(first:$page, after:$cursor,
                 orderBy:{field:CREATED_AT, direction:DESC},
                 states:[MERGED, CLOSED, OPEN]) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title state createdAt mergedAt closedAt
        additions deletions changedFiles
        author { login }
        mergedBy { login }
        labels(first:10) { nodes { name } }
        reviews(first:20) { nodes { author { login } state submittedAt } }
        commits(first:20) {
          nodes { commit { oid messageHeadline committedDate } }
        }
      }
    }
  }
}
"""


def gh_token():
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True,
                           text=True, check=True)
        return r.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        sys.exit("error: need a GitHub token — run `gh auth login`")


def graphql(token, variables):
    body = json.dumps({"query": QUERY, "variables": variables}).encode()
    req = urllib.request.Request(
        "https://api.github.com/graphql", data=body, method="POST",
        headers={"Authorization": f"bearer {token}",
                 "Content-Type": "application/json",
                 "User-Agent": "signoz-git-backfill"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                out = json.loads(r.read().decode())
            if "errors" in out:
                msg = json.dumps(out["errors"])[:300]
                # secondary rate limit -> back off and retry
                if "RATE_LIMIT" in msg.upper():
                    time.sleep(20 * (attempt + 1))
                    continue
                sys.exit(f"graphql error: {msg}")
            return out["data"]["repository"]["pullRequests"]
        except urllib.error.HTTPError as e:
            if e.code in (403, 429, 502, 503):
                time.sleep(15 * (attempt + 1))
                continue
            sys.exit(f"http {e.code}: {e.read().decode()[:300]}")
    sys.exit("giving up after repeated GitHub errors")


def cache_path(repo):
    return os.path.join(CACHE, repo.replace("/", "_") + ".json")


def cmd_fetch(args):
    token = gh_token()
    owner, name = args.repo.split("/")
    os.makedirs(CACHE, exist_ok=True)
    prs, cursor = [], None
    while len(prs) < args.limit:
        page = min(args.page, args.limit - len(prs))
        data = graphql(token, {"owner": owner, "name": name,
                               "cursor": cursor, "page": page})
        prs.extend(data["nodes"])
        print(f"  fetched {len(prs)}", flush=True)
        if not data["pageInfo"]["hasNextPage"]:
            break
        cursor = data["pageInfo"]["endCursor"]
    with open(cache_path(args.repo), "w") as f:
        json.dump(prs, f)
    if prs:
        oldest = min(p["createdAt"] for p in prs)
        newest = max(p["createdAt"] for p in prs)
        print(f"\ncached {len(prs)} PRs -> {cache_path(args.repo)}")
        print(f"range : {oldest[:10]} .. {newest[:10]}")


def load_prs(repo):
    p = cache_path(repo)
    if not os.path.exists(p):
        sys.exit(f"no cache for {repo} — run `fetch` first")
    with open(p) as f:
        return json.load(f)


# ------------------------------------------------------------------ spans

def attr(k, v):
    # bool must precede int: bool is a subclass of int
    if isinstance(v, bool):
        return {"key": k, "value": {"boolValue": v}}
    if isinstance(v, int):
        return {"key": k, "value": {"intValue": str(v)}}
    if isinstance(v, float):
        return {"key": k, "value": {"doubleValue": v}}
    return {"key": k, "value": {"stringValue": str(v)}}


def mkspan(tid, sid, parent, name, kind, start, end, attrs, error=False):
    if end < start:
        end = start
    s = {"traceId": tid, "spanId": sid, "name": name, "kind": kind,
         "startTimeUnixNano": str(start), "endTimeUnixNano": str(end),
         "attributes": attrs}
    if parent:
        s["parentSpanId"] = parent
    # 2 = STATUS_CODE_ERROR, 1 = OK
    s["status"] = {"code": 2, "message": "pr closed unmerged"} if error else {"code": 1}
    return s


def build_pr_spans(repo, pr, cutoff_ns):
    """Turn one PR into a list of OTLP spans. Returns [] if it predates cutoff."""
    num = pr["number"]
    created = iso_to_ns(pr["createdAt"])
    merged = iso_to_ns(pr.get("mergedAt"))
    closed = iso_to_ns(pr.get("closedAt"))
    state = pr["state"]
    author = (pr.get("author") or {}).get("login") or "ghost"

    # reviews, chronological, ignoring PENDING (never submitted)
    reviews = sorted(
        [r for r in pr["reviews"]["nodes"] if r.get("submittedAt")],
        key=lambda r: r["submittedAt"])
    commits = sorted(
        [c["commit"] for c in pr["commits"]["nodes"] if c.get("commit")],
        key=lambda c: c["committedDate"])

    # A PR's real work starts at its first commit, which usually predates the
    # PR being opened. Anchoring the root there makes the authoring phase
    # visible instead of hiding it before the trace begins.
    first_commit = iso_to_ns(commits[0]["committedDate"]) if commits else None
    root_start = min(created, first_commit) if first_commit else created
    root_end = merged or closed or now_ns()
    if root_end <= root_start:
        root_end = root_start + 1_000_000  # 1ms floor for same-instant PRs

    if root_start < cutoff_ns:
        return []

    tid = trace_id(repo, num)
    root_sid = span_id(repo, num, "root")
    opened_dt = datetime.fromtimestamp(created / 1e9, timezone.utc)

    root_attrs = [
        attr("pr.number", num),
        attr("pr.title", pr["title"][:120]),
        attr("pr.state", state),
        attr("pr.author", author),
        attr("pr.merged", state == "MERGED"),
        attr("pr.additions", pr["additions"]),
        attr("pr.deletions", pr["deletions"]),
        attr("pr.changed_files", pr["changedFiles"]),
        attr("pr.churn", pr["additions"] + pr["deletions"]),
        attr("pr.review_count", len(reviews)),
        attr("pr.commit_count", len(commits)),
        attr("pr.opened_day", opened_dt.strftime("%A")),
        attr("pr.opened_hour", opened_dt.hour),
        attr("pr.opened_at", pr["createdAt"]),
        attr("vcs.repository", repo),
        attr("vcs.provider", "github"),
    ]
    if pr.get("mergedBy"):
        root_attrs.append(attr("pr.merged_by", pr["mergedBy"]["login"]))
    labels = [n["name"] for n in pr["labels"]["nodes"]]
    if labels:
        root_attrs.append(attr("pr.labels", ",".join(labels)))
    if reviews:
        root_attrs.append(attr("pr.first_reviewer",
                               (reviews[0].get("author") or {}).get("login") or "ghost"))

    spans = [mkspan(tid, root_sid, None, f"PR #{num} {pr['title'][:60]}",
                    2, root_start, root_end, root_attrs,
                    error=(state == "CLOSED"))]

    # authoring: first commit -> PR opened
    if first_commit and first_commit < created:
        auth_sid = span_id(repo, num, "authoring")
        spans.append(mkspan(tid, auth_sid, root_sid, "authoring", 1,
                            first_commit, created,
                            [attr("pr.author", author),
                             attr("pr.commit_count", len(commits))]))
        parent_for_commits = auth_sid
    else:
        parent_for_commits = root_sid

    for i, c in enumerate(commits):
        ts = iso_to_ns(c["committedDate"])
        spans.append(mkspan(tid, span_id(repo, num, "commit", i),
                            parent_for_commits,
                            f"commit {c['oid'][:7]}", 1,
                            ts, ts + 1_000_000,
                            [attr("commit.sha", c["oid"][:12]),
                             attr("commit.message", c["messageHeadline"][:120]),
                             attr("pr.author", author)]))

    # awaiting first review: the headline human-latency span
    if reviews:
        first = reviews[0]
        first_ts = iso_to_ns(first["submittedAt"])
        reviewer = (first.get("author") or {}).get("login") or "ghost"
        spans.append(mkspan(tid, span_id(repo, num, "await_review"), root_sid,
                            "awaiting first review", 1, created, first_ts,
                            [attr("pr.reviewer", reviewer),
                             attr("pr.author", author),
                             attr("pr.churn", pr["additions"] + pr["deletions"])]))

        prev = first_ts
        for i, r in enumerate(reviews):
            ts = iso_to_ns(r["submittedAt"])
            who = (r.get("author") or {}).get("login") or "ghost"
            spans.append(mkspan(tid, span_id(repo, num, "review", i), root_sid,
                                f"review round {i + 1} by {who}", 1,
                                prev if i else ts, max(ts, prev if i else ts),
                                [attr("pr.reviewer", who),
                                 attr("review.state", r["state"]),
                                 attr("review.round", i + 1),
                                 attr("pr.author", author)],
                                error=(r["state"] == "CHANGES_REQUESTED")))
            prev = ts

        # awaiting merge: approved but sitting there
        if merged and prev < merged:
            spans.append(mkspan(tid, span_id(repo, num, "await_merge"), root_sid,
                                "awaiting merge", 1, prev, merged,
                                [attr("pr.author", author),
                                 attr("pr.review_count", len(reviews))]))
    elif merged:
        # merged with zero reviews — worth being able to query for
        spans.append(mkspan(tid, span_id(repo, num, "unreviewed"), root_sid,
                            "merged without review", 1, created, merged,
                            [attr("pr.author", author),
                             attr("pr.unreviewed", True)]))

    return spans


# --------------------------------------------------------------- retention

def clickhouse(query):
    try:
        r = subprocess.run(
            ["kubectl", "exec", "-n", CH_NS, CH_POD, "-c", "clickhouse", "--",
             "clickhouse-client", "--query", query],
            capture_output=True, text=True, timeout=60)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def retention_days():
    """Read the live traces TTL out of ClickHouse. None if unreadable."""
    q = ("SELECT create_table_query FROM system.tables "
         "WHERE database='signoz_traces' AND name='signoz_index_v3'")
    out = clickhouse(q)
    if not out:
        return None
    m = re.search(r"TTL toDateTime\(timestamp\) \+ toIntervalSecond\((\d+)\)", out)
    return int(m.group(1)) / 86400 if m else None


def cmd_preflight(args):
    prs = load_prs(args.repo)
    oldest = min(iso_to_ns(p["createdAt"]) for p in prs)
    span_days = (now_ns() - oldest) / NS_PER_DAY
    print(f"PRs cached      : {len(prs)}")
    print(f"oldest PR       : {human(oldest)}  ({span_days:.0f} days ago)")

    days = retention_days()
    if days is None:
        print("retention       : UNKNOWN (could not reach ClickHouse)")
        return 0
    print(f"traces retention: {days:.1f} days")

    if span_days > days:
        lost = sum(1 for p in prs
                   if (now_ns() - iso_to_ns(p["createdAt"])) / NS_PER_DAY > days)
        print(f"\n  REFUSING TO SEND")
        print(f"  {lost}/{len(prs)} PRs predate the {days:.0f}-day retention window.")
        print(f"  ClickHouse applies the TTL at INSERT against the span's own")
        print(f"  timestamp, so those spans would be accepted over OTLP with")
        print(f"  HTTP 200 and then silently dropped. You would see no error.")
        print(f"\n  Fix: raise traces retention to >= {span_days:.0f} days, then re-run.")
        return 1
    print("\nOK — every PR falls inside the retention window.")
    return 0


# ------------------------------------------------------------------- send

def post_otlp(endpoint, spans, service):
    payload = {"resourceSpans": [{
        "resource": {"attributes": [
            attr("service.name", service),
            attr("service.namespace", "vcs"),
            attr("deployment.environment", "backfill"),
        ]},
        "scopeSpans": [{"scope": {"name": "git-backfill"}, "spans": spans}],
    }]}
    req = urllib.request.Request(
        endpoint.rstrip("/") + "/v1/traces",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status, r.read().decode()


def cmd_send(args):
    prs = load_prs(args.repo)
    days = retention_days()
    cutoff = now_ns() - int(days * NS_PER_DAY) if days and not args.force else 0
    if days and not args.force:
        print(f"retention {days:.0f}d -> dropping PRs older than {human(cutoff)}")

    all_spans, skipped = [], 0
    for pr in prs:
        s = build_pr_spans(args.repo, pr, cutoff)
        if not s:
            skipped += 1
        all_spans.extend(s)

    print(f"built {len(all_spans)} spans from {len(prs) - skipped} PRs "
          f"({skipped} outside retention)")
    if args.dry_run:
        print(json.dumps(all_spans[:2], indent=2)[:1500])
        return 0
    if not all_spans:
        print("nothing to send")
        return 1

    sent = 0
    for i in range(0, len(all_spans), args.batch):
        chunk = all_spans[i:i + args.batch]
        st, body = post_otlp(args.endpoint, chunk, args.service or
                             args.repo.replace("/", "-").lower())
        sent += len(chunk)
        print(f"  sent {sent}/{len(all_spans)}  http={st} {body[:40]}", flush=True)
        time.sleep(0.2)
    print("\nsend complete — now run `verify`. HTTP 200 does not mean stored.")
    return 0


# ----------------------------------------------------------------- verify

def cmd_verify(args):
    service = args.service or args.repo.replace("/", "-").lower()
    prs = load_prs(args.repo)
    # Count DISTINCT span ids, not rows. The exporter retries at-least-once,
    # so a slow sink inflates row count while the real coverage stays flat --
    # comparing raw rows to expected reports success on duplicated data.
    q = (f"SELECT count(), uniqExact(span_id) "
         f"FROM signoz_traces.distributed_signoz_index_v3 "
         f"WHERE resource_string_service$$name = '{service}'")
    got = clickhouse(q)
    if got is None:
        sys.exit("could not query ClickHouse")
    rows, stored = (int(x) for x in got.split("\t"))

    days = retention_days() or 0
    cutoff = now_ns() - int(days * NS_PER_DAY)
    expected = sum(len(build_pr_spans(args.repo, p, cutoff)) for p in prs)

    print(f"service         : {service}")
    print(f"spans expected  : {expected}")
    print(f"distinct stored : {stored}")
    print(f"rows on disk    : {rows}")

    if rows > stored:
        print(f"\n  DUPLICATES: {rows/stored:.1f}x — {rows - stored} redundant rows.")
        print("  The exporter retried batches the sink was too slow to accept.")
    if stored == 0:
        print("\nnothing landed, despite HTTP 200 on every batch.")
    elif stored < expected:
        pct = 100 * (1 - stored / expected)
        print(f"\n  MISSING: {expected - stored} spans ({pct:.1f}%) never arrived.")
    else:
        print("\nall spans accounted for.")

    rng = clickhouse(
        f"SELECT min(toDateTime(timestamp)), max(toDateTime(timestamp)) "
        f"FROM signoz_traces.distributed_signoz_index_v3 "
        f"WHERE resource_string_service$$name = '{service}'")
    if rng:
        print(f"stored range    : {rng}")
    return 0


# ------------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="pull PR history from GitHub into cache/")
    f.add_argument("--repo", required=True, help="owner/name")
    f.add_argument("--limit", type=int, default=500)
    f.add_argument("--page", type=int, default=25)

    pf = sub.add_parser("preflight", help="check cached PRs against live retention")
    pf.add_argument("--repo", required=True)

    s = sub.add_parser("send", help="build spans and export over OTLP")
    s.add_argument("--repo", required=True)
    s.add_argument("--endpoint", default="http://localhost:14318")
    s.add_argument("--service", default=None)
    s.add_argument("--batch", type=int, default=400)
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--force", action="store_true",
                   help="send even if older than retention (they will vanish)")

    v = sub.add_parser("verify", help="count what actually reached ClickHouse")
    v.add_argument("--repo", required=True)
    v.add_argument("--service", default=None)

    args = p.parse_args()
    return {"fetch": cmd_fetch, "preflight": cmd_preflight,
            "send": cmd_send, "verify": cmd_verify}[args.cmd](args) or 0


if __name__ == "__main__":
    sys.exit(main())
