"""Bedrock ConverseStream wrapper with latency instrumentation -- the fallback lane.

Gemma 3 27B on bedrock-runtime, called with the **plain** base model ID: no `us.` or `eu.`
prefix exists for Gemma and no Gemma inference profile exists in any region (design 3.2,
3.3), so there is nothing else this identifier could be.

We always stream, even though the HTTP API returns one JSON blob. Streaming is the only
way to observe TTFT, which is a first-class success criterion in the source doc (<1s), and
it's what production would use anyway.

Timing model:
    t0       request issued
    t_first  first contentBlockDelta with actual text  -> ttft
    t_end    stream exhausted                          -> e2e
    otps     output tokens / (t_end - t_first)

Identical to the mantle lane's model, down to "with actual text": both lanes emit empty
deltas around the content, and the A/B toggle compares TTFT between the two, so a
different absence rule on each side would make the headline comparison a measurement of
our own inconsistency.

We also surface Bedrock's own `metrics.latencyMs` from the stream metadata. Having both
that and our measured e2e lets us attribute latency between the model and everything else
(network, Lambda, TLS) -- the first question anyone asks when a number looks wrong. This
is the only lane where that attribution is possible at all: the OpenAI-shape mantle
response carries no equivalent field, and AWS/Bedrock publishes 14 metrics for this model
against 0 for Gemma 4 (design 8.5).

Two levers the source doc asks for are deliberately *absent* here rather than gated off:

    cachePoint         -> AccessDeniedException on Gemma 3, reproduced at ~500, ~1200 and
                          ~2500-token prefixes, so it is model-level (design 5.3)
    performanceConfig  -> ValidationException for every candidate model in us-east-1,
                          this one included (design 5.4)

Both were branches in the prior art. Deleting beats gating: a flag that can only ever be
False is a flag someone eventually sets, and neither failure is a soft degrade.

Output shape is prompt-enforced only on this lane -- Converse exposes no `response_format`
-- so Gemma 3 fences its JSON and `schemas.extract_json` is load-bearing here, not
defensive theatre.


NOTE ON THE FILENAME: this module is deliberately NOT called `runtime_client`.
Inside the AWS Lambda python3.12 environment the name `runtime_client` is already in
`sys.modules` -- the runtime interface client aliases it to a module whose __name__ is
`runtime` -- and a cached entry beats /var/task on sys.path. So `import runtime_client`
silently returned AWS's module and every fallback-lane call died with
"AttributeError: module 'runtime' has no attribute 'converse'" *only once deployed*;
it worked locally throughout. Do not rename this back.
"""

import time

import boto3
from botocore.config import Config

# Re-exported, never redefined: there is exactly one JSON parser in the repo and it lives
# beside the schemas so bench/ gets both from a single import (tasks T1.2). This is the
# lane that actually needs it.
from schemas import extract_json  # noqa: F401  (re-export for this lane's callers)

# Retry/timeout config per source doc 5.4, extended for streaming reads. read_timeout is
# 25s rather than the prior art's 90s: API Gateway hard-caps at 30s, so a longer read
# timeout only guarantees a bare gateway 504 while the Lambda keeps billing. The chain is
# read 25s < Lambda 29s < API Gateway 30s < CloudFront 120s, innermost limit first.
_CONFIG = Config(
    retries={"total_max_attempts": 6, "mode": "standard"},
    connect_timeout=5,
    read_timeout=25,
    tcp_keepalive=True,
)

# One client per region, built at module scope so warm Lambda invocations reuse
# HTTP connections (source doc 4.5, "connection pooling").
_clients = {}


def _client(region):
    if region not in _clients:
        _clients[region] = boto3.client("bedrock-runtime", region_name=region, config=_CONFIG)
    return _clients[region]


def warm(region=None):
    """Pre-build a client at cold start so the first request doesn't pay for it.

    Cold TTFT is ~1.6s against ~0.4s warm, a 4x effect, which is why this is called at
    import time by the handler rather than left to the first user-visible scan
    (design 5.2, 7.6).
    """
    from model_registry import REGION

    _client(region or REGION)


