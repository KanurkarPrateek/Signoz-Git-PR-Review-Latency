#!/usr/bin/env python3
"""
Create the OTLP canary alert in SigNoz.

Fires when the canary's spans stop arriving. This is the only reliable way to
detect the silent-drop failure mode, because the drop increments no metric --
there is literally no counter to threshold. You cannot alert on the pipeline,
so you alert on the data.

Two API details that cost me time:
  * /api/v1/rules requires `"version": "v5"`, even though /api/v1/dashboards
    on the same build happily accepts v4. v5 replaces the `builderQueries` map
    with a `queries` array of typed specs and an expression-based filter.
  * A rule cannot be created unless at least one notification channel exists.

    SIGNOZ_URL=http://<ip>:8080 SIGNOZ_API_KEY=$(cat .signoz-key) \
      python3 make_alert.py
"""
import json
import os
import sys
import urllib.error
import urllib.request

URL = os.environ["SIGNOZ_URL"].rstrip("/")
KEY = os.environ["SIGNOZ_API_KEY"].strip()
CHANNEL = os.environ.get("SIGNOZ_CHANNEL", "placeholder-webhook")

rule = {
    "alert": "OTLP canary stopped arriving",
    "alertType": "TRACES_BASED_ALERT",
    "ruleType": "threshold_rule",
    "evalWindow": "5m",
    "frequency": "1m",
    "version": "v5",
    "condition": {
        "compositeQuery": {
            "queries": [{
                "type": "builder_query",
                "spec": {
                    "name": "A",
                    "signal": "traces",
                    "disabled": False,
                    "aggregations": [{"expression": "count()"}],
                    "filter": {"expression": "service.name = 'otlp-canary'"},
                    "stepInterval": 60,
                },
            }],
        },
        "op": "4",          # Below
        "target": 1,        # fewer than 1 canary span in the window
        "matchType": "2",   # at least once
        "targetUnit": "",
    },
    "labels": {"severity": "critical", "team": "observability"},
    "annotations": {
        "title": "OTLP canary stopped arriving",
        "description":
            "No canary spans in 5 minutes. Telemetry is being dropped between "
            "the exporter and ClickHouse. The collector will report healthy: "
            "the age-limit drop in the clickhousetraces exporter logs at Debug "
            "and increments no metric, so this alert is the only signal there "
            "is.",
    },
    "preferredChannels": [CHANNEL],
}


def main():
    req = urllib.request.Request(
        URL + "/api/v1/rules", data=json.dumps(rule).encode(), method="POST",
        headers={"Content-Type": "application/json", "SIGNOZ-API-KEY": KEY})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"created alert: {rule['alert']} (http={r.status})")
            print(f"  -> {URL}/alerts")
            return 0
    except urllib.error.HTTPError as e:
        print(f"FAILED http={e.code}")
        print(e.read().decode()[:500])
        return 1


if __name__ == "__main__":
    sys.exit(main())
