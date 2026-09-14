#!/usr/bin/env python3
"""Load driver for the email-scan PoC. Calls Bedrock directly, on either lane.

Ported from an internal prior-art load driver: the ThreadPoolExecutor top-up
loop, the ramp, and -- critically -- the `total_max_attempts=1` retry-disable are that
script's, because they were already right. What is new is a second transport, output
parsing, a three-way error taxonomy, a warm-up discard, and results.json.

Never goes through CloudFront or API Gateway (design 8.1). The numbers here are the
model's and the network's, with zero gateway distortion, which is also why they stay
comparable when the UI's transport changes.

READ THIS BEFORE QUOTING ANY NUMBER FROM THIS TOOL
--------------------------------------------------
1. One laptop cannot reach the 700-800 RPS target, and no amount of concurrency fixes
   that. The wall is the *client*, not Bedrock: mantle resets connections from a single
   client at concurrency >= 50 (47.9% errors, all ConnectionReset), while bedrock-runtime
   sustained 150 cleanly from the same laptop in the same session (design 6.4). So this
   tool caps the mantle lane at 40 and the honest 700 RPS path is ~18 workers across ~5
   Fargate tasks (design 8.4, gated build step 11). Report single-client runs as
   "validated to N RPS, single-client-limited" -- never as the 700 RPS number.

2. Distinguish the three error buckets before drawing any conclusion. Across every load
   test run against this account, `InvocationThrottles` stayed at **zero** and every
   single failure was client-side transport exhaustion (design 6.3). A saturated load
   generator reported as a Bedrock limit is the one mistake that would discredit the
   whole benchmark, so `report.py` refuses to print a verdict when client_transport is
   non-empty.

3. Quotas differ per lane and neither is the doc's number. bedrock-runtime carries
   10,000 RPM = 167 RPS (non-adjustable) and one *combined* in+output TPM quota of 100M;
   the mantle endpoint carries **no Gemma quota entry at all** (design 6.1).

4. Achieved RPS is slightly understated on short runs: the top-up loop can over-submit up
   to `concurrency` requests past the deadline, and the drain of those in-flight requests
   is inside the measured wall. Fine at 60s+, worth knowing at 20s.

5. Run it in-region. From a laptop, transatlantic RTT turns TTFT<1s into a test of your
   ISP rather than of the model.

Usage
-----
  export AWS_PROFILE=aws
  python3.12 bench/driver.py --lane primary  --concurrency 4  --duration 20
  python3.12 bench/driver.py --lane primary  --steps 10,25,40 --duration 60
  python3.12 bench/driver.py --lane fallback --steps 25,50,100,150 --stage deep-scan
  python3.12 bench/driver.py --lane primary  --region eu-central-1 --concurrency 10
"""

import argparse
import csv
import itertools
import json
import math
import os
import statistics
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

# The prior art's sys.path trick, kept: bench/ imports the *same* backend modules the
# Lambda runs. schemas.py in particular is what makes the harness's numbers and the
# demo's numbers measurements of one request rather than two similar ones (design 8.1).
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.join(_HERE, os.pardir, "backend")
sys.path.insert(0, _BACKEND)

import boto3  # noqa: E402
from botocore.config import Config  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

import model_registry  # noqa: E402
import prompts  # noqa: E402
import schemas  # noqa: E402

# Per-worker concurrency ceilings, measured (design 6.4). The mantle number is a hard cap
# here rather than advice: at 50 the lane returned 47.9% errors, all ConnectionReset, so a
# default ramp through 50 just burns tokens manufacturing client-side failures.
SAFE_CONCURRENCY = {"mantle": 40, "runtime": 150}

# Default ramps, sized to stay inside those ceilings. Design's own ramp went 25/40/50 and
# 100/150/300; the steps above the ceiling are omitted because their result is already
# known and `--allow-unsafe-concurrency` exists for reproducing them deliberately.
DEFAULT_STEPS = {"mantle": (10, 25, 40), "runtime": (25, 50, 100, 150)}

LANE_ENDPOINT = {"primary": "mantle", "fallback": "runtime"}
ENDPOINT_LANE = {v: k for k, v in LANE_ENDPOINT.items()}

# Doc 6.5's success criteria, carried into results.json so report.py and the UI grade
# against the source guidance's numbers rather than against invented ones.
TARGETS = {
    "ttftMs": 1000,
    "stage1E2eP50Ms": 3000,
    "stage2E2eP95Ms": 10000,
    "errorRatePct": 0.1,
}