def converse(model, system_prompt, user_text, max_tokens, temperature=0):
    """Invoke a model via ConverseStream and return content plus timing/usage.

    `model` is a registry entry from model_registry.MODELS. `max_tokens` is caller-supplied
    (200 for Stage 1, 600 for Stage 2 -- design 4.5) because the token budget belongs to
    the stage, not to the client. `temperature` defaults to 0: at 0.1 repeated runs
    disagree with each other in front of an audience.

    Returns the shared lane contract -- raw / stopReason / usage / timings / timeline. The
    mantle lane returns the same keys, and that is what keeps the handler and the UI
    transport-agnostic.
    """
    kwargs = {
        "modelId": model["id"],
        "messages": [{"role": "user", "content": [{"text": user_text}]}],
        # The email is never spliced into the system block; it arrives as the user message
        # above, so the injection guard cannot be displaced by scanned content (design 4.2).
        "system": [{"text": system_prompt}],
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
    }

    t0 = time.perf_counter()
    t_first = None
    chunks = []
    timeline = []
    usage = {}
    bedrock_latency_ms = None
    stop_reason = None

    response = _client(model["region"]).converse_stream(**kwargs)
    for event in response["stream"]:
        if "contentBlockDelta" in event:
            text = event["contentBlockDelta"]["delta"].get("text", "")
            # Only a delta carrying actual text starts the clock, which is what the mantle
            # lane does too. Verified live: this lane emits contentBlockDeltas whose text is
            # "" both before and after the content, and counting one of those measures the
            # stream handshake rather than the first token. The A/B toggle compares TTFT
            # across the two lanes, so the two definitions have to be the same definition or
            # the comparison is rigged by up to one delta interval (~200ms here).
            if text:
                now = time.perf_counter()
                if t_first is None:
                    t_first = now
                chunks.append(text)
                # Per-delta arrival times, not just the concatenated text. The UI replays
                # the stream with the real recorded inter-token gaps (design 7.2), and those
                # gaps cannot be reconstructed after the loop has discarded them. Empty
                # deltas are left out for the same reason they don't start the clock: an
                # empty frame in the replay is a pause the model never took.
                timeline.append({"tMs": round((now - t0) * 1000, 1), "text": text})
        elif "messageStop" in event:
            stop_reason = event["messageStop"].get("stopReason")
        elif "metadata" in event:
            usage = event["metadata"].get("usage", {}) or {}
            bedrock_latency_ms = event["metadata"].get("metrics", {}).get("latencyMs")

    t_end = time.perf_counter()
    raw = "".join(chunks)

    ttft_ms = round((t_first - t0) * 1000, 1) if t_first is not None else None
    e2e_ms = round((t_end - t0) * 1000, 1)
    in_tokens = usage.get("inputTokens", 0)
    out_tokens = usage.get("outputTokens", 0)
    gen_seconds = (t_end - t_first) if t_first is not None else 0
    otps = round(out_tokens / gen_seconds, 1) if gen_seconds > 0 and out_tokens else None

    return {
        "raw": raw,
        "stopReason": stop_reason,
        "usage": {
            "inputTokens": in_tokens,
            "outputTokens": out_tokens,
            # Summed rather than defaulted to 0 when the metadata event never arrived, which
            # is what the mantle lane does with the same field. Both lanes must be absent in
            # the same way: a 0 total beside non-zero in/out tokens is a contradiction the UI
            # would render as fact.
            "totalTokens": usage.get("totalTokens") or (in_tokens + out_tokens),
            # Structurally always 0 on this lane -- cachePoint is an AccessDeniedException
            # here, so there is nothing to read or write. Reported anyway so both lanes
            # hand the UI one shape and it needs no lane awareness.
            "cacheReadInputTokens": usage.get("cacheReadInputTokens", 0),
            "cacheWriteInputTokens": usage.get("cacheWriteInputTokens", 0),
        },
        "timings": {
            "ttftMs": ttft_ms,
            "e2eMs": e2e_ms,
            "bedrockLatencyMs": bedrock_latency_ms,
            # Everything that isn't the model: network, TLS, SDK, Lambda. Tested with
            # `is not None` rather than truthiness -- a server-reported 0 is a measurement,
            # not an absence, and the prior art reported it as one.
            "overheadMs": (
                round(e2e_ms - bedrock_latency_ms, 1) if bedrock_latency_ms is not None else None
            ),
            "otps": otps,
        },
        "timeline": timeline,
    }
