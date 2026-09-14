"""DynamoDB persistence for scan runs, and the public endpoint's daily quota counter.

One table (env `RUNS_TABLE`), two item kinds, no GSI:

    pk "RUN"    sk "<iso8601>#<8-hex runId>"   one row per scan. The sort key is
                                               chronological, so a descending Query on a
                                               single partition is "most recent first"
                                               with no secondary index to maintain. One
                                               constant partition is fine at PoC volume
                                               and would need a date-sharded pk at scale.
    pk "QUOTA"  sk "<YYYY-MM-DD>"              one atomically-incremented counter per UTC
                                               day, in the same table so there is one
                                               resource to provision and one to tear down.

Every item carries `expiresAt`, an epoch-seconds TTL 7 days out, so the table drains
itself and nothing here needs a cleanup job.

DATA AT REST -- stated once, factually. Every run stores the complete email: full sender
address, subject, body, and attachment names, alongside the verdict, timings and token
usage. That is a deliberate product decision -- the SCAN RESULTS tab has to be able to show what
was actually scanned -- and it means consumer email content sits at rest in DynamoDB for
7 days while design 10 R5 (the account-level `data_retention: provider_data_share` mode
on the Gemma 4 catalog entry) is still open and unanswered. The TTL bounds the exposure;
it does not remove it.

Failure posture, and it is deliberately asymmetric:

  * `record_run` and `list_runs` never raise into the request path. A failed write logs
    and returns None; losing a history row must not fail a scan the user asked for.
  * `check_and_bump_daily` fails **closed**. It is the only thing standing between a
    leaked API key and an unbounded overnight bill, so when the counter cannot be read the
    request is refused rather than waved through. It signals that case with `count == -1`
    so the caller can answer 503 ("counter unavailable") instead of 429 ("you are over
    the cap"), which are very different things to be told.

Zero new dependencies: boto3 ships in the python3.12 Lambda runtime (design 1.3).
"""

import logging
import math
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

log = logging.getLogger()

RUN_PK = "RUN"
QUOTA_PK = "QUOTA"

# Design 10 R5 is open, so retention is short and enforced by the table rather than by a
# promise: 7 days, as an epoch-seconds TTL attribute DynamoDB deletes on.
TTL_DAYS = 7
TTL_SECONDS = TTL_DAYS * 24 * 60 * 60

# Lambda-enforced ceiling on the public endpoint, on top of API Gateway's route-level
# 10 req/s + 20 burst. The gateway limits the *rate*; this limits the *day*, which is the
# only one of the two that stops a leaked key running an unbounded bill overnight.
DEFAULT_DAILY_CAP = 5000

# Full email bodies are stored, and a DynamoDB item is hard-capped at 400 KB. A body over
# this is truncated with a visible marker rather than allowed to fail the whole PutItem,
# because a truncated row is worth more than no row. 100k chars is ~20x the 2-5 KB the
# requirements doc 2.4 specifies.
MAX_BODY_CHARS = 100_000

# Short and few. Every call here sits inside a request whose whole budget is 25s of read
# timeout (T3.5's chain), so a slow table must not be what blows it: worst case is
# ~3 attempts x 2s = 6s, not the SDK default's tens of seconds.
_CONFIG = Config(retries={"max_attempts": 3, "mode": "standard"},
                 connect_timeout=1, read_timeout=2)

# Module scope, so a warm invocation reuses the client, its credential resolution and its
# TLS connection instead of rebuilding all three (design 5.1 "connection reuse").
_RESOURCE = None


def _table_name():
    return (os.environ.get("RUNS_TABLE") or "").strip()


def _table():
    """The DynamoDB Table, or None when it cannot be built. Never raises.

    None is a real, handled state rather than an error: every caller below degrades on it,
    so a deploy that has not yet been given a table still serves scans -- it just cannot
    remember them.

    Constructing the resource is inside the try for a reason. `boto3.resource` is not a
    pure lookup -- it loads the DynamoDB resource model and resolves a region -- so it can
    raise, and it is called by all four public functions *outside* their own try blocks.
    An exception escaping here would bypass every failure posture documented in the module
    docstring at once: `record_run` would fail the user's scan, `list_runs` would 500
    the SCAN RESULTS tab, and `check_and_bump_daily` would return a bare 502 instead of the 503
    that says the counter is unavailable. Returning None routes all three back through the
    paths that were designed for a missing table.
    """
    global _RESOURCE
    name = _table_name()
    if not name:
        return None
    try:
        if _RESOURCE is None:
            region = (os.environ.get("AWS_REGION")
                      or os.environ.get("AWS_DEFAULT_REGION")
                      or "us-east-1")
            _RESOURCE = boto3.resource("dynamodb", region_name=region, config=_CONFIG)
        return _RESOURCE.Table(name)
    except Exception:  # noqa: BLE001 -- see the docstring; None is the handled state
        log.exception("runs_store: could not build a DynamoDB client for table %s", name)
        return None


