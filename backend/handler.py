"""Lambda entry point. Routes every endpoint behind one API Gateway HTTP API.

  POST    /scan          stage 1, the escalation gate, and stage 2 when the gate fires
  POST    /classify      stage 1 only -- the UI's Simple Scan button. Also returns what the
                         gate DECIDED about that output (`wouldEscalate`), without running
                         stage 2, which is what makes the gate visible one stage at a time
  POST    /deep-scan     stage 2 only -- the UI's Deep Scan button. No prior context
  GET     /models        serve the registry so the UI dropdowns aren't hardcoded
  GET     /health        liveness only; no model call, so it stays trivially fast
  GET     /runs          every persisted run, newest first, + the key and today's usage
  POST    /public/scan   the same pipeline, open to any caller holding the API key
  OPTIONS /public/scan   CORS preflight for that one route; 204, no key check, no model call

One function rather than seven: fewer cold starts to warm, one deploy, and the
module-scope lane clients get reused across every route.

The public route is the same pipeline, not a second one. `_run_pipeline` is shared, so a
change to the escalation gate or the stage budgets cannot apply to the UI and miss the API.

Eleven things in here are load-bearing and non-obvious:

  * The `/api` prefix strip. CloudFront forwards `/api/*` to API Gateway verbatim and the
    API uses a `$default` catch-all, so this function sees `/api/scan`, not `/scan`.
    Without `removeprefix("/api")` every single call is a silent 404.
  * The escalation gate (design 4.4) is decided here, never by the client, and never on
    the model's verdict alone. The URL and attachment clauses fire even when Stage 1 says
    benign, because keying only on the verdict means a false negative on an email
    containing `http://198.51.100.7/mfa` silently skips the deep scan.
  * Stage 1 is threaded into Stage 2 server-side. The client never supplies the prior
    classification; if it did, both the escalation decision and the context Stage 2
    reasons over would be attacker-controlled on a product whose input is adversarial.
  * `x-origin-secret` fails closed. The prior art gated its check on `if API_SECRET:`
    (`handler.py:20,221`), so an unset env var silently authorised every request. Here a
    missing secret is a broken deploy and answers 500 -- the one thing it must never do is
    allow the call. It applies to `/public/scan` too: the published public endpoint is the
    CloudFront URL, so the header is always present on a legitimate call, and requiring it
    keeps the raw API Gateway URL unusable even though that route's gateway authorization
    is NONE.
  * `x-api-key` answers **401**, never 403. 403 already means "no/bad x-origin-secret", and
    one status covering both turns "which of the two layers rejected me" into a guess. A
    missing or empty `PUBLIC_API_KEY` env var is a 500, never an allow -- same posture as
    the origin secret, for the same reason.
  * The daily cap is enforced here, in the Lambda, on top of the gateway's route-level
    10 req/s + 20 burst. The gateway bounds the *rate*; only a counter bounds the *day*,
    and an unbounded day is what a leaked key actually costs.
  * A client-supplied `systemPrompt` replaces the instruction BODY and nothing else, and
    only on the three Cognito-authenticated routes. prompts.system_for re-appends the JSON
    shape and then the injection guard, so neither is client-deletable -- which is what
    makes the UI's live prompt editor safe to ship. `/public/scan` ignores an override
    entirely: a key-holder able to supply system instructions would have a general-purpose
    LLM proxy on this account's Bedrock quota rather than an email scanner.
  * `EVERY LANE IS AMAZON BEDROCK.` Both models are invoked on bedrock-mantle or
    bedrock-runtime and each response carries the exact host in `servedVia`. No Vertex AI,
    AI Studio or googleapis.com endpoint is reachable from any code path here; `google.` in
    a model ID is Bedrock's namespace for the provider.
  * `BEDROCK_REGION` is separate from `REGION` on purpose. Where the stack runs and where
    Bedrock is called are two decisions, and the EU default (eu-central-1) applies only to
    the second -- so an EU model default does not require moving the API, the table and the
    user pool with it.
  * A request may arrive as JSON *or* as a raw email with `content-type: message/rfc822` or
    `text/plain`, in which case the body IS the email and the options move to the query
    string (`?region=...`). That exists so `--data-binary @message.eml` works: an email is
    full of quotes, backslashes and newlines, and hand-escaping one into JSON is the step
    most likely to go wrong. `systemPrompt` is NOT accepted from the query string -- a query
    string lands in access logs and a prompt is content, not a parameter.

CORS exists on `/public/scan` and nowhere else. The rest of the API is same-origin behind
CloudFront (design 7.1), and the public route needs it because a browser caller cannot see
a CORS-less response at all -- including the 429 that tells it why it was refused.

The Cognito JWT is verified by the API Gateway JWT authorizer, not here. `/runs` therefore
sits behind the JWT `$default` route and is already authenticated, which is what makes it
the right place -- and the only place -- to serve the public API key back.
"""

import base64
import functools
import hmac
import json
import logging
import os
import re
import time

import mantle_client
import model_discovery
import model_registry
import prompts
import converse_client
import runs_store
import schemas

log = logging.getLogger()
log.setLevel(logging.INFO)

# The two lanes. Which one a request takes is a property of the model (design 3.2), so
# `_invoke` is the only function below that knows they differ.
_TRANSPORTS = {"mantle": mantle_client, "runtime": converse_client}

# Design 4.5. Client-overridable within a ceiling: maxTokens is the dominant lever on E2E
# and E2E has to stay inside API Gateway's hard 30s cap (tasks T3.5).
#
# classify is 320, not the design's 200. Measured: at 200, Gemma 4 truncated 4 of 58 Stage 1
# responses mid-JSON (stopReason=max_tokens) when it returned the full 5 ranked signals with
# their detail strings. A truncated response is a parse failure, and the escalation gate then
# fires on parse_failed rather than on the model's actual verdict -- so the cheap stage silently
# escalates ~7% of traffic for the wrong reason and doubles its cost. 320 clears the observed
# ceiling with headroom; the E2E cost is ~150ms on a 3s target we beat by 2s.
_MAX_TOKENS = {"classify": 320, "deep-scan": 600}
_MAX_TOKENS_CEILING = 2000

