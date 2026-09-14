#!/usr/bin/env python3
"""Render bench/results/results.json to markdown, and to the chart JSON the UI reads.

Two consumers, one input:
  * stdout (or --md FILE) -- markdown for docs/results.md and for the slide
  * --charts FILE         -- the arrays the Benchmark tab plots (design 7.3, T5.4)

THE HARD RULE
-------------
This script REFUSES to print a pass/fail verdict on any run whose `client_transport`
bucket is non-empty. It prints a warning banner instead, and exits 2.

That is not defensiveness, it is the central finding of design 6.3: across every load test
against this account `InvocationThrottles` stayed at **zero** and every single failure was
client-side transport exhaustion -- ConnectionReset(54) and NewConnectionError on mantle,
ReadTimeout / ConnectionClosed / EndpointConnection on bedrock-runtime -- which persisted
with a shared 60-connection pool, `ulimit -n` at 1,048,576 and 16K free ephemeral ports.
A saturated load generator reported as a Bedrock capacity limit is the one mistake that
would discredit the entire benchmark in front of an audience, and it is an easy mistake
to make because the symptom looks like a ceiling. So the ceiling is not certifiable from
that run: add generator capacity (design 8.4: ~18 workers across ~5 Fargate tasks) and
re-run.

Exit codes -- distinct on purpose, so CI and demo scripts can tell the three apart:
  0  verdict PASS
  1  verdict FAIL (targets missed, on a run clean enough to judge)
  2  NO VERDICT (client_transport non-empty, or nothing measured)

Usage
-----
  python3.12 bench/report.py bench/results/results.json
  python3.12 bench/report.py bench/results/results.json --md docs/results-bench.md \
      --charts bench/results/charts.json
"""

import argparse
import json
import os
import sys

# design 8.3's buckets, in the order they should be read: is it Bedrock, is it us, or is it
# our request?
BUCKETS = ("service_throttle", "client_transport", "application")

BUCKET_MEANING = {
    "service_throttle": "a real Bedrock capacity limit — back off, and a legitimate quota "
                        "data point",
    "client_transport": "OUR LOAD GENERATOR SATURATED, not Bedrock — never report as a "
                        "service limit",
    "application": "a bug in our request or prompt (4xx validation, schema-parse failure)",
}

BANNER = (
    "!!  NO VERDICT — CLIENT-TRANSPORT ERRORS PRESENT  !!\n"
    "The load generator hit its own ceiling on this run, so nothing here certifies a\n"
    "Bedrock limit. Client-transport failures are ConnectionReset / ReadTimeout /\n"
    "ConnectionClosed / EndpointConnection / NewConnectionError: our sockets, not their\n"
    "capacity. Add generator capacity (design 8.4: ~18 workers across ~5 Fargate tasks,\n"
    "and cap per-worker concurrency at 40 on the mantle lane) and re-run before quoting\n"
    "any throughput number from it."
)


def _fmt(value, unit=""):
    return "—" if value is None else f"{value}{unit}"


def _n(block, key):
    """A percentile out of a possibly-absent distribution block."""
    return None if not block else block.get(key)


# ------------------------------------------------------------------------------- verdict

