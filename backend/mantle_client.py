"""Gemma 4 lane: SigV4-signed, OpenAI-compatible SSE over the stdlib.

The primary lane, and the only transport that reaches the target model
(design 3.1). `google.gemma-4-26b-a4b` lives *only* behind

    POST https://bedrock-mantle.{region}.api.aws/openai/v1/chat/completions

with plain SigV4 and signing service name **`bedrock`** -- not `bedrock-mantle`, and no
API key. The routing rule is mutually exclusive and unforgiving: `/v1/chat/completions`
on the same host returns 400 "model `google.gemma-4-26b-a4b` isn't supported on this
route", which reads like a model-availability problem. The tuple lives in
model_registry; this module just sends what it is handed.

Zero pip dependencies (design 1.3): `botocore.auth.SigV4Auth` and `http.client` are both
already in the python3.12 Lambda runtime, so the function packages with Terraform's
`archive_file`. Do not hand-roll SigV4 when botocore ships a signer.

Timing model, identical to the fallback lane so the UI needs no lane awareness:
    t0       request issued
    t_first  first *non-empty* content delta        -> ttftMs
    t_end    stream exhausted                      -> e2eMs
    otps     output tokens / (t_end - t_first)

Three things about this route are non-obvious and were all verified live:

  1. `stream_options: {"include_usage": true}` is **mandatory**. Without it the streamed
     response carries no `usage` object at all -- every mid-stream chunk sends
     `"usage": null` and no final usage chunk arrives -- which silently zeroes input and
     output tokens, the cache counter, *and* otps (gated on output tokens). That is the
     single easiest way to ship a demo whose cost panel reads zero.

  2. There is **no server-reported latency** on this lane. Verified by dumping every
     response header for both a buffered and a streamed 200: Date, Content-Type,
     x-amzn-requestid, x-request-id, vary, access-control-*, cache-control -- and no
     trailers after the chunked body. There is no equivalent of Converse's
     `metadata.metrics.latencyMs`, so `bedrockLatencyMs` and `overheadMs` are `None`
     here. `_LATENCY_HEADERS` is still checked first in case one appears later; nothing
     is fabricated when it does not. The UI must render those two tiles as
     "n/a on this lane" -- never as 0 (design 11, correction 11).

  3. The stream opens with a priming delta whose `content` is the empty string (role
     only), and closes with `finish_reason` on an empty delta, then a `choices: []`
     usage chunk, then `data: [DONE]`. TTFT is stamped on the first delta with actual
     text: counting the priming frame would measure the handshake and report a
     flatteringly low TTFT.

The per-delta `timeline` is captured here rather than reconstructed later -- T5.3's
"replayed from server timeline" needs the real inter-token gaps, and concatenating the
chunks throws them away.
"""

import http.client
import json
import queue
import threading
import time

import botocore.session
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

import model_registry

# Verified: the signing service name is `bedrock` even though the host and the IAM action
# prefix are both `bedrock-mantle`.
SIGNING_SERVICE = "bedrock"

# Timeout chain (T3.5): read 25s < Lambda 29s < API Gateway 30s < CloudFront 120s. The
# innermost limit must fire first so the caller gets a readable error instead of a bare
# gateway 504 while the Lambda keeps billing. The prior art's 90s read timeout inverts it.
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 25

# Idle connections held per region. Mantle resets connections from a single client at
# concurrency >= 50 (design 6.4), and the harness caps this lane at 40, so 40 is the
# useful ceiling; anything beyond it is closed rather than pooled.
POOL_MAXSIZE = 40

# Checked first on every response, in case this route ever grows a server-latency header.
# None of these are present today -- see the module docstring.
_LATENCY_HEADERS = ("x-amzn-bedrock-invocation-latency", "x-amzn-invocation-latency",
                    "x-amzn-model-latency", "x-amzn-latency")