# doc 6.1a. Real and accepted on the mantle lane; halves token cost and measured *slower*,
# not faster, so the UI frames it as cost. Anything else would be a 400 from the service.
_SERVICE_TIERS = ("default", "flex")

# The default lane region. A per-request `region` overrides it (design 1.1 #8); this is
# only the fallback and what /health reports.
#
# Both names are read because infra/lambda.tf sets `REGION` (from var.region) and this file
# was reading only `BEDROCK_REGION`, which nothing sets -- so the Terraform variable was
# inert and a tfvars change to eu-central-1 would have been silently ignored in favour of
# the registry default. `BEDROCK_REGION` still wins where it is set explicitly.
REGION = (os.environ.get("BEDROCK_REGION")
          or os.environ.get("REGION")
          or model_registry.DEFAULT_REGION)

_FALLBACK_DEFAULT = next(
    (m["id"] for m in model_registry.MODELS if m["id"] != model_registry.DEFAULTS["classify"]),
    model_registry.DEFAULTS["classify"],
)
PRIMARY_MODEL_ID = os.environ.get("PRIMARY_MODEL_ID") or model_registry.DEFAULTS["classify"]
FALLBACK_MODEL_ID = os.environ.get("FALLBACK_MODEL_ID") or _FALLBACK_DEFAULT

# The one public, key-authenticated route. Named once so the router, the CORS decision and
# the endpoint string the UI advertises cannot drift apart.
PUBLIC_PATH = "/public/scan"

# This route only. Allow-Origin `*` is correct here and only here: the caller is anyone
# holding the key, from anywhere, so there is no origin to pin. The rest of the API stays
# same-origin with no CORS at all (design 7.1).
_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "content-type,x-api-key",
    "Access-Control-Allow-Methods": "POST,OPTIONS",
    "Access-Control-Max-Age": "600",
}

# How many rows GET /runs returns. Bounded because each row carries a full email body.
_RUNS_LIMIT = 100

# Headers a pasted email may lead with. Anything else -- including the first line that is
# not a header -- starts the body.
_PASTED_HEADER_RE = re.compile(r"^(from|to|subject)\s*:\s*(.*)$", re.IGNORECASE)

# Set once per execution environment; lets the UI flag cold-start runs so a 3s cold
# start never gets read as model latency.
_COLD_START = True


# ---------------------------------------------------------------------------
# Cold-start validation
# ---------------------------------------------------------------------------
def _validate_lane(model_id, region):
    """Probe one configured (model, endpoint, path, region) triple. Raises on a bad one.

    Every fact checked here is fixed at deploy time, so it is checked once per execution
    environment rather than once per request: a wrong `PRIMARY_MODEL_ID` or an unroutable
    region belongs in the init log, not in a failed user scan.

    This is a configuration and credential probe, not an inference call. A token-spending
    probe at init would add ~0.5s to every cold start and would turn a transient Bedrock
    blip into a hard init failure; `smoke.sh` (T6.1) is what asserts a real end-to-end
    call before the demo.
    """
    if model_id not in model_registry.BY_ID:
        raise RuntimeError(
            f"model '{model_id}' is not in the registry; known ids: "
            f"{', '.join(sorted(model_registry.BY_ID))}"
        )
    model = model_registry.resolve(model_id, "classify", region)  # raises on a bad pair

    transport = _TRANSPORTS.get(model["endpoint"])
    if transport is None:
        raise RuntimeError(
            f"model '{model_id}' declares endpoint '{model['endpoint']}', but the only "
            f"lanes implemented are: {', '.join(sorted(_TRANSPORTS))}"
        )
    # Design 3.2: the path is not decoration. On mantle `/openai/v1/*` carries Gemma 4 and
    # `/v1/*` carries Gemma 3, and one wrong segment is a 400 that reads like a
    # model-availability problem.
    if model["endpoint"] == "mantle" and not (model["path"] or "").startswith("/"):
        raise RuntimeError(
            f"model '{model_id}' is on the mantle lane but carries no request path "
            f"(expected e.g. /openai/v1/chat/completions, got {model['path']!r})"
        )
    if model["endpoint"] == "runtime" and model["path"]:
        raise RuntimeError(
            f"model '{model_id}' is on the bedrock-runtime lane, which takes no URL path, "
            f"but the registry sets path={model['path']!r}"
        )

    # Builds the boto3 client / resolves frozen SigV4 credentials and one TLS connection
    # now, so the first real request doesn't pay for it (design 5.1 "connection reuse",
    # 5.2 "cold start"). Both lane clients expose warm(region) as part of the contract.
    transport.warm(model["region"])
    return model


def _startup():
    """Validate both configured lanes. The primary raises; the fallback only warns.

    Asymmetric on purpose. The primary lane is the demo, so a bad one should fail the init
    loudly. A broken fallback costs only the A/B toggle, and killing a working demo over
    it would be the worse outcome.
    """
    state = {"region": REGION, "primary": PRIMARY_MODEL_ID,
             "fallback": FALLBACK_MODEL_ID, "warnings": []}

    model = _validate_lane(PRIMARY_MODEL_ID, REGION)
    log.info("startup: primary lane ok -- %s via %s%s in %s",
             model["id"], model["endpoint"], model["path"] or "", model["region"])

    try:
        model = _validate_lane(FALLBACK_MODEL_ID, REGION)
        log.info("startup: fallback lane ok -- %s via %s in %s",
                 model["id"], model["endpoint"], model["region"])
    except Exception as exc:  # noqa: BLE001 -- degrade the A/B toggle, don't kill the demo
        warning = f"fallback lane '{FALLBACK_MODEL_ID}' unusable: {type(exc).__name__}: {exc}"
        state["warnings"].append(warning)
        log.warning("startup: %s", warning)

    return state