def grade(results):
    """Grade the run against doc 6.5's criteria. Returns (checks, verdict_or_None).

    `verdict` is None when the run is not judgeable -- which is the refusal path, not a
    failure. The distinction is the whole point: "we cannot certify this" and "this missed
    the target" are different sentences to say out loud.
    """
    totals = results["totals"]
    targets = results["targets"]
    stage = results["run"]["stage"]
    steps = results.get("steps") or []

    # Grade latency on the cleanest measured step rather than on `totals`, which mixes
    # concurrency levels by construction.
    clean = [s for s in steps if s["ok"] and not s["errorsByBucket"]["client_transport"]]
    basis = clean[-1] if clean else (steps[-1] if steps else None)

    checks = []
    if basis:
        e2e_target = (targets["stage1E2eP50Ms"] if stage == "classify"
                      else targets["stage2E2eP95Ms"])
        e2e_key = "p50" if stage == "classify" else "p95"
        checks.append({
            "criterion": f"TTFT p50 (concurrency {basis['concurrency']})",
            "target": f"< {targets['ttftMs']} ms",
            "measured": _fmt(_n(basis["ttftMs"], "p50"), " ms"),
            "pass": _cmp_lt(_n(basis["ttftMs"], "p50"), targets["ttftMs"]),
        })
        checks.append({
            "criterion": f"E2E {e2e_key} — {stage} (concurrency {basis['concurrency']})",
            "target": f"< {e2e_target} ms",
            "measured": _fmt(_n(basis["e2eMs"], e2e_key), " ms"),
            "pass": _cmp_lt(_n(basis["e2eMs"], e2e_key), e2e_target),
        })
    checks.append({
        "criterion": "Error rate, all steps",
        "target": f"< {targets['errorRatePct']} %",
        "measured": _fmt(totals["errorRatePct"], " %"),
        "pass": _cmp_lt(totals["errorRatePct"], targets["errorRatePct"], allow_equal=True),
    })
    checks.append({
        "criterion": "Service throttles (429/503)",
        "target": "0",
        "measured": str(sum(totals["errorsByBucket"]["service_throttle"].values())),
        "pass": not totals["errorsByBucket"]["service_throttle"],
    })
    # Never graded, always reported. Sustained 700 RPS cannot be measured from one client
    # and the PoC says so on the slide rather than extrapolating quietly (design 8.4, R6).
    checks.append({
        "criterion": "Sustained throughput ≥ 700 RPS / 30 min",
        "target": "≥ 700 RPS",
        "measured": f"{max((s['achievedRps'] for s in steps), default=0)} RPS single client "
                    f"— EXTRAPOLATED, NOT MEASURED",
        "pass": None,
    })

    if totals["errorsByBucket"]["client_transport"] or not totals["ok"]:
        # Withdraw every per-criterion mark too, not just the overall verdict. A screenshot
        # of "[PASS] TTFT p50" taken from a saturated run is exactly the claim this script
        # exists to refuse, and the measurements it was computed from came out of a run
        # whose own generator was failing. The numbers stay; the judgement does not.
        for check in checks:
            check["pass"] = None
            check["notCertifiable"] = True
        return checks, None
    graded = [c["pass"] for c in checks if c["pass"] is not None]
    return checks, ("PASS" if all(graded) else "FAIL")


def _cmp_lt(measured, target, allow_equal=False):
    if measured is None:
        return None
    return measured <= target if allow_equal else measured < target


# ------------------------------------------------------------------------------ markdown