# design 4.4's escalation gate and the email rendering, imported rather than mirrored.
# Both live in schemas.py next to the json_schemas and the parser, so the Lambda and this
# harness run the same code: the escalation rate is design 9.3's headline number and it has
# to describe the gate production actually runs. This file used to carry its own copy and
# the two had already drifted -- the mirror's URL clause matched only `http://` and `www.`,
# where handler.py's also matched raw-IP hosts, punycode and host-with-path, so the rate
# reported here described a gate the Lambda does not run.
#
# Importing *handler.py* is still the wrong move, and that is why the gate moved rather
# than the import: handler.py runs a cold-start lane probe at module scope and a load
# harness must not depend on the Lambda's environment.
_CONFIDENCE_FLOOR = schemas.ESCALATE_BELOW_CONFIDENCE


# --------------------------------------------------------------------------- percentiles

def pct(values, p):
    """Nearest-rank percentile, ceil variant: idx = ceil(p/100 * n) - 1.

    THE UI MUST USE THIS EXACT RULE. There is one percentile implementation in this repo
    and this is it. In the prior art there were two -- driver.py used `int(round(...))`
    and RunHistory.tsx used `Math.ceil(...)` -- and they disagree at small n, which is
    exactly the regime a demo runs in: with n=10, p95 is index 9 under ceil and index 9
    under round, but with n=12 it is index 11 vs index 10, so the same run showed two
    different p95s depending on which panel you read. results.json carries
    `percentileMethod` so the UI can assert on it instead of assuming.
    """
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    idx = min(len(vals) - 1, max(0, math.ceil((p / 100) * len(vals)) - 1))
    return round(vals[idx], 1)


