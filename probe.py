#!/usr/bin/env python3
"""
Send a single OTLP span of a chosen age and report what the wire said.

The point of this tool is that the wire *always* says 200. Use `verify` in
backfill.py (or query ClickHouse) to find out whether the span actually
exists. HTTP 200 from an OTLP endpoint means "I parsed your protobuf", not
"I stored your data".

    ./probe.py --age-days 0     --endpoint http://localhost:14319
    ./probe.py --age-days 730   --endpoint http://localhost:14319
    ./probe.py --sweep          --endpoint http://localhost:14319
"""
import argparse
import json
import secrets
import sys
import time
import urllib.request


def send(endpoint, age_days, service, label=None):
    start = int((time.time() - age_days * 86400) * 1e9)
    end = start + int(1e8)
    name = label or f"probe-{age_days}d"
    payload = {"resourceSpans": [{
        "resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": service}}]},
        "scopeSpans": [{"scope": {"name": "probe"}, "spans": [{
            "traceId": secrets.token_hex(16),
            "spanId": secrets.token_hex(8),
            "name": name, "kind": 1,
            "startTimeUnixNano": str(start),
            "endTimeUnixNano": str(end),
            "attributes": [
                {"key": "probe.age_days", "value": {"doubleValue": age_days}}],
        }]}]}]}
    req = urllib.request.Request(
        endpoint.rstrip("/") + "/v1/traces",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode()
    except Exception as e:
        return None, str(e)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--endpoint", default="http://localhost:14319")
    p.add_argument("--service", default="probe")
    p.add_argument("--age-days", type=float, default=0)
    p.add_argument("--sweep", action="store_true",
                   help="sweep 1h..20d to locate the ingest age wall")
    a = p.parse_args()

    if a.sweep:
        print(f"{'age':>10}  {'http':>5}  body")
        for hours in [1, 6, 12, 24, 48, 72, 120, 240, 336, 360, 384, 480]:
            st, body = send(a.endpoint, hours / 24, a.service, f"sweep-{hours}h")
            print(f"{str(hours)+'h':>10}  {str(st):>5}  {body[:34]}")
        print("\nEvery line above says 200. That is the problem —")
        print("now check which of them actually exist.")
        return 0

    st, body = send(a.endpoint, a.age_days, a.service)
    print(f"age={a.age_days}d  http={st}  body={body[:60]}")
    return 0 if st == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