def render_markdown(results, checks, verdict):
    run, totals = results["run"], results["totals"]
    out = []
    add = out.append

    add(f"# Benchmark — {run['modelLabel']}")
    add("")
    add(f"Generated {results['generatedAt']} · lane **{run['lane']}** "
        f"(`{run['endpoint']}{run['path'] or ''}`) · region `{run['region']}` · "
        f"stage `{run['stage']}` · maxTokens {run['maxTokens']}")
    add("")
    add(f"- Transport: {run['throughVia']}")
    add(f"- SDK attempts per request: **{run['sdkAttempts']}** "
        f"(retries disabled so throttles stay visible)")
    add(f"- `response_format: json_schema strict`: "
        f"{'sent' if run['jsonSchemaSent'] else 'unavailable on this lane'}")
    add(f"- Warm-up discarded: {run['warmupSeconds']} s per step")
    add(f"- Percentiles: `{results['percentileMethod']}`")
    add(f"- Corpus: {len(run['corpusSamples'])} samples "
        f"(`{'`, `'.join(run['corpusSamples'])}`)")
    add("")

    if verdict is None:
        add("> ```")
        for line in BANNER.splitlines():
            add(f"> {line}")
        add("> ```")
        add("")
    else:
        add(f"**Verdict: {verdict}**")
        add("")

    add("## Success criteria (doc 6.5)")
    add("")
    add("| Criterion | Target | Measured | Status |")
    add("|---|---|---|---|")
    for check in checks:
        mark = {True: "✅", False: "❌", None: "⚠️ not graded"}[check["pass"]]
        if check.get("notCertifiable"):
            mark = "⚠️ not certifiable — client transport errors"
        add(f"| {check['criterion']} | {check['target']} | {check['measured']} | {mark} |")
    add("")

    add("## Per step")
    add("")
    add("| Concurrency | Requests | Errors | Achieved RPS | Out tok/s | TTFT p50 | TTFT p95 "
        "| E2E p50 | E2E p95 | E2E p99 | Escalation |")
    add("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for step in results["steps"]:
        add(f"| {step['concurrency']} | {step['requests']} | {step['errors']} "
            f"({step['errorRatePct']} %) | {step['achievedRps']} | "
            f"{step['outputTokensPerSec']} | {_fmt(_n(step['ttftMs'], 'p50'))} | "
            f"{_fmt(_n(step['ttftMs'], 'p95'))} | {_fmt(_n(step['e2eMs'], 'p50'))} | "
            f"{_fmt(_n(step['e2eMs'], 'p95'))} | {_fmt(_n(step['e2eMs'], 'p99'))} | "
            f"{_fmt(step['escalation']['ratePct'], ' %')} |")
    add("")

    if any(s.get("bedrockLatencyMs") for s in results["steps"]):
        add("### Server-reported latency vs ours")
        add("")
        add("| Concurrency | Bedrock p50 | Bedrock p95 | Overhead median |")
        add("|---:|---:|---:|---:|")
        for step in results["steps"]:
            block = step.get("bedrockLatencyMs")
            add(f"| {step['concurrency']} | {_fmt(_n(block, 'p50'))} | "
                f"{_fmt(_n(block, 'p95'))} | {_fmt(step.get('overheadMsMedian'), ' ms')} |")
        add("")
        add("Overhead is E2E minus Bedrock's own latency: network, TLS, SDK, Lambda. If it "
            "is large, the model is not the problem.")
        add("")
    else:
        add("### Server-reported latency vs ours")
        add("")
        add("**n/a on this lane.** The OpenAI-shape mantle response carries no equivalent of "
            "Converse's `metadata.metrics.latencyMs`, and `AWS/Bedrock` publishes 0 metrics "
            "for `google.gemma-4-26b-a4b` against 14 for `google.gemma-3-27b-it`. Latency "
            "attribution and the CloudWatch cross-check are both available on the fallback "
            "lane only (design 8.5, risk R3).")
        add("")

    add("## Error taxonomy (design 8.3)")
    add("")
    add("| Bucket | Count | Signals | Means |")
    add("|---|---:|---|---|")
    for bucket in BUCKETS:
        counts = totals["errorsByBucket"][bucket]
        signals = ", ".join(f"`{k}` × {v}" for k, v in counts.items()) or "—"
        add(f"| **{bucket}** | {sum(counts.values())} | {signals} | {BUCKET_MEANING[bucket]} |")
    add("")

    esc = totals["escalation"]
    add("## Escalation rate")
    add("")
    add(f"**{_fmt(esc['ratePct'], ' %')}** — {esc['escalated']} of {esc['parsed']} parsed "
        f"responses escalated to Stage 2.")
    add("")
    add("Design 9.3: this is the single most valuable number the PoC produces. It drives "
        "both the quota ask (design 6.2: at 15 % escalation, 800 RPS of ingest needs 58.3M "
        "of the 100M TPM ceiling rather than the doc's 86.4M) and the cost model (5 % → "
        "30 % moves daily cost by ~50 %).")
    add("")
    if esc["reasons"]:
        add("| Gate reason | Count |")
        add("|---|---:|")
        for reason, count in esc["reasons"].items():
            add(f"| `{reason}` | {count} |")
        add("")
    if esc.get("perSample"):
        add("| Sample | Requests | Escalated | Verdicts |")
        add("|---|---:|---:|---|")
        for sample_id, entry in esc["perSample"].items():
            verdicts = ", ".join(f"{k} × {v}" for k, v in entry["verdicts"].items())
            add(f"| `{sample_id}` | {entry['n']} | {entry['escalated']} "
                f"({_fmt(entry['ratePct'], ' %')}) | {verdicts} |")
        add("")

    add("## Tokens")
    add("")
    tok = totals["tokens"]
    add(f"- Input total {tok['inputTotal']:,} · output total {tok['outputTotal']:,} · "
        f"cache-read {tok['cacheReadTotal']:,}")
    add(f"- Aggregate {totals['outputTokensPerSec']} output tokens/sec over "
        f"{totals['measuredWallSeconds']} s of measured wall")
    add("")

    add("## Notes carried from the run")
    add("")
    for note in results.get("notes", []):
        add(f"- {note}")
    add("")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------------- charts

def build_charts(results, checks, verdict):
    """The arrays the Benchmark tab plots. Shape is the contract with T5.4.

    `percentiles.method` travels with the numbers so the UI can assert it matches its own
    rule instead of assuming: the prior art's driver and UI used different percentile
    definitions and disagreed at small n.
    """
    steps = results["steps"]
    totals = results["totals"]
    basis = next((s for s in reversed(steps)
                  if s["ok"] and not s["errorsByBucket"]["client_transport"]),
                 steps[-1] if steps else None)

    taxonomy_values = [sum(totals["errorsByBucket"][b].values()) for b in BUCKETS]

    return {
        "schemaVersion": 1,
        "generatedAt": results["generatedAt"],
        "meta": {
            "lane": results["run"]["lane"],
            "modelId": results["run"]["modelId"],
            "modelLabel": results["run"]["modelLabel"],
            "region": results["run"]["region"],
            "stage": results["run"]["stage"],
            "verdict": verdict,
            # The UI renders the banner from this field. Non-null means: no verdict.
            "banner": None if verdict else BANNER,
            "serverLatencyAvailable": bool(basis and basis.get("bedrockLatencyMs")),
        },
        "percentiles": {
            "method": results["percentileMethod"],
            "basisConcurrency": basis["concurrency"] if basis else None,
            "labels": ["p50", "p95", "p99"],
            "unit": "ms",
            "series": [
                {"name": "TTFT", "values": [_n(basis["ttftMs"], k) if basis else None
                                            for k in ("p50", "p95", "p99")]},
                {"name": "E2E", "values": [_n(basis["e2eMs"], k) if basis else None
                                           for k in ("p50", "p95", "p99")]},
            ],
            "targets": {"TTFT": results["targets"]["ttftMs"],
                        "E2E": (results["targets"]["stage1E2eP50Ms"]
                                if results["run"]["stage"] == "classify"
                                else results["targets"]["stage2E2eP95Ms"])},
        },
        "rpsVsConcurrency": {
            "labels": [s["concurrency"] for s in steps],
            "series": [
                {"name": "achieved RPS", "values": [s["achievedRps"] for s in steps]},
                {"name": "output tokens/sec", "values": [s["outputTokensPerSec"] for s in steps],
                 "axis": "right"},
                {"name": "error rate %", "values": [s["errorRatePct"] for s in steps],
                 "axis": "right"},
            ],
        },
        "errorTaxonomy": {
            "labels": list(BUCKETS),
            "values": taxonomy_values,
            "meaning": BUCKET_MEANING,
            "detail": {b: totals["errorsByBucket"][b] for b in BUCKETS},
            # A red banner in the UI whenever this is true (design 7.3).
            "clientSaturated": bool(totals["errorsByBucket"]["client_transport"]),
        },
        "escalation": {
            "ratePct": totals["escalation"]["ratePct"],
            "escalated": totals["escalation"]["escalated"],
            "parsed": totals["escalation"]["parsed"],
            "reasons": totals["escalation"]["reasons"],
            "perSample": totals["escalation"].get("perSample", {}),
            "assumedRatePct": 15,
        },
        "criteria": checks,
        "notes": results.get("notes", []),
    }


# ------------------------------------------------------------------------------------ main

def print_console(results, checks, verdict):
    run = results["run"]
    print(f"{run['modelLabel']}  ({run['modelId']})")
    print(f"lane {run['lane']} · {run['endpoint']}{run['path'] or ''} · {run['region']} · "
          f"stage {run['stage']} · {results['generatedAt']}")
    print()
    for check in checks:
        mark = {True: "PASS", False: "FAIL", None: "n/c "}[check["pass"]]
        print(f"  [{mark}] {check['criterion']:<52} target {check['target']:<22} "
              f"measured {check['measured']}")
    print()
    totals = results["totals"]
    for bucket in BUCKETS:
        counts = totals["errorsByBucket"][bucket]
        detail = ", ".join(f"{k}={v}" for k, v in counts.items()) if counts else "0"
        print(f"  {bucket:<18} {detail}")
    esc = totals["escalation"]
    print(f"\n  escalation rate    {_fmt(esc['ratePct'], '%')} "
          f"({esc['escalated']}/{esc['parsed']} parsed responses)")
    print()
    if verdict is None:
        print("=" * 78)
        print(BANNER)
        print("=" * 78)
    else:
        print(f"VERDICT: {verdict}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", nargs="?",
                    default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "results", "results.json"),
                    help="path to results.json from driver.py")
    ap.add_argument("--md", help="write the markdown here instead of stdout")
    ap.add_argument("--charts", help="write the UI's chart JSON here")
    ap.add_argument("--quiet", action="store_true", help="suppress the console summary")
    args = ap.parse_args()

    try:
        with open(args.results, encoding="utf-8") as fh:
            results = json.load(fh)
    except FileNotFoundError:
        raise SystemExit(f"no results file at {args.results} — run bench/driver.py first")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{args.results} is not valid JSON: {exc}")

    missing = [k for k in ("run", "steps", "totals", "targets", "percentileMethod")
               if k not in results]
    if missing:
        raise SystemExit(f"{args.results} is missing {', '.join(missing)}; it was not "
                         f"written by this version of driver.py")

    checks, verdict = grade(results)
    markdown = render_markdown(results, checks, verdict)

    if args.md:
        os.makedirs(os.path.dirname(os.path.abspath(args.md)), exist_ok=True)
        with open(args.md, "w", encoding="utf-8") as fh:
            fh.write(markdown)
        print(f"Wrote {args.md}")
    elif args.quiet:
        pass
    else:
        print(markdown)

    if args.charts:
        os.makedirs(os.path.dirname(os.path.abspath(args.charts)), exist_ok=True)
        with open(args.charts, "w", encoding="utf-8") as fh:
            json.dump(build_charts(results, checks, verdict), fh, indent=2)
            fh.write("\n")
        print(f"Wrote {args.charts}")

    if not args.quiet:
        print_console(results, checks, verdict)

    # 0 pass, 1 fail, 2 no verdict. See the module docstring.
    return 0 if verdict == "PASS" else (1 if verdict == "FAIL" else 2)


if __name__ == "__main__":
    sys.exit(main())
