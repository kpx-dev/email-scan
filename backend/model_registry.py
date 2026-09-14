"""Model catalog and the routing rule for the two lanes.

Model, endpoint and path are **mutually exclusive** (design 3.2). This is the single
most important operational fact in the build, and a single wrong path segment is a 400
that reads like a model-availability problem -- so the whole
`(model, endpoint, path, region)` tuple lives here and nowhere else:

  google.gemma-4-26b-a4b  mantle   /openai/v1/chat/completions  -> 200, 424ms
  google.gemma-4-26b-a4b  mantle   /v1/chat/completions         -> 400 "isn't supported on this route"
  google.gemma-4-26b-a4b  runtime  Converse                     -> ValidationException, invalid identifier
  google.gemma-3-27b-it   runtime  Converse / ConverseStream     -> 200, plain base ID
  google.gemma-3-27b-it   mantle   /openai/v1/chat/completions  -> 400 "isn't supported on this route"

On mantle, `/openai/v1/*` carries the Gemma 4 family and `/v1/*` carries Gemma 3. Auth
for both is plain SigV4 with signing service name `bedrock` -- no API key. Gemma 4 is
invisible to `bedrock list-foundation-models` and has no foundation-model ARN at all,
which is why a first pass concludes it does not exist.

`regions` is empirically true rather than aspirational. Verified live:
  google.gemma-4-26b-a4b  us-east-1    -> 200, 489ms
  google.gemma-4-26b-a4b  eu-central-1 -> 200, 758ms
  google.gemma-4-26b-a4b  eu-west-1    -> 404 not_found_error
  google.gemma-3-27b-it   us-east-1    -> 200
  google.gemma-3-27b-it   eu-central-1 -> ValidationException, invalid model identifier
  google.gemma-3-27b-it   eu-west-1    -> 200
The prior art defaulted to eu-west-1, which would have silently broken the primary lane.

THE TWO LANES HAVE DISJOINT EU REGIONS, and this is a real constraint on the
EU story rather than a quirk of ours. `aws bedrock list-foundation-models --region
eu-central-1` returns ZERO Gemma models of any size, so the fallback lane cannot run in
Frankfurt; and the primary lane 404s in Ireland. There is therefore NO single EU region
where both lanes work:
  Frankfurt (eu-central-1)  primary only
  Ireland   (eu-west-1)     fallback only
  N. Virginia (us-east-1)   both  <- the only region that runs the full A-B comparison
A/B-ing the two models in the EU means running them in two different regions, which
undermines any latency comparison drawn there. Raise this with the account team alongside
the residency questions in design 10.

`DEFAULT_REGION` is eu-central-1, an EU default chosen deliberately and with a cost:
  * Both stage defaults are the PRIMARY model, and Frankfurt is the only EU region it
    answers in. eu-west-1 would 404 the default lane on the first scan.
  * It is measurably slower from this account than N. Virginia -- 758ms against 489ms on
    the same model and prompt. That is the price of the EU default, it is stated in the UI
    next to the region selector, and it is not hidden behind an average.
  * The FALLBACK model cannot run here at all. `_startup` therefore logs a fallback-lane
    warning on every cold start in this default, by design: the A/B toggle needs
    us-east-1 or eu-west-1, and a warning is the honest way to say so.

EVERY MODEL HERE IS SERVED BY AMAZON BEDROCK. `served_via` carries the exact host so the
UI can state it rather than assert it. No Google Cloud, Vertex AI, AI Studio or
generativelanguage.googleapis.com endpoint is contacted by any code path in this repo:
`google.` is the Bedrock model-ID namespace for the provider, nothing more. The two hosts
below are the only inference endpoints that exist here.

The `supports_*` flags are set from measured behaviour, not documentation, and every
False carries the verbatim exception in `unsupported_reason` -- the UI greys the toggle
out and shows that text on hover, because showing the real error is more
convincing than a paragraph (design 5.3, 5.4). The flags are also what stop the code
hard-failing on levers the source doc asked for:
  cachePoint on Gemma 3           -> hard AccessDeniedException, not a soft degrade
  performanceConfig=optimized     -> ValidationException on every candidate in us-east-1
  Gemma 4 prompt caching          -> automatic and opportunistic (42% hit rate measured),
                                     no parameter to send, and no latency separation at all
                                     between hits and misses -- a cost lever, not a latency one
"""