_STARTUP = _startup()


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------
def _reply(status, body, headers=None):
    # no-store on every API response, not just the sensitive ones. GET /runs returns the
    # public API key and the full stored email bodies, and relying on the CloudFront
    # behaviour's max_ttl=0 to keep that out of a cache puts the control in the wrong place:
    # it lives in the distribution config, one edit away from being lost, and it does nothing
    # about the browser cache or any proxy in between. The header travels with the response.
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
            **(headers or {}),
        },
        "body": json.dumps(body),
    }


def _cors_reply(status, body, headers=None):
    """A reply on the public route. Used for its errors too, deliberately.

    A browser cannot read a response that lacks Allow-Origin, so a CORS-less 429 or 401 is
    indistinguishable from a network failure at the caller -- the two statuses most likely
    to need reading are the two that would be invisible.
    """
    return _reply(status, body, {**_CORS_HEADERS, **(headers or {})})


def _public_endpoint():
    """The URL public callers POST to. Env-supplied when known, otherwise derived.

    Derived rather than hardcoded so a domain change cannot leave the UI advertising an
    endpoint that 404s.
    """
    explicit = (os.environ.get("PUBLIC_ENDPOINT_URL") or "").strip()
    if explicit:
        return explicit
    domain = (os.environ.get("SITE_DOMAIN") or "email-scan.example.com").strip()
    domain = domain.split("://")[-1].strip("/")
    return f"https://{domain}/api{PUBLIC_PATH}"


def _origin_allowed(event):
    """Check CloudFront's `x-origin-secret`. Returns (allowed, failure_reply).

    The secret is read per request rather than cached at import so that the fail-closed
    branch is exercisable and stays honest: no code path can reach the model with an
    unset `ORIGIN_SECRET`.

    API Gateway payload format 2.0 lowercases every header name, so the prior art's
    `.title()` fallback is dead code. `compare_digest` raises TypeError on a non-ASCII
    `str`, so compare bytes.
    """
    secret = os.environ.get("ORIGIN_SECRET") or ""
    if not secret:
        return False, _reply(500, {
            "error": "server misconfigured: ORIGIN_SECRET is unset, so no request can be "
                     "authorised",
            "errorClass": "ConfigurationError",
        })

    presented = (event.get("headers") or {}).get("x-origin-secret")
    if not presented:
        return False, _reply(403, {"error": "missing x-origin-secret",
                                   "errorClass": "Forbidden"})
    if not hmac.compare_digest(presented.encode("utf-8", "surrogateescape"),
                               secret.encode("utf-8")):
        return False, _reply(403, {"error": "invalid x-origin-secret",
                                   "errorClass": "Forbidden"})
    return True, None


def _api_key_allowed(event):
    """Check `x-api-key` on the public route. Returns (allowed, failure_reply).

    Fails closed on a missing or empty `PUBLIC_API_KEY`, exactly like the origin secret: an
    unset key is a broken deploy, and the one thing it must never do is authorise the call.

    401, not 403. 403 is already what a missing or wrong `x-origin-secret` returns, and
    collapsing both layers onto one status makes a failing call impossible to diagnose from
    the outside. Compared as bytes because `hmac.compare_digest` raises TypeError on a
    non-ASCII `str`, and a header is attacker-controlled.
    """
    expected = os.environ.get("PUBLIC_API_KEY") or ""
    if not expected:
        return False, _cors_reply(500, {
            "error": "server misconfigured: PUBLIC_API_KEY is unset, so the public "
                     "endpoint cannot authorise any request",
            "errorClass": "ConfigurationError",
        })

    presented = (event.get("headers") or {}).get("x-api-key")
    if not presented:
        return False, _cors_reply(401, {"error": "missing x-api-key header",
                                        "errorClass": "Unauthorized"})
    if not hmac.compare_digest(presented.encode("utf-8", "surrogateescape"),
                               expected.encode("utf-8")):
        return False, _cors_reply(401, {"error": "invalid x-api-key",
                                        "errorClass": "Unauthorized"})
    return True, None


def parse_pasted_email(text):
    """Split raw pasted email text into {from, to, subject, body}.

    The convenience form of the public request -- and probably the common one, because
    `curl -d '{"text": "<the email>"}'` is what a caller reaches for first. Only leading
    From:/To:/Subject: lines are consumed, and only until the first line that is not one of
    them (a blank line ends the block, as in RFC 5322). An email with no headers at all is
    all body, which is the correct reading of a pasted fragment rather than a parse failure.

    Any header-looking line *inside* the body stays in the body: the input is adversarial by
    definition on this product, and hoisting a `Subject:` planted three paragraphs down
    would let a sender rewrite the field the analyst reads.
    """
    email = {"from": "", "to": "", "subject": "", "body": ""}
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")

    index = 0
    for index, line in enumerate(lines):
        match = _PASTED_HEADER_RE.match(line)
        if not match:
            break
        email[match.group(1).lower()] = match.group(2).strip()
    else:
        # Every line was a header, so there is no body to take.
        index = len(lines)

    body = "\n".join(lines[index:])
    email["body"] = body.lstrip("\n") if any(email[k] for k in ("from", "to", "subject")) else body
    return email


def _email_input(body):
    """Normalise every accepted request shape into (email_dict, prompt_text, problem).

    Three shapes reach this function and all three must end up as one thing, because the
    dict is what gets persisted and the text is what the model and the escalation gate both
    see:

        {"email": {"from", "subject", "body", "attachments"}}   the UI
        {"text": "<raw pasted email>"}                          the public convenience form
        {"email": "<raw pasted email>"}                         the original flat form

    Exactly one of `email_dict`/`prompt_text` and `problem` is None.
    """
    email = body.get("email")
    raw = body.get("text")

    # The structured form wins whenever it carries content, so a caller that sends both is
    # scanned on the field it filled in rather than on whichever branch is tested first.
    if isinstance(email, dict) and (email.get("body") or email.get("subject")):
        return email, schemas.format_email(email), None

    pasted = email if isinstance(email, str) else raw if isinstance(raw, str) else None
    if pasted is not None:
        parsed = parse_pasted_email(pasted.strip())
        if not parsed["body"] and not parsed["subject"]:
            return None, None, "email or text is required"
        return parsed, schemas.format_email(parsed), None

    if isinstance(email, dict):
        return None, None, "email.body or email.subject is required"
    return None, None, "email or text is required"