# ---------------------------------------------------------------------------
# Time and type helpers
# ---------------------------------------------------------------------------
def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    """ISO8601 UTC with milliseconds and a `Z` suffix: 2026-09-13T04:05:06.789Z.

    Fixed width and lexically sortable, which is what makes it safe as the leading segment
    of the sort key.
    """
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _next_utc_midnight(now=None):
    now = now or _now()
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def seconds_until_reset(now=None):
    """Whole seconds until the daily counter rolls over. Feeds the 429's Retry-After."""
    now = now or _now()
    return max(1, int((_next_utc_midnight(now) - now).total_seconds()))


def daily_cap(cap=None):
    """The effective daily cap: the argument, else the env var, else 5000.

    `PUBLIC_DAILY_CAP` is the name infra/lambda.tf actually sets (from
    var.public_daily_cap), so it is read first. `DAILY_CAP` is accepted as an alias
    because that is the name this module shipped with, and a deploy that still carries
    the old variable must not silently fall back to the 5000 default and quietly ignore a
    tuned cap. Lambda env vars are always strings, hence the int().
    """
    raw = cap
    if raw is None:
        raw = os.environ.get("PUBLIC_DAILY_CAP") or os.environ.get("DAILY_CAP")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_DAILY_CAP


def _ddb_safe(value):
    """Coerce a JSON-ish value into what DynamoDB accepts.

    The only real trap is `float`: the resource API rejects it outright and demands
    Decimal, and `confidence`, every timing and `otps` are all floats. Non-finite floats
    become None, because Decimal('NaN') is accepted by Python and then rejected by
    DynamoDB -- one malformed number must not cost the whole row.
    """
    if value is None or isinstance(value, (bool, int, str, Decimal)):
        return value
    if isinstance(value, float):
        return Decimal(str(value)) if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _ddb_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_ddb_safe(v) for v in value]
    return str(value)


def _json_safe(value):
    """The inverse: Decimal -> int/float, so a read item is `json.dumps`-able."""
    if isinstance(value, Decimal):
        as_float = float(value)
        return int(as_float) if as_float.is_integer() else as_float
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Run items
# ---------------------------------------------------------------------------
_TIMING_KEYS = ("ttftMs", "e2eMs", "bedrockLatencyMs", "overheadMs", "otps")
_USAGE_KEYS = ("inputTokens", "outputTokens", "totalTokens")
_STAGE2_KEYS = ("recommendedAction", "riskScore", "disagreesWithStage1")

# Which route recorded the row. Whitelisted rather than passed through, so a caller cannot
# invent a source and make the table's badge meaningless:
#   ui          the /scan pipeline: Stage 1, the gate, and Stage 2 when it fired
#   ui-simple   the Simple Scan button: POST /classify, one stage
#   ui-deep     the Deep Scan button: POST /deep-scan, one stage, no prior context
#   api         POST /public/scan, the key-authenticated route
_SOURCES = ("ui", "ui-simple", "ui-deep", "api")

# The stage names a row may claim to have run. Same reasoning as _SOURCES.
_STAGE_NAMES = ("classify", "deep-scan")


def _email_item(email):
    """The stored email: full content, per the product decision in the module docstring."""
    email = email if isinstance(email, dict) else {}
    body = email.get("body") or ""
    if not isinstance(body, str):
        body = str(body)
    if len(body) > MAX_BODY_CHARS:
        # Self-describing inside the value itself, so a reader of the row can never
        # mistake a truncated body for the whole email.
        body = (body[:MAX_BODY_CHARS]
                + f"\n\n[...truncated for storage at {MAX_BODY_CHARS} characters...]")

    attachments = email.get("attachments") or []
    if isinstance(attachments, str):
        attachments = [attachments]

    item = {
        "from": email.get("from") or "",
        "subject": email.get("subject") or "",
        "body": body,
        "attachments": [str(a) for a in attachments if a],
    }
    # Additive and only when present: a pasted email may carry a To: line, and dropping it
    # would mean the stored row is not what was scanned.
    if email.get("to"):
        item["to"] = str(email["to"])
    return item