# EU by default. See the module docstring: this is the only EU region the default (primary)
# model answers in, and it costs ~270ms against us-east-1.
DEFAULT_REGION = "eu-central-1"

# The region that runs both lanes, so an A/B needs it explicitly. Named rather than spelled
# inline in five places, because it is a fact about the models and not a preference.
BOTH_LANES_REGION = "us-east-1"

# Back-compat alias: the ported prior art and the task list both refer to `REGION`.
REGION = DEFAULT_REGION

# Every inference host this repo can reach. Asserted nowhere, listed here and rendered from
# `served_via` in the UI, so "it is all Bedrock" is a checkable claim rather than copy.
SERVED_VIA = {
    "mantle": "Amazon Bedrock — bedrock-mantle.{region}.api.aws (SigV4, service name 'bedrock')",
    "runtime": "Amazon Bedrock — bedrock-runtime.{region}.amazonaws.com (Converse via boto3)",
}

MODELS = [
    {
        "id": "google.gemma-4-26b-a4b",
        "label": "Gemma 4 26B-A4B — primary",
        "endpoint": "mantle",                              # bedrock-mantle.{region}.api.aws
        "path": "/openai/v1/chat/completions",             # NOT /v1/... -> 400
        "style": "openai",
        "provider": "Google",
        "region": DEFAULT_REGION,                          # active region; resolve() overrides per request
        "regions": ["us-east-1", "eu-central-1"],          # eu-west-1 -> 404, verified
        "supports_json_schema": True,
        "supports_prompt_caching": False,   # automatic & opportunistic (42%); no parameter to send
        "supports_latency_optimized": False,
        "unsupported_reason": {
            "prompt_caching":
                "No cachePoint or cache_control parameter exists on this route. Caching is "
                "automatic and opportunistic: 5 hits in 12 byte-identical 1873-token requests "
                "(42%), with no latency separation between hits (533-814ms) and misses "
                "(527-728ms). A cost lever, not a latency lever.",
            "latency_optimized":
                "No performanceConfig equivalent on the OpenAI-compatible route. On "
                "bedrock-runtime the parameter is refused for every candidate model in "
                "us-east-1: \"ValidationException: Latency performance configuration is not "
                "supported for <model> in us-east-1\".",
            "region:eu-west-1":
                "404 not_found_error from "
                "https://bedrock-mantle.eu-west-1.api.aws/openai/v1/chat/completions",
        },
        "observed_latency_ms": 424,
        "notes": "Primary. Served by Amazon Bedrock, never Vertex AI. Invisible to "
                 "list-foundation-models. SigV4 service name 'bedrock'.",
        # Measured per region on the same prompt, so the EU default's cost is a number the UI
        # can show rather than a caveat it has to word carefully.
        "observed_latency_by_region_ms": {"us-east-1": 489, "eu-central-1": 758},
    },
    {
        "id": "google.gemma-3-27b-it",
        "label": "Gemma 3 27B — fallback",
        "endpoint": "runtime",                             # bedrock-runtime, Converse/ConverseStream
        "path": None,
        "style": "converse",
        "provider": "Google",
        "region": DEFAULT_REGION,
        # eu-central-1 has ZERO Gemma models on bedrock-runtime -- verified, see the
        # module docstring. Listing it here would make the region dropdown a lie.
        "regions": ["us-east-1", "eu-west-1"],
        "supports_json_schema": False,      # Converse has no equivalent -> fence parser required
        "supports_prompt_caching": False,   # cachePoint -> AccessDeniedException
        "supports_latency_optimized": False,
        "unsupported_reason": {
            "json_schema":
                "Converse and ConverseStream expose no response_format parameter, so output "
                "shape is prompt-enforced only. This lane fences its JSON, which is why "
                "schemas.extract_json is load-bearing here.",
            "prompt_caching":
                "AccessDeniedException: You invoked an unsupported model or your request did "
                "not allow prompt caching. (Reproduced at ~500, ~1200 and ~2500-token "
                "prefixes, so it is model-level, not size-related.)",
            "prompt_caching_ttl":
                "ValidationException: Value at 'system.2.member.cachePoint.type' failed to "
                "satisfy constraint: Member must satisfy enum value set: [default] — the "
                "doc's selectable 1-hour TTL does not exist.",
            "latency_optimized":
                "ValidationException: Latency performance configuration is not supported for "
                "google.gemma-3-27b-it in us-east-1",
            "region:eu-central-1":
                "ValidationException: The provided model identifier is invalid. "
                "`aws bedrock list-foundation-models --region eu-central-1` returns ZERO Gemma "
                "models of any size, so this lane cannot run in the eu-central-1 default.",
        },
        "observed_latency_ms": 273,
        "notes": "Served by Amazon Bedrock via Converse. Plain base model ID — NO us./eu. "
                 "prefix, none exists for Gemma. The only lane with CloudWatch metrics "
                 "(14 vs 0 for Gemma 4). Cannot run in the eu-central-1 default: use "
                 "eu-west-1 for an EU run of this lane, or us-east-1 to A/B both.",
        "observed_latency_by_region_ms": {"us-east-1": 273},
    },
]