# ---------------------------------------------------------------------------
# The escalation gate (design 4.4)
# ---------------------------------------------------------------------------
# Imported, not defined here. The gate and the email rendering both live in schemas.py
# alongside the parser and the json_schemas, because bench/driver.py measures the
# escalation rate (design 9.3's headline number) and it has to measure *this* gate over
# *this* rendering. It previously carried its own mirror, and the two had already drifted:
# the harness's URL clause matched only `http://` and `www.`, so the number it reported
# described a gate the Lambda does not run.
URL_RE = schemas.URL_RE
ATTACHMENT_RE = schemas.ATTACHMENT_RE
should_escalate = schemas.should_escalate
_ESCALATE_BELOW_CONFIDENCE = schemas.ESCALATE_BELOW_CONFIDENCE


# ---------------------------------------------------------------------------
# Stage execution
# ---------------------------------------------------------------------------
def _invoke(model, stage, system_prompt, user_text, max_tokens, temperature, service_tier):
    """Send one request down whichever lane the model lives on.

    Both clients return the same dict (raw / stopReason / usage / timings / timeline), so
    everything downstream -- including the whole frontend -- is transport-agnostic.
    """
    transport = _TRANSPORTS[model["endpoint"]]
    if model["endpoint"] != "mantle":
        # Converse has no response_format, so shape is prompt-enforced on this lane and
        # schemas.extract_json does the rest. No cachePoint and no performanceConfig are
        # sent anywhere: both are hard failures on Gemma (design 5.3, 5.4).
        return transport.converse(model, system_prompt, user_text,
                                  max_tokens=max_tokens, temperature=temperature)

    kwargs = {}
    if model["supports_json_schema"]:
        kwargs["response_format"] = schemas.response_format(stage)
    if service_tier:
        kwargs["service_tier"] = service_tier
    return transport.chat(model, system_prompt, user_text,
                          max_tokens=max_tokens, temperature=temperature, **kwargs)


def _run_stage(stage, body, user_text, allow_prompt_override=False):
    """Run one stage. Returns (payload, error); exactly one of the two is None."""
    try:
        model = model_registry.resolve(body.get("modelId"), stage, body.get("region"))
    except model_registry.UnsupportedRegionError as exc:
        # A 400, not a 502: the request asked for a pair the registry knows is a 404.
        return None, {"status": 400, "error": str(exc),
                      "errorClass": type(exc).__name__, "stage": stage}

    # A client-supplied `systemPrompt` replaces the instruction BODY and nothing else:
    # prompts.system_for re-appends the JSON shape and then the injection guard, so neither is
    # client-deletable. That is what makes the UI's live editor safe to expose. The prior art
    # (`handler.py:70`) let the client replace the whole prompt, guard included.
    override = body.get("systemPrompt") if allow_prompt_override else None
    if override is not None and not isinstance(override, str):
        return None, {"status": 400, "error": "systemPrompt must be a string",
                      "errorClass": "BadRequest", "stage": stage}
    if isinstance(override, str) and len(override) > prompts.MAX_OVERRIDE_CHARS:
        return None, {
            "status": 400,
            "error": f"systemPrompt is {len(override)} characters; the ceiling is "
                     f"{prompts.MAX_OVERRIDE_CHARS}. Every character is billed as input on "
                     f"every request at this prompt.",
            "errorClass": "BadRequest", "stage": stage,
        }
    system_prompt, prompt_overridden = prompts.system_for(stage, model, override)
    try:
        max_tokens = min(int(body.get("maxTokens") or _MAX_TOKENS[stage]), _MAX_TOKENS_CEILING)
        # Design 4.5: 0.0, not the prior art's 0.1. Non-deterministic output means two
        # consecutive runs disagree with each other in front of an audience.
        temperature = min(max(float(body.get("temperature", 0.0)), 0.0), 1.0)
    except (TypeError, ValueError) as exc:
        # A garbage number is the caller's bug, and a clean 400 beats an unhandled 500.
        return None, {"status": 400, "error": f"invalid maxTokens or temperature: {exc}",
                      "errorClass": type(exc).__name__, "stage": stage}
    service_tier = body.get("serviceTier") if body.get("serviceTier") in _SERVICE_TIERS else None

    global _COLD_START
    cold = _COLD_START
    _COLD_START = False

    lambda_t0 = time.perf_counter()
    try:
        out = _invoke(model, stage, system_prompt, user_text, max_tokens, temperature,
                      service_tier)
    except Exception as exc:  # noqa: BLE001 -- surfaced to the UI, with its class
        # `errorClass` is machine-readable on purpose. Without it a ValidationException
        # from a bad response_format, an AccessDeniedException from a missing mantle IAM
        # action (design R1) and a genuine model error are indistinguishable, and T5.4's
        # greyed-toggle hover needs the verbatim text.
        log.exception("%s failed on %s in %s", stage, model["id"], model["region"])
        return None, {"status": 502, "error": f"{type(exc).__name__}: {exc}",
                      "errorClass": type(exc).__name__, "stage": stage,
                      "modelId": model["id"], "region": model["region"]}

    parsed, parse_error = schemas.extract_json(out["raw"])
    timings = dict(out["timings"])
    timings["lambdaMs"] = round((time.perf_counter() - lambda_t0) * 1000, 1)
    timings["coldStart"] = cold

    return {
        "stage": stage,
        "result": parsed,
        "parseError": parse_error,
        "raw": out["raw"],
        "stopReason": out["stopReason"],
        "modelId": model["id"],
        "modelLabel": model["label"],
        "region": model["region"],
        "endpoint": model["endpoint"],
        # The exact Bedrock host this call went to. Returned per response rather than stated
        # once in the UI, so "all Bedrock, no Vertex AI" is answered by the request itself.
        "servedVia": model["served_via"],
        "invocationStyle": model["style"],
        "jsonSchemaEnforced": bool(model["supports_json_schema"]),
        "serviceTier": service_tier,
        # True when this run used an edited prompt body. The UI badges it, because a latency
        # or verdict comparison across two different prompts is not a comparison.
        "promptOverridden": prompt_overridden,
        # Exact bytes sent, so the UI can show the real prompt rather than a guess.
        "promptSent": {"system": system_prompt, "user": user_text},
        "usage": out["usage"],
        "timings": timings,
        # Per-delta arrival times; T5.3 replays these as the real inter-token gaps.
        "timeline": out.get("timeline") or [],
    }, None