# Credential *resolution* happens once, at import: walking the provider chain (SSO cache,
# container credential endpoint) is the expensive part and a warm container must not repay
# it. `get_frozen_credentials()` is then called per request -- it returns a cached snapshot
# and only re-resolves near expiry, so a long-lived container never signs with stale keys.
_SESSION = botocore.session.get_session()
_CREDENTIALS = _SESSION.get_credentials()

# region -> pool of idle keep-alive connections. LIFO so the hottest socket is reused
# first, which is also the one least likely to have been reaped.
_pools = {}
_pools_lock = threading.Lock()


class MantleError(RuntimeError):
    """A mantle call that did not produce a usable response.

    Carries `status` (None for a transport failure), `error_class` and `request_id` so
    handler.py can return a machine-readable `errorClass` -- an AccessDeniedException
    from a missing IAM action, a ValidationException from a bad `response_format`, and a
    real model error must be distinguishable, not all flattened into one 502. The
    benchmark harness buckets on the same two fields: 429/503 is a service throttle,
    `status is None` is client transport.
    """

    def __init__(self, message, status=None, error_class=None, request_id=None, body=""):
        super().__init__(message)
        self.status = status
        self.error_class = error_class or (f"HTTP{status}" if status else "TransportError")
        self.request_id = request_id
        self.body = body


def _host(region):
    return f"bedrock-mantle.{region}.api.aws"


def _pool(region):
    with _pools_lock:
        pool = _pools.get(region)
        if pool is None:
            pool = _pools[region] = queue.LifoQueue(maxsize=POOL_MAXSIZE)
        return pool


def _connect(host):
    """Open a connection with separate connect and read timeouts.

    http.client takes one socket timeout, so connect under CONNECT_TIMEOUT and then
    relax the socket to READ_TIMEOUT for the streamed body.
    """
    conn = http.client.HTTPSConnection(host, timeout=CONNECT_TIMEOUT)
    conn.connect()
    conn.sock.settimeout(READ_TIMEOUT)
    return conn


def _checkout(region, host):
    """Return (conn, reused). `reused` decides whether a failure is safely retryable."""
    try:
        return _pool(region).get_nowait(), True
    except queue.Empty:
        return _connect(host), False


def _checkin(region, conn):
    try:
        _pool(region).put_nowait(conn)
    except queue.Full:
        conn.close()


def _discard(conn):
    try:
        conn.close()
    except OSError:
        pass


def warm(region=None):
    """Pay the credential-chain and TLS-handshake cost before the first real request.

    Cold TTFT is ~1.6s against ~0.4s warm, a 4x effect (design 5.2), and part of that gap
    is this module's setup rather than Bedrock. Costs nothing: no inference call is made.
    """
    region = region or model_registry.DEFAULT_REGION
    if _CREDENTIALS is not None:
        _CREDENTIALS.get_frozen_credentials()
    _checkin(region, _connect(_host(region)))


def _signed_headers(region, host, path, body):
    if _CREDENTIALS is None:
        raise MantleError(
            "no AWS credentials available to sign the mantle request; the Lambda "
            "execution role or AWS_PROFILE is not resolving"
        )
    url = f"https://{host}{path}"
    # Host is set before signing and sent verbatim afterwards: SigV4 signs the host it
    # derives from the URL, so letting http.client add its own leaves room for a mismatch.
    request = AWSRequest(method="POST", url=url, data=body,
                         headers={"Content-Type": "application/json", "Host": host})
    SigV4Auth(_CREDENTIALS.get_frozen_credentials(), SIGNING_SERVICE, region).add_auth(request)
    return dict(request.headers)