BY_ID = {m["id"]: m for m in MODELS}

# Models found at runtime by model_discovery, keyed by id. Kept SEPARATE from MODELS: these are
# entitlements the account can see, not routes this PoC has verified, and nothing may promote one
# into a stage default or into the region matrix smoke.sh checks.
#
# They are still registered here for one specific reason. `resolve()` falls back to the stage
# default on an unknown id, which is right for a stale dropdown value -- but it would be very wrong
# for a discovered model: the UI would offer it, the user would pick it, and the scan would come
# back labelled with a model nobody selected. Registering them makes `resolve()` able to route
# them, or to raise a clean region error, instead of silently substituting.
DISCOVERED = {}


def register_discovered(models):
    """Make discovered entries resolvable. Registry entries are never overwritten."""
    for entry in models or []:
        model_id = entry.get("id")
        if model_id and model_id not in BY_ID:
            DISCOVERED[model_id] = entry


def lookup(model_id):
    """A registry entry or a discovered one, whichever has this id. None if neither."""
    return BY_ID.get(model_id) or DISCOVERED.get(model_id)


DEFAULTS = {
    "classify": "google.gemma-4-26b-a4b",
    "deep-scan": "google.gemma-4-26b-a4b",
}


class UnsupportedRegionError(ValueError):
    """Raised for a (model, region) pair the registry knows is not invokable.

    A ValueError subclass so a caller that only catches ValueError still behaves,
    while the handler can map this one case to a 400 rather than a 502.
    """


def served_via(entry, region=None):
    """The exact Bedrock host this entry is invoked on, with the region filled in.

    One function so the answer to "where does this actually run" is derived from the same
    `endpoint` field the transport dispatch uses. A lane that were ever added without a
    SERVED_VIA row would read "unknown", never silently inherit a Bedrock label.
    """
    template = SERVED_VIA.get(entry.get("endpoint"))
    if not template:
        return f"unknown endpoint {entry.get('endpoint')!r}"
    return template.format(region=region or entry.get("region") or DEFAULT_REGION)


def all_regions():
    """Every region any registered model answers in, sorted.

    The UNION, not the intersection. The intersection is `{us-east-1}` -- the two lanes have
    disjoint EU regions -- so offering only that would hide both EU regions from the UI and
    make the EU default unreachable from the selector. Callers pair this with each model's own
    `regions` to disable the pairs that do not exist.

    Verified models only. A discovered model is listed in exactly one region (the one it was
    listed in), and letting that widen this set would put an untested region in the selector.
    """
    return sorted({r for m in MODELS for r in m["regions"]})


def resolve(model_id, stage, region=None):
    """Return the registry entry for model_id, falling back to the stage default.

    Unknown IDs fall back rather than erroring: the UI is the only caller that
    supplies one, and a stale dropdown value shouldn't break a live demo.

    An unsupported (model, region) pair is the opposite case and raises. Falling back
    silently there would make the region dropdown a lie, and the failure it hides is a
    404 in front of an audience (Gemma 4 in eu-west-1).

    The return value is a copy carrying the resolved `region`, so both lane clients
    read `model["region"]` and neither has to know how the region was chosen.
    """
    entry = lookup(model_id) or BY_ID[DEFAULTS[stage]]
    region = region or DEFAULT_REGION

    if region not in entry["regions"]:
        detail = entry["unsupported_reason"].get(f"region:{region}")
        message = (
            f"{entry['id']} is not available in {region}; "
            f"supported regions: {', '.join(entry['regions'])}"
        )
        raise UnsupportedRegionError(f"{message} — {detail}" if detail else message)

    return {**entry, "region": region, "served_via": served_via(entry, region)}