def _final_verdict(stage1, stage2):
    """The verdict the product would act on: Stage 2's when it ran, else Stage 1's."""
    for payload in (stage2, stage1):
        result = (payload or {}).get("result") or {}
        if result.get("verdict"):
            return result["verdict"]
    return None


# ---------------------------------------------------------------------------
# The scan pipeline, shared by the UI route and the public route
# ---------------------------------------------------------------------------
def _run_pipeline(body, email_text, allow_prompt_override=False):
    """Stage 1, the design 4.4 gate, and Stage 2 only when the gate fires.

    Returns (status, payload). One implementation for both routes on purpose: a forked
    pipeline is one where the escalation gate, the stage budgets or the threading string can
    be changed for the UI and silently miss the API, and the persisted runs from the two
    sources would then not be comparable.

    `allow_prompt_override` is the one thing the two routes do differ on, and it is passed in
    rather than read from `body` here so the difference is visible at both call sites. See
    prompts.py: the public route runs the default body.
    """
    t0 = time.perf_counter()
    stage1, error = _run_stage("classify", body, f"Analyze this email:\n{email_text}",
                               allow_prompt_override)
    if error:
        return error.pop("status"), error

    reason = should_escalate(stage1["result"], email_text, stage1["parseError"] is None)
    out = {"escalated": reason is not None, "escalationReason": reason,
           "stage1": stage1, "stage2": None}

    if reason is not None:
        prior = stage1["result"]
        user_text = f"Analyze this email:\n{email_text}"
        if prior is not None:
            # The prior art's exact threading string. Server-side, always.
            user_text = (f"Stage 1 classification: {json.dumps(prior)}\n\n"
                         f"Analyze this email in detail:\n{email_text}")
        stage2, error = _run_stage("deep-scan", body, user_text, allow_prompt_override)
        if error:
            # Carry Stage 1 into the failure: its verdict is real and measured, and the UI
            # can render it instead of an empty pipeline.
            return error.pop("status"), {**error, "escalated": True,
                                         "escalationReason": reason,
                                         "stage1": stage1}
        out["stage2"] = stage2

    out["verdict"] = _final_verdict(stage1, out["stage2"])
    out["totalMs"] = round((time.perf_counter() - t0) * 1000, 1)
    return 200, out


def _run_payload(out, email, source):
    """Flatten a scan result into the row shape runs_store persists.

    Two choices worth naming, because the SCAN RESULTS tab has to label them honestly:

      * `timings` are **Stage 1's**. TTFT is a Stage 1 property (it is the first token of
        the triage call), and the whole-run wall clock is already `totalMs` in the response.
      * `usage` is **summed across whichever stages ran**, because that -- not Stage 1's
        share of it -- is what the run cost.

    `verdict` is the final verdict, i.e. Stage 2's when it overturned Stage 1, while
    `confidence` and `signals` stay Stage 1's, since Stage 2's schema emits neither.
    """
    stage1 = out.get("stage1") or {}
    stage2 = out.get("stage2") or None
    result1 = stage1.get("result") or {}
    result2 = (stage2 or {}).get("result") or {}

    # Which stage payloads to sum usage over. Defaulted rather than always derived, because a
    # standalone Deep Scan passes the SAME payload as both the timing source and the Stage 2
    # findings -- deriving the list from (stage1, stage2) would then double every token count.
    stages_ran = out.get("stagesRan")
    if stages_ran is None:
        stages_ran = [s for s in (stage1, stage2) if s]

    usage = {key: 0 for key in ("inputTokens", "outputTokens", "totalTokens")}
    for stage in stages_ran:
        stage_usage = (stage or {}).get("usage") or {}
        for key in usage:
            try:
                usage[key] += int(stage_usage.get(key) or 0)
            except (TypeError, ValueError):
                pass

    # Stage 1's for a pipeline run: TTFT is a Stage 1 property and the whole-run wall clock is
    # already `totalMs`. A standalone Deep Scan has no Stage 1, so it names its own payload here
    # rather than persisting a row of nulls.
    timings = (out.get("timingsFrom") or stage1).get("timings") or {}
    # The stage payload the row's model, endpoint and region describe. Same reason as `timings`.
    lane = out.get("timingsFrom") or stage1

    return {
        "source": source,
        "stages": out.get("stages") or [s.get("stage") for s in stages_ran if s.get("stage")],
        "modelId": lane.get("modelId"),
        "modelLabel": lane.get("modelLabel"),
        "endpoint": lane.get("endpoint"),
        "servedVia": lane.get("servedVia"),
        "region": lane.get("region"),
        # Persisted because a row run on an edited prompt is not comparable to the rest of the
        # table, and the table has no other way to know.
        "promptOverridden": bool(lane.get("promptOverridden")
                                 or (stage2 or {}).get("promptOverridden")),
        "verdict": out.get("verdict"),
        "confidence": result1.get("confidence"),
        "signals": result1.get("signals") or [],
        "parseError": lane.get("parseError"),
        "escalated": bool(out.get("escalated")),
        "escalationReason": out.get("escalationReason"),
        "stage2": ({
            "recommendedAction": result2.get("recommendedAction"),
            "riskScore": result2.get("riskScore"),
            "disagreesWithStage1": result2.get("disagreesWithStage1"),
        } if stage2 else None),
        "timings": {key: timings.get(key) for key in
                    ("ttftMs", "e2eMs", "bedrockLatencyMs", "overheadMs", "otps")},
        "usage": usage,
        # Full content, per the product decision recorded in runs_store's docstring.
        "email": email,
    }