def _send(region, host, path, body, headers):
    """POST the signed request, returning (conn, response).

    Retries exactly once, and only on a *reused* connection that failed before any
    response byte arrived. Mantle reaps idle keep-alive sockets and the failure mode is
    ConnectionResetError (design 6.3); a socket that died before answering cannot have
    had the request processed, so re-sending is safe rather than a duplicate invocation.

    Connect and DNS failures land here too, so the harness sees them as one readable
    client-transport error class rather than a bare OSError from three call frames down.
    """
    for attempt in (1, 2):
        conn, reused = None, False
        try:
            conn, reused = _checkout(region, host)
            conn.request("POST", path, body=body, headers=headers)
            return conn, conn.getresponse()
        except (http.client.HTTPException, OSError) as exc:
            if conn is not None:
                _discard(conn)
            if reused and attempt == 1:
                continue
            raise MantleError(
                f"mantle POST https://{host}{path} failed before any response: "
                f"{type(exc).__name__}: {exc}",
                error_class=type(exc).__name__,
            ) from exc


def _raise_for_status(response, host, path, body_text):
    """Turn a non-200 into a readable exception carrying the status and a body excerpt.

    The error body is OpenAI-shaped -- {"error": {"code", "message", "type"}} -- e.g. a
    400 `validation_error` for a wrong path segment or a 404 `not_found_error` for
    Gemma 4 in eu-west-1.
    """
    detail = {}
    try:
        detail = (json.loads(body_text) or {}).get("error") or {}
    except (json.JSONDecodeError, AttributeError):
        pass
    error_class = detail.get("type") or detail.get("code") or f"HTTP{response.status}"
    message = detail.get("message") or body_text[:600].strip() or response.reason
    request_id = response.getheader("x-amzn-requestid")
    raise MantleError(
        f"mantle POST https://{host}{path} -> HTTP {response.status} "
        f"{error_class}: {message} [requestId={request_id}]",
        status=response.status,
        error_class=error_class,
        request_id=request_id,
        body=body_text[:2000],
    )


def _server_latency_ms(response):
    for name in _LATENCY_HEADERS:
        value = response.getheader(name)
        if value:
            try:
                return float(value)
            except ValueError:
                continue
    return None


def _drain(response):
    """Consume whatever follows `[DONE]` so the socket can be pooled. False if it can't."""
    try:
        if not response.isclosed():
            response.read()
        return not response.will_close
    except (http.client.HTTPException, OSError):
        return False