def _run_item(payload, run_id, ts, expires_at):
    stage2 = payload.get("stage2")
    signals = payload.get("signals") or []
    if not isinstance(signals, list):
        signals = []

    return {
        "pk": RUN_PK,
        # Chronological, so ScanIndexForward=False is "newest first". The runId suffix
        # keeps two runs landing in the same millisecond from colliding.
        "sk": f"{ts}#{run_id}",
        "runId": run_id,
        "ts": ts,
        "source": payload.get("source") if payload.get("source") in _SOURCES else "ui",
        # Which stages actually ran, in order. Without it a standalone Deep Scan row is
        # indistinguishable from a pipeline row whose Stage 1 fields happened to be empty.
        "stages": [s for s in (payload.get("stages") or []) if s in _STAGE_NAMES],
        "modelId": payload.get("modelId"),
        "modelLabel": payload.get("modelLabel"),
        "endpoint": payload.get("endpoint"),
        # The Bedrock host the call actually went to, verbatim from the handler.
        "servedVia": payload.get("servedVia"),
        "region": payload.get("region"),
        # A row run on an edited system prompt is not comparable to the rest of the table, and
        # nothing else in the row records that.
        "promptOverridden": bool(payload.get("promptOverridden")),
        "verdict": payload.get("verdict"),
        "confidence": payload.get("confidence"),
        "signals": [
            {"signal": str(s.get("signal") or ""), "detail": str(s.get("detail") or "")}
            for s in signals if isinstance(s, dict)
        ],
        "parseError": payload.get("parseError"),
        "escalated": bool(payload.get("escalated")),
        "escalationReason": payload.get("escalationReason"),
        "stage2": ({k: (stage2 or {}).get(k) for k in _STAGE2_KEYS}
                   if isinstance(stage2, dict) else None),
        "timings": {k: (payload.get("timings") or {}).get(k) for k in _TIMING_KEYS},
        "usage": {k: (payload.get("usage") or {}).get(k) for k in _USAGE_KEYS},
        "email": _email_item(payload.get("email")),
        "expiresAt": expires_at,
    }


def record_run(payload):
    """Persist one run. Returns the 8-hex runId, or None if it was not stored.

    Called on both the UI route (`source: "ui"`) and the public API route
    (`source: "api"`), after the scan has already succeeded. It must never raise: a
    history row is worth less than the scan the user just paid for, so every failure
    -- no table configured, throttled, item too large, credentials -- is logged and
    swallowed. The None return is honest rather than convenient: the caller reports
    `runId: null` instead of handing back an id that names a row nobody wrote.
    """
    run_id = uuid.uuid4().hex[:8]
    now = _now()
    ts = _iso(now)

    table = _table()
    if table is None:
        log.warning("runs_store: RUNS_TABLE is unset, dropping run %s", run_id)
        return None

    item = _run_item(payload or {}, run_id, ts, int(time.time()) + TTL_SECONDS)
    try:
        table.put_item(Item=_ddb_safe(item))
    except Exception:  # noqa: BLE001 -- history must never fail a scan
        log.exception("runs_store: failed to record run %s", run_id)
        return None
    return run_id


def _public_run(item):
    """Strip the storage-only keys. pk/sk/expiresAt are how the table works, not data."""
    return {k: _json_safe(v) for k, v in item.items()
            if k not in ("pk", "sk", "expiresAt")}


def list_runs(limit=100):
    """Every run, most recent first. Returns [] rather than raising on any failure.

    One Query, descending, on the single "RUN" partition -- which is the whole reason the
    sort key leads with a timestamp. Note that DynamoDB caps a Query page at 1 MB and
    these rows carry full email bodies, so a large `limit` can come back short. That is
    fine for the SCAN RESULTS tab and is not paginated on purpose: an unbounded read of stored
    email content is not something a PoC endpoint should offer.
    """
    try:
        limit = max(1, min(int(limit or 100), 500))
    except (TypeError, ValueError):
        limit = 100

    table = _table()
    if table is None:
        log.warning("runs_store: RUNS_TABLE is unset, returning no runs")
        return []

    try:
        response = table.query(
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": RUN_PK},
            ScanIndexForward=False,   # newest first
            Limit=limit,
        )
    except Exception:  # noqa: BLE001 -- an empty history beats a 500 on the UI's tab
        log.exception("runs_store: failed to list runs")
        return []

    return [_public_run(item) for item in (response.get("Items") or [])]