def _record(out, email, source):
    """Persist one completed run and return its id, or None.

    Only completed scans are recorded: the row shape carries a verdict and timings, and a
    502 from a lane failure has neither. The failure is already in CloudWatch with its
    exception class, which is the artifact that diagnoses it.
    """
    return runs_store.record_run(_run_payload(out, email, source))


def _record_single(payload, email, stage, reason=None):
    """Persist one standalone stage run -- the UI's two scan buttons.

    These land in the same table as the pipeline and API runs, because the Scan Results tab is
    supposed to be every scan the backend performed, not a subset that happens to exclude the
    two buttons a user actually presses. `source` distinguishes them, so a row is never
    mistaken for a pipeline run.

    A standalone Deep Scan has no Stage 1, so its own payload is named as the lane and timing
    source and also supplies the Stage 2 findings. `stagesRan` is passed explicitly for exactly
    that case: deriving it would count the one payload twice and double every token in the row.
    """
    result = payload.get("result") or {}
    if stage == "classify":
        out = {
            "stage1": payload,
            "stage2": None,
            "stagesRan": [payload],
            "stages": ["classify"],
            # `escalated` is "Stage 2 ran", and on this route it did not. The gate's decision is
            # still recorded, as the reason string -- that is what the button reports.
            "escalated": False,
            "escalationReason": reason,
            "verdict": result.get("verdict"),
        }
        return _record(out, email, "ui-simple")

    out = {
        "stage1": {},
        "stage2": payload,
        "timingsFrom": payload,
        "stagesRan": [payload],
        "stages": ["deep-scan"],
        # Nothing escalated: an operator asked for the deep stage directly.
        "escalated": False,
        "escalationReason": None,
        "verdict": result.get("verdict"),
    }
    return _record(out, email, "ui-deep")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def _single_stage(stage, body):
    """POST /classify and POST /deep-scan -- one stage, no gate. The UI's two scan buttons.

    A standalone deep scan deliberately gets no prior context. Stage 1's output is threaded
    in only by `_run_pipeline`, from a Stage 1 the server itself just ran; accepting a
    `classification` from the client would put the deep scan's premise in the caller's
    hands, so this route ignores one if it is sent.

    On `/classify` the response also carries what the design 4.4 gate DECIDED about this
    Stage 1 output -- without running Stage 2. The UI's Simple Scan button needs to show the
    gate verdict ("this one would escalate, because url_present_despite_benign") and the only
    honest way to get it is from the server's own gate function. Computing it in the browser
    would put the product's escalation logic in client code, where it could drift from the
    Lambda's and would be trivially editable by anyone reading the page.

    Both routes are behind the Cognito JWT authorizer, so the prompt-body override is allowed
    here. `/public/scan` is the route where it is not.
    """
    email, email_text, problem = _email_input(body)
    if problem:
        return _reply(400, {"error": problem})

    payload, error = _run_stage(stage, body, f"Analyze this email:\n{email_text}",
                                allow_prompt_override=True)
    if error:
        return _reply(error.pop("status"), error)

    reason = None
    if stage == "classify":
        reason = should_escalate(payload["result"], email_text,
                                 payload["parseError"] is None)
        # `wouldEscalate`, not `escalated`: no Stage 2 ran on this route, and a field named as
        # though one had would misread in the UI and in any client built against this.
        payload["wouldEscalate"] = reason is not None
        payload["escalationReason"] = reason

    payload["runId"] = _record_single(payload, email, stage, reason)
    return _reply(200, payload)


def _scan(body):
    """POST /scan -- the UI route. Behind the JWT `$default` route and the origin secret."""
    email, email_text, problem = _email_input(body)
    if problem:
        return _reply(400, {"error": problem})

    # Authenticated route, so a prompt-body override is honoured here.
    status, out = _run_pipeline(body, email_text, allow_prompt_override=True)
    if status != 200:
        return _reply(status, out)

    # Every run is persisted, whichever route it came in on, so the Scan Results tab shows the
    # whole history rather than only the API's half of it. A failed write returns None and is
    # ignored here by design -- see runs_store.record_run.
    out["runId"] = _record(out, email, "ui")
    return _reply(200, out)


def _public_scan(event, body):
    """POST /public/scan -- open to any caller holding the key. Three gates, in this order.

    Key, then quota, then work. The order is the point: the quota counter must not be spent
    on a request that was never authorised, and neither an unauthorised nor an over-cap
    request may reach the model, since the whole purpose of the cap is that a leaked key
    cannot spend tokens.
    """
    allowed, denial = _api_key_allowed(event)
    if not allowed:
        return denial

    allowed, count, cap, resets_at = runs_store.check_and_bump_daily()
    if not allowed:
        if count < 0:
            # The counter itself was unavailable. That is not "you are over the cap", and
            # saying 429 would send the caller to wait for a reset that is not the problem.
            log.error("public scan refused: the daily quota counter is unavailable")
            return _cors_reply(503, {
                "error": "the daily quota counter is unavailable, so the public endpoint "
                         "is refusing requests rather than running uncapped",
                "errorClass": "QuotaUnavailable",
            })
        log.warning("public scan refused: daily cap reached (%s/%s)", count, cap)
        return _cors_reply(429, {
            "error": f"daily request cap reached: {count} of {cap} used",
            "errorClass": "DailyCapExceeded",
            "count": count, "cap": cap, "resetsAt": resets_at,
        }, {"Retry-After": str(runs_store.seconds_until_reset())})

    email, email_text, problem = _email_input(body)
    if problem:
        return _cors_reply(400, {
            "error": problem,
            "errorClass": "BadRequest",
            "hint": 'send {"email": {"from", "subject", "body", "attachments"}} '
                    'or {"text": "<raw pasted email>"}',
        })

    # No prompt override on this route, deliberately -- see prompts.py. A key-holder able to
    # supply system instructions would have an LLM proxy on our quota, not an email scanner.
    status, out = _run_pipeline(body, email_text, allow_prompt_override=False)
    if status != 200:
        return _cors_reply(status, out)

    out["runId"] = _record(out, email, "api")
    return _cors_reply(200, out)