def chat(model, system_prompt, user_text, max_tokens, temperature=0,
         response_format=None, service_tier=None, region=None):
    """Invoke a mantle model over SSE and return content plus timing/usage.

    `model` is a registry entry from model_registry.MODELS. The return value is the same
    dict contract as converse_client.converse(), so handler.py and the frontend are
    transport-agnostic.

    No (model, region) validation happens here on purpose: registry.resolve() owns that,
    and T1.7's matrix test needs a real 404 out of this function to prove the registry's
    `regions` list is empirically honest rather than optimistic.
    """
    if model.get("endpoint") != "mantle":
        raise MantleError(
            f"{model.get('id')} is a {model.get('endpoint')!r}-lane model; "
            "route it to converse_client.converse() instead",
            error_class="LaneMismatch",
        )

    region = region or model.get("region") or model_registry.DEFAULT_REGION
    host, path = _host(region), model["path"]

    payload = {
        "model": model["id"],
        "messages": [{"role": "system", "content": system_prompt},
                     {"role": "user", "content": user_text}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
        # Mandatory -- see the module docstring. Omit it and every token counter reads 0.
        "stream_options": {"include_usage": True},
    }
    # Strict schemas are schema-exact, unfenced and fastest (design 4.3). Gated on the
    # registry flag: the Converse lane has no equivalent, so it never reaches this module.
    if response_format and model.get("supports_json_schema"):
        payload["response_format"] = response_format
    # Real and accepted, and it halves token cost -- but measured *slower* (1563ms flex vs
    # 961-1024ms default), so the UI frames it as cost, not latency (design 9.2).
    if service_tier:
        payload["service_tier"] = service_tier

    body = json.dumps(payload).encode("utf-8")
    headers = _signed_headers(region, host, path, body)

    t0 = time.perf_counter()
    conn, response = _send(region, host, path, body, headers)
    try:
        if response.status != 200:
            _raise_for_status(response, host, path, response.read().decode("utf-8", "replace"))

        t_first = None
        chunks = []
        timeline = []
        usage = {}
        stop_reason = None

        # SSE, line by line: `data: {json}` frames separated by blank lines, terminated by
        # the `data: [DONE]` sentinel. Anything else (comments, keepalives) is skipped.
        while True:
            line = response.readline()
            if not line:
                break
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[len(b"data:"):].strip()
            if data == b"[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue

            # A 200 can still carry an error frame mid-stream; surface it rather than
            # returning a truncated verdict as if it were complete.
            if chunk.get("error"):
                error = chunk["error"]
                raise MantleError(
                    f"mantle stream error: {error.get('message') or error}",
                    status=200,
                    error_class=error.get("type") or error.get("code") or "StreamError",
                    request_id=response.getheader("x-amzn-requestid"),
                    body=data.decode("utf-8", "replace")[:2000],
                )

            # `usage` is null on every content chunk and populated only on the final
            # `choices: []` frame, so last-one-wins is the correct read.
            if chunk.get("usage"):
                usage = chunk["usage"]

            for choice in chunk.get("choices") or []:
                text = (choice.get("delta") or {}).get("content") or ""
                if text:
                    now = time.perf_counter()
                    if t_first is None:
                        t_first = now
                    chunks.append(text)
                    # Per-delta, not concatenated: T5.3's replay needs the gaps.
                    timeline.append({"tMs": round((now - t0) * 1000, 1), "text": text})
                if choice.get("finish_reason"):
                    stop_reason = choice["finish_reason"]

        t_end = time.perf_counter()
        bedrock_latency_ms = _server_latency_ms(response)
        reusable = _drain(response)
    except BaseException:
        _discard(conn)
        raise

    if reusable:
        _checkin(region, conn)
    else:
        _discard(conn)

    raw = "".join(chunks)
    # `is not None`, not truthiness, on every t_first test below: perf_counter()'s epoch is
    # unspecified, so a legitimate 0.0 reading must not be mistaken for "no delta arrived".
    # Same falsy-zero bug class as overheadMs, and converse_client tests it the same way --
    # the two lanes must agree on when a timing is absent, not just on its name.
    ttft_ms = round((t_first - t0) * 1000, 1) if t_first is not None else None
    e2e_ms = round((t_end - t0) * 1000, 1)

    # OpenAI naming normalised to the shared contract here, not in the handler, so the UI
    # needs no lane awareness. cached_tokens is the 42%-hit opportunistic counter from
    # design 5.3; cache_write_tokens is present on this route and always 0 so far.
    prompt_details = usage.get("prompt_tokens_details") or {}
    in_tokens = usage.get("prompt_tokens", 0)
    out_tokens = usage.get("completion_tokens", 0)
    gen_seconds = (t_end - t_first) if t_first is not None else 0
    otps = round(out_tokens / gen_seconds, 1) if gen_seconds > 0 and out_tokens else None

    return {
        "raw": raw,
        "stopReason": stop_reason,
        "usage": {
            "inputTokens": in_tokens,
            "outputTokens": out_tokens,
            "totalTokens": usage.get("total_tokens", in_tokens + out_tokens),
            "cacheReadInputTokens": prompt_details.get("cached_tokens", 0),
            "cacheWriteInputTokens": prompt_details.get("cache_write_tokens", 0),
        },
        "timings": {
            "ttftMs": ttft_ms,
            "e2eMs": e2e_ms,
            # None on this lane, and honestly so: no header and no trailer carries it.
            "bedrockLatencyMs": bedrock_latency_ms,
            # Everything that isn't the model: network, TLS, Lambda. `is not None` rather
            # than truthiness, so a legitimate 0 does not read as "absent".
            "overheadMs": (round(e2e_ms - bedrock_latency_ms, 1)
                           if bedrock_latency_ms is not None else None),
            "otps": otps,
        },
        "timeline": timeline,
    }