# ---------------------------------------------------------------------------
# Daily quota
# ---------------------------------------------------------------------------
def check_and_bump_daily(cap=None):
    """Increment today's counter and say whether the request is inside the cap.

    Returns (allowed, count, cap, resets_at). `count == -1` means the counter itself was
    unavailable, which is not the same thing as being over the cap and should not be
    reported as a 429.

    ONE UpdateItem does the increment and the comparison. That is the entire point: a
    read-then-write is a race, and under the concurrency this endpoint is throttled to
    (10 req/s at the gateway) the cap would leak by however many requests are in flight.
    `ADD` also creates the item on first use, so there is no separate initialisation path.

    The increment is CONDITIONAL on being under the cap, so the counter stops at the cap
    instead of climbing forever while a caller keeps hammering a rejected key. An
    unconditional ADD is still atomic and still safe, but it reports nonsense once you are
    over -- "5 of 2 used" -- and that number is what the UI and the 429 body show. Stopping
    at the ceiling keeps the reported figure meaningful without giving up atomicity: the
    condition is evaluated server-side inside the same UpdateItem, so there is no race.
    ConditionalCheckFailedException is the over-cap signal, not an error.
    """
    cap = daily_cap(cap)
    now = _now()
    day = now.strftime("%Y-%m-%d")
    resets_at = _iso(_next_utc_midnight(now))

    table = _table()
    if table is None:
        # Fail closed. Without a counter there is no cap, and an uncapped public endpoint
        # behind a shared key is the risk this whole mechanism exists to bound.
        log.error("runs_store: RUNS_TABLE is unset, refusing public request (no quota counter)")
        return False, -1, cap, resets_at

    try:
        response = table.update_item(
            Key={"pk": QUOTA_PK, "sk": day},
            UpdateExpression="SET #e = :expires ADD #c :one",
            # Only increment while we are still under the cap. First call of the day has no
            # `count` attribute at all, hence the attribute_not_exists arm.
            ConditionExpression="attribute_not_exists(#c) OR #c < :cap",
            ExpressionAttributeNames={"#c": "count", "#e": "expiresAt"},
            ExpressionAttributeValues={
                ":one": Decimal(1),
                ":cap": Decimal(cap),
                # Re-stamped on every bump, which is harmless and keeps the counter
                # self-cleaning without a second call to create it.
                ":expires": Decimal(int(time.time()) + TTL_SECONDS),
            },
            ReturnValues="UPDATED_NEW",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            # Over the cap. Not an error: this is the mechanism working, and the counter is
            # left parked at the ceiling so the reported figure stays truthful.
            return False, cap, cap, resets_at
        log.exception("runs_store: daily quota check failed, refusing request")
        return False, -1, cap, resets_at
    except Exception:  # noqa: BLE001 -- fail closed, see the docstring
        log.exception("runs_store: daily quota check failed, refusing request")
        return False, -1, cap, resets_at

    try:
        count = int((response.get("Attributes") or {}).get("count") or 0)
    except (TypeError, ValueError):
        log.error("runs_store: quota counter returned a non-numeric count, refusing request")
        return False, -1, cap, resets_at

    return count <= cap, count, cap, resets_at


def daily_usage(cap=None):
    """Today's usage for the admin view: {"date", "count", "cap"}. Read-only -- no bump.

    Deliberately does not go through check_and_bump_daily: the UI opening its SCAN RESULTS tab
    must not consume a user's quota.
    """
    cap = daily_cap(cap)
    usage = {"date": _now().strftime("%Y-%m-%d"), "count": 0, "cap": cap}

    table = _table()
    if table is None:
        return usage

    try:
        response = table.get_item(Key={"pk": QUOTA_PK, "sk": usage["date"]})
        usage["count"] = int((response.get("Item") or {}).get("count") or 0)
    except Exception:  # noqa: BLE001 -- a missing number must not break the tab
        log.exception("runs_store: failed to read the daily counter")
    return usage