def _public_preflight():
    """OPTIONS /public/scan -- 204, CORS headers, no key check and no model call.

    A preflight carries no `x-api-key` by definition (that is what it is asking permission
    to send), so checking one here would make every browser call fail before it started.
    """
    return {"statusCode": 204, "headers": dict(_CORS_HEADERS), "body": ""}


def _runs():
    """GET /runs -- every persisted run, newest first, plus how to call the public endpoint.

    Behind the JWT `$default` route, so this is the one authenticated place the public API
    key is served. It is returned here rather than printed in a Terraform output so the
    operator reads it from the UI they are already signed in to -- and so it never appears
    on the unauthenticated public route.
    """
    runs = runs_store.list_runs(_RUNS_LIMIT)
    return _reply(200, {
        "runs": runs,
        "count": len(runs),
        "publicEndpoint": _public_endpoint(),
        "apiKey": os.environ.get("PUBLIC_API_KEY") or "",
        "dailyUsage": runs_store.daily_usage(),
    })


def _models():
    """GET /models -- the registry plus whatever Bedrock says this account can see.

    The model list is the two verified registry entries followed by every other Gemma model
    `bedrock:ListFoundationModels` reports, each flagged `verified: false`. See
    model_discovery: discovery ADDS and never replaces, because the primary model is invisible
    to that API and a list that omits it must not be read as the model being gone.
    """
    # `catalog` is the verified registry entries FOLLOWED BY the discovered ones, in that order,
    # so the dropdown's first two options stay the two models whose numbers were measured.
    catalog, discovery_note = model_discovery.discover(REGION)
    # Registered so a discovered id the UI offers actually routes. Without this, resolve() would
    # fall back to the stage default and return a scan labelled with a model nobody picked.
    model_registry.register_discovered(catalog)
    # The UNION of every model's regions, not the intersection. The intersection is
    # {us-east-1}, because the two lanes have disjoint EU regions -- returning it would hide
    # both EU regions from the selector and make the eu-central-1 default unreachable from the
    # UI that is supposed to default to it. Per-model `regions` below is what the UI disables
    # the impossible pairs from, and the server still refuses one with a 400 either way.
    return _reply(200, {
        "region": REGION,
        "regions": model_registry.all_regions(),
        "defaultRegion": model_registry.DEFAULT_REGION,
        "bothLanesRegion": model_registry.BOTH_LANES_REGION,
        # Null when discovery had nothing to say. Non-null is either "n models discovered" or the
        # verbatim exception that stopped it -- an AccessDeniedException here means the Lambda role
        # is missing bedrock:ListFoundationModels, which is fixable and worth naming in the UI.
        "discoveryNote": discovery_note,
        "defaults": model_registry.DEFAULTS,
        "primaryModelId": PRIMARY_MODEL_ID,
        "fallbackModelId": FALLBACK_MODEL_ID,
        "serviceTiers": list(_SERVICE_TIERS),
        "budgets": {"maxTokens": _MAX_TOKENS, "temperature": 0.0},
        "targets": {
            "classifyP50Ms": 3000,
            "deepScanP95Ms": 10000,
            "ttftMs": 1000,
        },
        "escalation": {
            "confidenceThreshold": _ESCALATE_BELOW_CONFIDENCE,
            # The reason codes the arrow can show, in the order the gate tests them.
            "reasons": ["parse_failed", "verdict=<not benign>", "low_confidence=<value>",
                        "url_present_despite_benign", "attachment_present_despite_benign"],
        },
        # Three separate things, because the UI's prompt editor may only edit one of them.
        # `bodies` seeds the editable textareas; `jsonShapes` and `injectionGuard` are what the
        # server appends afterwards and the UI shows read-only. `full` is the assembled default,
        # kept so a client that only wants to display the prompt does not have to reassemble it
        # and get the order wrong.
        "prompts": {
            "bodies": dict(prompts.BODIES),
            "jsonShapes": dict(prompts.JSON_SHAPES),
            "injectionGuard": prompts.INJECTION_GUARD,
            "maxOverrideChars": prompts.MAX_OVERRIDE_CHARS,
            # The override is honoured on /scan, /classify and /deep-scan (all Cognito-
            # authenticated) and ignored on /public/scan. Advertised so a client does not have
            # to discover it by having its override silently dropped.
            "overrideRoutes": ["/scan", "/classify", "/deep-scan"],
            "full": {
                "classify": prompts.SYSTEM_PROMPTS["classify"],
                "deep-scan": prompts.SYSTEM_PROMPTS["deep-scan"],
            },
        },
        "models": [
            {
                "id": m["id"],
                "label": m["label"],
                "provider": m["provider"],
                "endpoint": m["endpoint"],
                "path": m["path"],
                "style": m["style"],
                # Where this model is actually invoked, with the default region filled in. The
                # UI renders this string; it does not compose its own claim about the host.
                "servedVia": model_registry.served_via(m, REGION),
                "regions": m["regions"],
                "observedLatencyByRegionMs": m.get("observed_latency_by_region_ms") or {},
                "supportsJsonSchema": m["supports_json_schema"],
                "supportsPromptCaching": m["supports_prompt_caching"],
                "supportsLatencyOptimized": m["supports_latency_optimized"],
                # Verbatim AccessDeniedException / ValidationException text. T5.4 shows
                # these on hover, because the real error is more persuasive than a claim.
                "unsupportedReason": m["unsupported_reason"],
                "observedLatencyMs": m["observed_latency_ms"],
                # False on a discovered model: the account can see it, which is not the same as
                # this PoC having measured it on the lane it would be routed to. The UI labels the
                # difference rather than presenting all of them as equally proven.
                "verified": bool(m.get("verified", True)),
                "notes": m["notes"],
            }
            for m in catalog
        ],
    })