def _dist(values, percentiles=(50, 95, 99)):
    """A percentile block, or None when nothing in the sample was populated."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    block = {f"p{p}": pct(vals, p) for p in percentiles}
    block["max"] = round(max(vals), 1)
    block["n"] = len(vals)
    return block


# --------------------------------------------------------------------------------- corpus

def load_corpus(path, only=None):
    """Load bench/corpus/*.json, skipping the empty "paste your own" template.

    Skipping is explicit rather than incidental: sample 08 carries no body and no subject,
    the handler rejects that with a 400, and counting those would understate achieved RPS
    while filling the application bucket with our own test data.
    """
    if not os.path.isdir(path):
        raise SystemExit(f"corpus directory not found: {path}")

    samples, skipped = [], []
    for name in sorted(os.listdir(path)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(path, name), encoding="utf-8") as fh:
            sample = json.load(fh)
        email = sample.get("email") or {}
        if sample.get("placeholder") or not (email.get("body") or email.get("subject")):
            skipped.append(sample.get("id") or name)
            continue
        if only and sample.get("id") not in only:
            continue
        samples.append(sample)

    if not samples:
        raise SystemExit(f"no usable samples in {path}"
                         + (f" matching {sorted(only)}" if only else ""))
    return samples, skipped


# The gate and the renderer, under this file's own names. Aliases rather than wrappers:
# a wrapper is somewhere for the two to diverge again.
format_email = schemas.format_email
would_escalate = schemas.should_escalate


def build_user_text(sample, stage):
    """The user message, threaded exactly as handler._scan threads it.

    Stage 2 in production receives Stage 1's output as prior context, so measuring it
    without that context would measure a shorter prompt than the demo sends. The stand-in
    verdict comes from the sample's own `expected_verdict` so the input token count lands
    where a real pipeline would put it.
    """
    email_text = format_email(sample["email"])
    if stage != "deep-scan":
        return f"Analyze this email:\n{email_text}", email_text

    prior = {
        "verdict": sample.get("expected_verdict") or "suspicious",
        "confidence": 0.91,
        "signals": [
            {"signal": "sender", "detail": "envelope and display name disagree"},
            {"signal": "urgency", "detail": "deadline stated in the subject line"},
        ],
    }
    user_text = (
        f"Stage 1 classification: {json.dumps(prior)}\n\n"
        f"Analyze this email in detail:\n{email_text}"
    )
    return user_text, email_text


# ------------------------------------------------------------------------ error taxonomy

# design 8.3. Three buckets, always reported separately, because the doc's 429/503 model
# has no bucket for the failure that actually occurs here.
BUCKETS = ("service_throttle", "client_transport", "application")

_THROTTLE_STATUSES = {429, 503}
_THROTTLE_NAMES = (
    "ThrottlingException", "TooManyRequestsException", "ServiceUnavailableException",
    "ServiceQuotaExceededException", "rate_limit", "slow_down", "throttl",
)
# What actually fails in practice, on both lanes: ConnectionResetError(54) and
# NewConnectionError on mantle; ReadTimeout, ConnectionClosed and EndpointConnection on
# bedrock-runtime (design 6.3). Matched as substrings of the exception class names along
# the whole __cause__/__context__ chain, because the useful name is usually three frames
# below the one that surfaced.
_TRANSPORT_TOKENS = (
    "ConnectionReset", "ConnectionAborted", "ConnectionRefused", "BrokenPipe",
    "ReadTimeout", "ConnectTimeout", "ConnectionClosed", "EndpointConnection",
    "NewConnectionError", "RemoteDisconnected", "IncompleteRead", "ProtocolError",
    "SSLEOFError", "SSLZeroReturnError", "URLError", "TimeoutError", "socket.timeout",
)
# Checked before the transport scan so a *server-side* timeout is not misread as our
# socket dying: ModelTimeoutException contains "Timeout" but is Bedrock's, not ours.
_SERVICE_SIDE_NAMES = ("ModelTimeoutException", "ModelNotReadyException",
                       "InternalServerException")


def _exception_names(exc):
    """Class names along the exception chain, plus urllib's wrapped `reason`."""
    names, seen, node = [], set(), exc
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        names.append(type(node).__name__)
        reason = getattr(node, "reason", None)
        if isinstance(reason, BaseException):
            names.append(type(reason).__name__)
        node = node.__cause__ or node.__context__
    return names


def classify_error(exc):
    """Return (bucket, signal). Signal is the exception class or service error code.

    Anything none of the three buckets names lands in `application` with its class name
    preserved -- an unrecognised failure is our bug until proven otherwise, and a visibly
    mislabelled error is recoverable in a way that an invented fourth bucket the UI does
    not render would not be.
    """
    status = getattr(exc, "status", None)
    name = getattr(exc, "error_class", None)        # mantle_client.MantleError
    if isinstance(exc, ClientError):                # botocore, the runtime lane
        meta = exc.response.get("ResponseMetadata") or {}
        status = meta.get("HTTPStatusCode")
        name = (exc.response.get("Error") or {}).get("Code") or name

    names = ([name] if name else []) + _exception_names(exc)
    haystack = " ".join(names)

    if status in _THROTTLE_STATUSES or any(t.lower() in haystack.lower() for t in _THROTTLE_NAMES):
        return "service_throttle", name or names[0]
    if any(t in haystack for t in _SERVICE_SIDE_NAMES):
        return "application", name or names[0]
    if any(t in haystack for t in _TRANSPORT_TOKENS):
        return "client_transport", next(
            (n for n in names if any(t in n for t in _TRANSPORT_TOKENS)), names[0]
        )
    return "application", name or names[0]


# ------------------------------------------------------------------------------ collector

class Stats:
    """Thread-safe result accumulator for one step.

    Rows and error counts are only recorded for requests that *started* after the warm-up
    window closed. Discarding by start time rather than completion time is deliberate: a
    request issued at 4.9s on a cold connection is a cold measurement no matter when it
    finishes, and cold TTFT is ~4x warm (design 5.2), so one of those in a 20-request
    sample moves p95 on its own.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.rows = []
        self.discarded = 0
        self.buckets = {b: Counter() for b in BUCKETS}
        self.examples = {}
        self.escalation_reasons = Counter()
        self.connections_opened = 0

    def ok(self, row, measured):
        with self.lock:
            if not measured:
                self.discarded += 1
                return
            self.rows.append(row)
            if row["escalate_reason"]:
                self.escalation_reasons[row["escalate_reason"]] += 1

    def err(self, bucket, signal, detail, measured):
        with self.lock:
            if not measured:
                self.discarded += 1
                return
            self.buckets[bucket][signal] += 1
            self.examples.setdefault(f"{bucket}/{signal}", detail[:400])

    def connection_opened(self):
        with self.lock:
            self.connections_opened += 1

    @property
    def error_total(self):
        return sum(sum(c.values()) for c in self.buckets.values())


# ------------------------------------------------------------------------------ transport

# The connection-counting wrapper below needs the current step's Stats without threading it
# through mantle_client's signature. One dict, rebound per step, read under the Stats lock.
_CONNECTION_COUNTER = {}


def prepare_lane(endpoint, region, attempts):
    """Import the selected lane's client and, on the runtime lane, disable SDK retries.

    Only the selected lane is imported: the mantle lane must not pay to build a
    bedrock-runtime client it never calls, and a laptop with no `boto3` region default
    should not fail a mantle run.

    The retry-disable is the single most important line in this file, inherited from the
    prior art's `make_client`. converse_client._CONFIG asks for 6 attempts, which is right
    for production (doc 5.4) and wrong for a load test: SDK-internal retries silently
    absorb the throttle signal the harness exists to measure. botocore rejects 0, so 1 --
    one try, no retries -- is the floor. We seed the module's own per-region client cache
    rather than calling converse_stream ourselves, so the measured code path stays exactly
    the code path the Lambda runs.
    """
    if endpoint == "mantle":
        import mantle_client

        # Count TLS connections opened. Mantle reaps idle keep-alive sockets and
        # mantle_client._send re-sends once on a *reused* socket that died before any
        # response byte -- safe, but it means a single reset never reaches the
        # client_transport bucket, so that bucket is a lower bound on this lane.
        # connectionsOpened >> concurrency is the visible form of what got absorbed.
        original_connect = getattr(mantle_client, "_connect", None)
        if original_connect is not None:
            def counting_connect(host, _original=original_connect):
                conn = _original(host)
                counter = _CONNECTION_COUNTER.get("stats")
                if counter is not None:
                    counter.connection_opened()
                return conn

            mantle_client._connect = counting_connect
        mantle_client.warm(region)
        return mantle_client

    import converse_client

    client = boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=Config(
            retries={"total_max_attempts": max(1, attempts), "mode": "standard"},
            connect_timeout=5,
            read_timeout=25,
            tcp_keepalive=True,
            max_pool_connections=200,
        ),
    )
    cache = getattr(converse_client, "_clients", None)
    if not isinstance(cache, dict):
        raise SystemExit(
            "converse_client no longer exposes its per-region client cache (`_clients`), so "
            "SDK retries cannot be disabled. Refusing to run: with retries on, throttles "
            "are absorbed by the SDK and this harness would report a quota ceiling as "
            "clean latency (design 8.1)."
        )
    cache[region] = client
    return converse_client


def invoke(lane_client, endpoint, model, system_prompt, user_text, max_tokens,
           response_format, service_tier):
    """One inference call on whichever lane, returning the shared contract dict."""
    if endpoint == "mantle":
        return lane_client.chat(
            model,
            system_prompt,
            user_text,
            max_tokens,
            temperature=0,
            response_format=response_format,
            service_tier=service_tier,
        )
    return lane_client.converse(model, system_prompt, user_text, max_tokens, temperature=0)


def one_request(ctx, sample, stats, measure_from):
    """Issue one request, parse it, and record a row or an error.

    Parsing is not optional. The prior art never looked at the response body, so a run in
    which 100% of responses were malformed JSON reported 0% errors and a healthy p50
    (design correction 13). A response that cannot be parsed is an application failure,
    and on the fallback lane -- which has no json_schema -- it is the failure most likely
    to happen.
    """
    t_start = time.perf_counter()
    measured = t_start >= measure_from
    try:
        out = invoke(
            ctx["client"], ctx["endpoint"], ctx["model"], ctx["system_prompt"],
            sample["_user_text"], ctx["max_tokens"], ctx["response_format"],
            ctx["service_tier"],
        )
    except Exception as exc:  # noqa: BLE001 -- every failure must be bucketed, not raised
        bucket, signal = classify_error(exc)
        stats.err(bucket, signal, f"{type(exc).__name__}: {exc}", measured)
        return

    parsed, parse_error = schemas.extract_json(out["raw"])
    if parse_error:
        # A truncated response is unparseable for a different reason than a malformed one:
        # the token budget was too small, not the prompt wrong. Both are `application` --
        # our request either way -- but conflating them sends someone hunting the prompt
        # when the fix is maxTokens. Measured: the injection sample writes long enough
        # `signals[].detail` strings to hit Stage 1's 200-token budget (design 4.5).
        truncated = (out["stopReason"] or "").lower() in ("length", "max_tokens")
        stats.err("application",
                  "output_truncated_max_tokens" if truncated else "schema_parse_failure",
                  f"stopReason={out['stopReason']} | {parse_error} "
                  f"| raw[:200]={out['raw'][:200]!r}", measured)
        return

    reason = would_escalate(parsed, sample["_email_text"], parse_ok=True)
    timings, usage = out["timings"], out["usage"]
    stats.ok({
        "sample_id": sample["id"],
        "ttft_ms": timings["ttftMs"],
        "e2e_ms": timings["e2eMs"],
        "bedrock_ms": timings["bedrockLatencyMs"],
        "overhead_ms": timings["overheadMs"],
        "otps": timings["otps"],
        "input_tokens": usage["inputTokens"],
        "output_tokens": usage["outputTokens"],
        "cache_read_tokens": usage["cacheReadInputTokens"],
        "stop_reason": out["stopReason"],
        "verdict": parsed.get("verdict"),
        "escalated": reason is not None,
        "escalate_reason": reason,
        "parse_ok": True,
        "concurrency": ctx["concurrency"],
    }, measured)


# ----------------------------------------------------------------------------- one step

def run_step(ctx, samples, concurrency, duration, warmup):
    """Hold `concurrency` requests in flight for `duration`, minus a warm-up window.

    The top-up loop is the prior art's: submit until `concurrency` futures are live, reap
    the done ones, repeat. It over-submits by up to `concurrency` past the deadline and
    the drain is inside the measured wall, so achieved RPS is slightly understated -- see
    docstring note 4.
    """
    ctx["concurrency"] = concurrency
    stats = Stats()
    _CONNECTION_COUNTER["stats"] = stats

    cursor = itertools.count()
    cursor_lock = threading.Lock()

    def next_sample():
        # Round-robin over the corpus so the escalation rate is measured across the whole
        # mix rather than against one email, and so input token counts vary as they would.
        with cursor_lock:
            return samples[next(cursor) % len(samples)]

    t_phase = time.perf_counter()
    measure_from = t_phase + warmup
    stop_at = t_phase + duration
    print(f"\n  concurrency={concurrency} for {duration}s "
          f"(first {warmup:.0f}s discarded as warm-up) ...", flush=True)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = set()
        while time.perf_counter() < stop_at:
            while len(futures) < concurrency:
                futures.add(pool.submit(one_request, ctx, next_sample(), stats, measure_from))
            done = {f for f in futures if f.done()}
            futures -= done
            if not done:
                time.sleep(0.01)
        for future in futures:
            future.result()

    measured_wall = time.perf_counter() - measure_from
    _CONNECTION_COUNTER.pop("stats", None)
    step = summarize(stats, measured_wall, concurrency, ctx["endpoint"])
    print_step(step)
    return step, stats.rows


def summarize(stats, measured_wall, concurrency, endpoint):
    """Roll one step's rows and buckets into the results.json step shape."""
    rows = stats.rows
    n_ok, n_err = len(rows), stats.error_total
    total = n_ok + n_err
    wall = max(measured_wall, 1e-6)
    out_tokens = sum(r["output_tokens"] for r in rows)
    escalated = sum(1 for r in rows if r["escalated"])

    step = {
        "concurrency": concurrency,
        "requests": total,
        "ok": n_ok,
        "errors": n_err,
        "errorRatePct": round(100 * n_err / total, 3) if total else None,
        "discardedWarmup": stats.discarded,
        "measuredWallSeconds": round(measured_wall, 2),
        "achievedRps": round(n_ok / wall, 2),
        "achievedRpm": round(n_ok / wall * 60),
        "outputTokensPerSec": round(out_tokens / wall, 1),
        "ttftMs": _dist([r["ttft_ms"] for r in rows]),
        "e2eMs": _dist([r["e2e_ms"] for r in rows]),
        "bedrockLatencyMs": _dist([r["bedrock_ms"] for r in rows], percentiles=(50, 95)),
        "otps": _dist([r["otps"] for r in rows], percentiles=(50,)),
        "overheadMsMedian": None,
        "tokens": {
            "inputTotal": sum(r["input_tokens"] for r in rows),
            "outputTotal": out_tokens,
            "cacheReadTotal": sum(r["cache_read_tokens"] for r in rows),
            "inputMean": (round(statistics.mean([r["input_tokens"] for r in rows]), 1)
                          if rows else None),
            "outputMean": (round(statistics.mean([r["output_tokens"] for r in rows]), 1)
                           if rows else None),
        },
        "escalation": {
            "parsed": n_ok,
            "escalated": escalated,
            "ratePct": round(100 * escalated / n_ok, 2) if n_ok else None,
            "reasons": dict(stats.escalation_reasons.most_common()),
        },
        "verdicts": dict(Counter(r["verdict"] for r in rows).most_common()),
        # All three buckets always present, two of them usually 0. A bucket that only
        # appears when non-empty is a bucket a reader forgets to look for.
        "errorsByBucket": {b: dict(stats.buckets[b].most_common()) for b in BUCKETS},
        "errorExamples": dict(stats.examples),
    }

    overheads = [r["overhead_ms"] for r in rows if r["overhead_ms"] is not None]
    if overheads:
        step["overheadMsMedian"] = round(statistics.median(overheads), 1)
    if endpoint == "mantle":
        # See prepare_lane: churn here is the early-warning form of client_transport.
        step["connectionsOpened"] = stats.connections_opened
    return step


def print_step(step):
    print(f"    requests   {step['requests']}  ({step['ok']} ok, {step['errors']} errors, "
          f"{step['errorRatePct']}% error rate, {step['discardedWarmup']} discarded as warm-up)")
    print(f"    throughput {step['achievedRps']} RPS   ({step['achievedRpm']} RPM)   "
          f"{step['outputTokensPerSec']} output tok/s")
    for label, key in (("E2E ", "e2eMs"), ("TTFT", "ttftMs")):
        block = step[key]
        if block:
            print(f"    {label}       P50 {block['p50']}ms   P95 {block['p95']}ms   "
                  f"P99 {block['p99']}ms   max {block['max']}ms")
    if step["bedrockLatencyMs"]:
        print(f"    Bedrock    P50 {step['bedrockLatencyMs']['p50']}ms   "
              f"P95 {step['bedrockLatencyMs']['p95']}ms  (server-side, excludes network)")
        print(f"    overhead   ~{step['overheadMsMedian']}ms median "
              f"(network + SDK; large means the model isn't the bottleneck)")
    else:
        print("    Bedrock    n/a on this lane (no server-reported latency in the "
              "OpenAI-shape response)")
    esc = step["escalation"]
    print(f"    escalation {esc['ratePct']}%  ({esc['escalated']}/{esc['parsed']} parsed "
          f"responses)   {esc['reasons'] or '{}'}")
    if "connectionsOpened" in step:
        print(f"    conns      {step['connectionsOpened']} TLS connections opened "
              f"(>> concurrency means keep-alive sockets are being reset)")
    for bucket in BUCKETS:
        counts = step["errorsByBucket"][bucket]
        detail = ", ".join(f"{k}={v}" for k, v in counts.items()) if counts else "0"
        print(f"    {bucket:<17} {detail}")
    if step["errorsByBucket"]["client_transport"]:
        print("               ^ CLIENT-SIDE. Our load generator saturated, not Bedrock. "
              "Never quote this as a service limit (design 8.3).")
    if step["errorsByBucket"]["service_throttle"]:
        print("               ^ a real Bedrock capacity limit, and a legitimate quota "
              "data point.")


# -------------------------------------------------------------------------------- totals

def totals_from_rows(rows, steps):
    """Corpus-wide aggregate. The escalation rate here is the headline number.

    Design 9.3: measuring the real escalation rate is the single most valuable number this
    PoC produces, because it drives both the quota ask (design 6.2) and the cost model
    (design 9.3, where 5% -> 30% moves daily cost by ~50%). Latency percentiles here mix
    concurrency levels; quote the per-step rows for those.
    """
    wall = sum(s["measuredWallSeconds"] for s in steps) or 1e-6
    n_ok = len(rows)
    escalated = sum(1 for r in rows if r["escalated"])
    reasons = Counter(r["escalate_reason"] for r in rows if r["escalate_reason"])
    per_sample = {}
    for row in rows:
        entry = per_sample.setdefault(
            row["sample_id"], {"n": 0, "escalated": 0, "verdicts": Counter()})
        entry["n"] += 1
        entry["escalated"] += 1 if row["escalated"] else 0
        entry["verdicts"][row["verdict"]] += 1

    buckets = {b: Counter() for b in BUCKETS}
    for step in steps:
        for bucket in BUCKETS:
            buckets[bucket].update(step["errorsByBucket"][bucket])
    n_err = sum(sum(c.values()) for c in buckets.values())

    return {
        "requests": n_ok + n_err,
        "ok": n_ok,
        "errors": n_err,
        "errorRatePct": round(100 * n_err / (n_ok + n_err), 3) if (n_ok + n_err) else None,
        "measuredWallSeconds": round(wall, 2),
        "outputTokensPerSec": round(sum(r["output_tokens"] for r in rows) / wall, 1),
        "ttftMs": _dist([r["ttft_ms"] for r in rows]),
        "e2eMs": _dist([r["e2e_ms"] for r in rows]),
        "bedrockLatencyMs": _dist([r["bedrock_ms"] for r in rows], percentiles=(50, 95)),
        "tokens": {
            "inputTotal": sum(r["input_tokens"] for r in rows),
            "outputTotal": sum(r["output_tokens"] for r in rows),
            "cacheReadTotal": sum(r["cache_read_tokens"] for r in rows),
        },
        "escalation": {
            "parsed": n_ok,
            "escalated": escalated,
            "ratePct": round(100 * escalated / n_ok, 2) if n_ok else None,
            "reasons": dict(reasons.most_common()),
            "perSample": {
                sid: {
                    "n": e["n"],
                    "escalated": e["escalated"],
                    "ratePct": round(100 * e["escalated"] / e["n"], 1) if e["n"] else None,
                    "verdicts": dict(e["verdicts"].most_common()),
                }
                for sid, e in sorted(per_sample.items())
            },
        },
        "errorsByBucket": {b: dict(buckets[b].most_common()) for b in BUCKETS},
        "note": "Percentiles here mix concurrency levels; quote the per-step rows instead.",
    }


# ----------------------------------------------------------------------------------- main

def parse_steps(raw, endpoint, single, allow_unsafe):
    """Turn --steps / --concurrency into a list, capping the mantle lane at 40."""
    if raw:
        try:
            steps = [int(s) for s in raw.replace(" ", "").split(",") if s]
        except ValueError:
            raise SystemExit(f"--steps must be a comma-separated list of integers, got {raw!r}")
    else:
        steps = [single]
    if any(s < 1 for s in steps):
        raise SystemExit("--steps values must be >= 1")

    ceiling = SAFE_CONCURRENCY[endpoint]
    if allow_unsafe:
        return steps
    capped = []
    for step in steps:
        if step > ceiling:
            print(f"  ! concurrency {step} capped to {ceiling} on the {endpoint} lane "
                  f"(design 6.4: measured ceiling; pass --allow-unsafe-concurrency to "
                  f"reproduce the failure deliberately)")
            step = ceiling
        if step not in capped:
            capped.append(step)
    return capped


def _resolve(model_id, stage, region):
    try:
        return model_registry.resolve(model_id, stage, region=region)
    except model_registry.UnsupportedRegionError as exc:
        # A refusal, not a fallback: silently retargeting the region would make the region
        # axis of the benchmark a lie, and the failure it hides is a 404 (design 3.1).
        raise SystemExit(f"{exc}")


def resolve_target(lane, model_id, region, stage):
    """Resolve (model, endpoint) from --lane and/or --model.

    `--model` alone is sufficient: the lane follows from the registry entry's `endpoint`,
    because the (model, endpoint, path) tuple is mutually exclusive (design 3.2). Giving
    both and having them disagree is refused rather than resolved -- silently preferring
    either one would report a number from a transport nobody asked for.
    """
    if model_id:
        model = _resolve(model_id, stage, region)
        endpoint = model["endpoint"]
        if lane and LANE_ENDPOINT[lane] != endpoint:
            raise SystemExit(
                f"--lane {lane} means the {LANE_ENDPOINT[lane]} endpoint, but {model['id']} is "
                f"a {endpoint}-lane model. Drop one of the two flags."
            )
        return model, endpoint

    wanted = LANE_ENDPOINT[lane or "primary"]
    model = _resolve(None, stage, region)          # the stage default from the registry
    if model["endpoint"] != wanted:
        candidates = [m for m in model_registry.MODELS if m["endpoint"] == wanted]
        if not candidates:
            raise SystemExit(f"no model in the registry uses the {wanted} endpoint")
        model = _resolve(candidates[0]["id"], stage, region)
    return model, model["endpoint"]


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lane", choices=sorted(LANE_ENDPOINT), default=None,
                    help="primary = Gemma 4 on bedrock-mantle; fallback = Gemma 3 on "
                         "bedrock-runtime Converse. Default: primary, or the model's own "
                         "lane when --model is given")
    ap.add_argument("--model", help="registry model id; implies its own lane")
    ap.add_argument("--region", default=model_registry.DEFAULT_REGION,
                    help="us-east-1 or eu-central-1 for Gemma 4; eu-west-1 is a 404 there")
    ap.add_argument("--stage", choices=["classify", "deep-scan"], default="classify")
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--steps", help="comma-separated concurrency ramp, e.g. 10,25,40")
    ap.add_argument("--duration", type=int, default=30, help="seconds per step, warm-up included")
    ap.add_argument("--warmup", type=float, default=5.0,
                    help="seconds discarded at the start of each step; cold TTFT is ~4x warm")
    ap.add_argument("--max-tokens", type=int, default=None)
    ap.add_argument("--attempts", type=int, default=1,
                    help="total SDK attempts per request on the runtime lane; keep at 1 so "
                         "throttles stay visible")
    ap.add_argument("--service-tier", choices=["flex"], default=None,
                    help="mantle only; halves cost and measured slower, so it is a cost lever")
    ap.add_argument("--corpus", default=os.path.join(_HERE, "corpus"))
    ap.add_argument("--sample", action="append", help="restrict to these corpus ids (repeatable)")
    ap.add_argument("--out", default=os.path.join(_HERE, "results", "results.json"))
    ap.add_argument("--csv", help="also write per-request rows here")
    ap.add_argument("--allow-unsafe-concurrency", action="store_true",
                    help="lift the measured per-lane concurrency cap (mantle fails at 50)")
    args = ap.parse_args()

    if args.warmup >= args.duration:
        raise SystemExit(
            f"--warmup {args.warmup}s must be less than --duration {args.duration}s, or every "
            f"request is discarded and the run measures nothing"
        )

    model, endpoint = resolve_target(args.lane, args.model, args.region, args.stage)
    lane = ENDPOINT_LANE[endpoint]
    steps = parse_steps(args.steps, endpoint, args.concurrency, args.allow_unsafe_concurrency)
    # Must track backend/handler.py's _MAX_TOKENS or the harness stops measuring the request the
    # demo actually sends. classify is 320 rather than the design's 200 because 200 truncated
    # Gemma 4 mid-JSON on ~7% of samples that returned all 5 ranked signals.
    max_tokens = args.max_tokens or (320 if args.stage == "classify" else 600)
    if args.service_tier and endpoint != "mantle":
        raise SystemExit("--service-tier is a mantle-lane parameter; Converse has no equivalent")

    samples, skipped = load_corpus(args.corpus, set(args.sample) if args.sample else None)
    # The harness always measures the default prompt: a benchmark of a prompt someone edited
    # in a browser is not comparable to any other run. Hence `_`, never an override.
    system_prompt, _ = prompts.system_for(args.stage, model)
    response_format = (
        schemas.response_format(args.stage) if model["supports_json_schema"] else None
    )
    for sample in samples:
        sample["_user_text"], sample["_email_text"] = build_user_text(sample, args.stage)

    print(f"Lane       {lane}  [{endpoint}{model['path'] or ''}]")
    print(f"Model      {model['label']}  ({model['id']})")
    print(f"Region     {model['region']}   style={model['style']}   "
          f"json_schema={'sent' if response_format else 'unavailable on this lane'}")
    print(f"Stage      {args.stage}   maxTokens={max_tokens}   attempts={args.attempts}"
          + (f"   service_tier={args.service_tier}" if args.service_tier else ""))
    print(f"Corpus     {len(samples)} samples from {args.corpus}"
          + (f"   (skipped: {', '.join(skipped)})" if skipped else ""))
    print(f"Steps      {steps}   duration={args.duration}s   warmup={args.warmup}s")
    print("\nNOTE: the client, not Bedrock, is the ceiling on one machine. Read the module")
    print("      docstring before quoting any of these numbers.")

    lane_client = prepare_lane(endpoint, model["region"], args.attempts)

    step_results, all_rows = [], []
    for concurrency in steps:
        step, rows = run_step({
            "client": lane_client,
            "endpoint": endpoint,
            "model": model,
            "system_prompt": system_prompt,
            "max_tokens": max_tokens,
            "response_format": response_format,
            "service_tier": args.service_tier,
        }, samples, concurrency, args.duration, args.warmup)
        step_results.append(step)
        all_rows += rows
        if step["errorsByBucket"]["service_throttle"]:
            print(f"\n  Service throttling at concurrency={concurrency}. Stopping the ramp "
                  f"— this is a real capacity ceiling and a quota data point.")
            break
        if step["errorsByBucket"]["client_transport"]:
            print(f"\n  Client-transport failures at concurrency={concurrency}. Stopping the "
                  f"ramp — the generator is saturated, so higher steps would measure our "
                  f"laptop rather than Bedrock (design 6.4).")
            break

    results = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # The UI must use the same rule. See pct().
        "percentileMethod": "nearest-rank-ceil: idx = ceil(p/100 * n) - 1",
        "run": {
            "lane": lane,
            "endpoint": endpoint,
            "path": model["path"],
            "modelId": model["id"],
            "modelLabel": model["label"],
            "region": model["region"],
            "stage": args.stage,
            "maxTokens": max_tokens,
            "temperature": 0,
            "jsonSchemaSent": bool(response_format),
            "serviceTier": args.service_tier,
            "sdkAttempts": args.attempts,
            "steps": steps,
            "durationSeconds": args.duration,
            "warmupSeconds": args.warmup,
            "corpusDir": os.path.relpath(args.corpus, os.path.dirname(_HERE)),
            "corpusSamples": [s["id"] for s in samples],
            "corpusSkipped": skipped,
            "throughVia": "bedrock direct (never CloudFront or API Gateway)",
        },
        "targets": TARGETS,
        "steps": step_results,
        "totals": totals_from_rows(all_rows, step_results),
        "notes": [
            "Called Bedrock directly; no CloudFront and no API Gateway, so there is zero "
            "gateway distortion in these numbers (design 8.1).",
            "SDK retries disabled (total_max_attempts=1) so throttles stay visible instead "
            "of being absorbed silently (design 8.1).",
            f"First {args.warmup}s of each step discarded: cold TTFT is ~1.6s against ~0.4s "
            f"warm, a 4x effect that would poison p50 (design 5.2).",
            "Achieved RPS is slightly understated: the top-up loop over-submits up to "
            "`concurrency` requests past the deadline and their drain is inside the "
            "measured wall.",
            "The escalation gate is design 4.4's, imported from backend/schemas.py -- the "
            "same function object the Lambda runs, over the same email rendering, so this "
            "escalation rate describes production rather than a mirror of it.",
        ],
    }
    if endpoint == "mantle":
        results["notes"].append(
            "client_transport is a LOWER BOUND on this lane: mantle_client._send re-sends "
            "once on a reused keep-alive socket that died before any response byte, so a "
            "single reaped-socket reset never reaches the bucket. Watch connectionsOpened "
            "against concurrency for the churn it hides."
        )
        results["notes"].append(
            "bedrockLatencyMs and overheadMs are null on this lane: the OpenAI-shape "
            "response carries no equivalent of Converse's metadata.metrics.latencyMs, and "
            "no x-amzn-* header supplies it. Render as \"n/a\", never as 0 (design 8.5)."
        )

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
        fh.write("\n")
    print(f"\nWrote {args.out}")

    if args.csv and all_rows:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(all_rows[0].keys()))
            writer.writeheader()
            writer.writerows(all_rows)
        print(f"Wrote {len(all_rows)} per-request rows to {args.csv}")

    esc = results["totals"]["escalation"]
    print(f"\nEscalation rate {esc['ratePct']}% across {len(samples)} corpus samples "
          f"({esc['escalated']}/{esc['parsed']}). Design 9.3: this is the single most "
          f"valuable number here — it drives both the quota ask and the cost model.")
    print(f"Next: python3.12 bench/report.py {args.out}")


if __name__ == "__main__":
    main()