def _health():
    """GET /health -- no model call, so it can never approach the 30s gateway ceiling."""
    return _reply(200, {
        "ok": True,
        # Reported, not consumed: /health warms nothing, so it must not claim to.
        "coldStart": _COLD_START,
        "startup": _STARTUP,
    })


_ROUTES = {
    "/scan": ("POST", _scan),
    "/classify": ("POST", functools.partial(_single_stage, "classify")),
    "/deep-scan": ("POST", functools.partial(_single_stage, "deep-scan")),
    "/models": ("GET", _models),
    "/health": ("GET", _health),
    "/runs": ("GET", _runs),
}


# Options accepted from the QUERY STRING, for the raw-body form where there is no JSON object to
# put them in. Whitelisted, and `systemPrompt` is deliberately NOT on the list: a query string is
# recorded in CloudFront and API Gateway access logs, and a prompt is content, not a parameter.
# Anyone sending one has the JSON form available.
_QUERY_OPTIONS = ("region", "modelId", "maxTokens", "temperature", "serviceTier")

# Content types that mean "the body IS the email". Anything else is parsed as JSON.
#
# This exists so a caller can `--data-binary @message.eml` without building a JSON object around a
# real message -- which is the case most likely to go wrong by hand, because an email is full of
# quotes, backslashes and newlines that all need escaping.
_RAW_EMAIL_TYPES = ("message/rfc822", "text/plain")


def _content_type(event):
    headers = event.get("headers") or {}
    # Payload format 2.0 lowercases header names; the fallback is for hand-built test events.
    raw = headers.get("content-type") or headers.get("Content-Type") or ""
    return raw.split(";")[0].strip().lower()


def _json_body(event):
    """Decode the request body into the dict the routes expect. Returns (body_dict, problem).

    Three input forms end up as one dict:

        {"email": {...}} / {"text": "..."}      a JSON object, the normal form
        raw bytes + content-type: message/rfc822 (or text/plain)  -> {"text": <the body>}
        ?region=...&modelId=...                 merged over either of the above

    The query string is merged UNDER the JSON body, not over it: a caller who sent both meant the
    body, and silently preferring the URL would be a surprising way to lose an explicit field.
    """
    raw = event.get("body") or ""
    if event.get("isBase64Encoded") and raw:
        raw = base64.b64decode(raw).decode("utf-8", "replace")

    if _content_type(event) in _RAW_EMAIL_TYPES:
        if not raw.strip():
            return None, ("empty body with content-type " + _content_type(event) +
                          ", which means the body should BE the email")
        body = {"text": raw}
    else:
        try:
            body = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            return None, (f"invalid JSON body: {exc}. To post a raw email instead, send it with "
                          f"content-type: message/rfc822 and options in the query string.")
        if not isinstance(body, dict):
            return None, "JSON body must be an object"

    query = event.get("queryStringParameters") or {}
    for key in _QUERY_OPTIONS:
        if key not in body and query.get(key) not in (None, ""):
            body[key] = query[key]

    return body, None


def lambda_handler(event, context):
    http = ((event.get("requestContext") or {}).get("http") or {})
    # CloudFront forwards /api/* verbatim and the API Gateway route is a $default
    # catch-all, so the raw path still carries the prefix. Missing this strip is a silent
    # 404 on every call -- including on the new /public/scan and /runs paths, which is why
    # the strip stays the single place the prefix is handled.
    path = (event.get("rawPath") or "").removeprefix("/api").rstrip("/") or "/"
    method = http.get("method") or ""

    allowed, denial = _origin_allowed(event)
    if not allowed:
        log.warning("blocked %s %s from %s", method or "?", path, http.get("sourceIp"))
        if path == PUBLIC_PATH:
            # Every reply on the public route carries CORS, this one included. A browser
            # cannot read a response without Allow-Origin, so a bare 403 here is
            # indistinguishable from a network failure -- and "you called the execute-api
            # hostname instead of the CloudFront one" is exactly the message that has to
            # get through.
            denial["headers"] = {**denial["headers"], **_CORS_HEADERS}
        return denial

    # The public route is dispatched ahead of the table because it is the only path that
    # answers two methods and the only one whose replies carry CORS headers.
    if path == PUBLIC_PATH:
        if method == "OPTIONS":
            return _public_preflight()
        if method and method != "POST":
            return _cors_reply(405, {"error": f"{path} is POST or OPTIONS, not {method}",
                                     "errorClass": "MethodNotAllowed"})
        body, problem = _json_body(event)
        if problem:
            return _cors_reply(400, {"error": problem, "errorClass": "BadRequest"})
        return _public_scan(event, body)

    route = _ROUTES.get(path)
    if route is None:
        return _reply(404, {"error": f"no route for {method or 'GET'} {path}"})

    verb, run = route
    # Enforced only when the event actually carries a method, so a hand-built test event
    # with no requestContext still reaches its route.
    if method and method != verb:
        return _reply(405, {"error": f"{path} is {verb}, not {method}"})
    if verb == "GET":
        return run()

    body, problem = _json_body(event)
    if problem:
        return _reply(400, {"error": problem})

    return run(body)
