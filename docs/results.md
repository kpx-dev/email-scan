# Email Scan PoC — Build and Measurement Results

**Deployed:** 2026-09-12 · **Account** 123456789012 · **Region** us-east-1
**URL:** https://email-scan.example.com · **Login:** `demo` / `<the cognito_demo_password from terraform.tfvars>`

Records what was actually built, what was measured, and the five places reality differed from
[`design.md`](design.md). Companion to [`tasks.md`](tasks.md).

---

## 1. Deployed resources

`terraform apply` created **24 resources**, plan reviewed create-only (0 to change, 0 to destroy).

| Resource | Identifier |
|---|---|
| CloudFront distribution | `E1EXAMPLEDIST0` → `dexample12345.cloudfront.net` |
| ACM certificate | `email-scan.example.com`, DNS-validated (issued in 31s) |
| Route53 A-alias | `email-scan.example.com` → distribution |
| S3 SPA bucket | `email-scan-frontend-123456789012` (OAC only) |
| API Gateway HTTP API | `email-scan-api`, `$default` route, JWT authorizer `email-scan-jwt` |
| Lambda | `email-scan`, python3.12, 512 MB, 29s timeout, **0 pip deps / 0 layers** |
| Cognito | pool `us-east-1_EXAMPLE1`, client `exampleclientid1234567890`, user `demo` CONFIRMED |
| CloudWatch Logs | `/aws/lambda/email-scan`, retention 14 days |
| CloudFront Function | `email-scan-basic-auth` — **not created** (`count = 0`, fallback off) |

**The two pre-existing stacks in this account were verified untouched** after apply — both still
`Deployed`.

## 2. Defence in depth — each layer verified independently

| Request | Expected | Actual |
|---|---|---|
| CloudFront `/api/health`, no token | 401 | **401** |
| CloudFront `/api/health`, valid JWT | 200 | **200** |
| API Gateway URL direct, no token | 401 | **401** (authorizer) |
| API Gateway URL direct, **valid JWT** | 403 | **403** (Lambda: no `x-origin-secret`) |
| S3 object URL direct | 403 | **403** (OAC only) |

The fourth row is the one that matters: it proves the origin-secret layer works independently of
the authorizer. Testing only the no-token case would have returned 401 and never exercised it.

## 3. Measured performance

`bench/driver.py`, calling Bedrock directly (no CloudFront, no API Gateway), SDK retries disabled,
5s warm-up discarded.

| | PRIMARY — Gemma 4 / mantle | FALLBACK — Gemma 3 27B / runtime |
|---|---|---|
| Concurrency | 4 | 8 |
| TTFT p50 | **381.5 ms** | **527.9 ms** |
| TTFT p95 | 716.1 ms | 713.5 ms |
| E2E p50 | **1082.4 ms** | 2587.7 ms |
| E2E p95 | 2124.8 ms | 4474.6 ms |
| Server-side latency p50 | n/a on this lane | 2517 ms |
| Output tok/s | 435.8 | 326.2 |
| Requests | 63 | 55 |
| **Error rate** | **0.0 %** | **0.0 %** |
| service_throttle | 0 | 0 |
| client_transport | 0 | 0 |
| application | 0 | 0 |

Against doc §6.5's success criteria: TTFT < 1s **met on both lanes**; Stage 1 E2E p50 < 3s **met on
both**; error rate < 0.1 % **met at 0 %**; sustained ≥ 700 RPS **not measured** — `report.py`
correctly refuses to claim it and prints *"EXTRAPOLATED, NOT MEASURED"*.

`InvocationThrottles` stayed at **zero** throughout, as in every earlier probe. The doc's §5.4
429/503 backoff guidance is implemented but has still never fired in this account.

## 4. Five places reality differed from the design

### 4.1 The two lanes have **disjoint EU regions** — new, and it matters

`design.md` and the first build both assumed Gemma 3 27B was available in eu-central-1. It is not:

```
aws bedrock list-foundation-models --region eu-central-1  ->  ZERO gemma models of any size
aws bedrock-runtime converse --region eu-central-1 --model-id google.gemma-3-27b-it
  -> ValidationException: The provided model identifier is invalid.
```

Combined with the primary lane's 404 in eu-west-1, the real picture is:

| Region | Primary (Gemma 4 / mantle) | Fallback (Gemma 3 / runtime) |
|---|---|---|
| us-east-1 | ✅ 489 ms | ✅ |
| eu-central-1 (Frankfurt) | ✅ 758 ms | ❌ no Gemma at all |
| eu-west-1 (Ireland) | ❌ 404 | ✅ |

**There is no single EU region where both lanes work.** A/B-ing the two models inside the EU means
running them in two different regions, which invalidates any latency comparison drawn there. This is
a genuine constraint on the EU story and belongs in the account-team conversation
alongside the residency questions in design §10.

Caught by `smoke.sh` step 3, which is exactly what that check exists for. Registry corrected;
the region dropdown now offers only pairs that are empirically true.

### 4.2 Stage 1 `maxTokens` 200 → **320**

Design §4.5 fixes Stage 1 at 200. Measured: Gemma 4 truncated **4 of 58** Stage 1 responses
mid-JSON (`stopReason=max_tokens`) when it returned all five ranked signals with their `detail`
strings. A truncated response is a parse failure, so the escalation gate fired on `parse_failed`
rather than on the model's actual verdict — silently escalating ~7 % of traffic for the wrong reason
and doubling its cost.

At 320 the application-error bucket went to **0**. Cost is roughly 150 ms of E2E on a 3s target we
beat by 2s. Changed in both `backend/handler.py` and `bench/driver.py` — they must track each other
or the harness stops measuring the request the demo sends.

### 4.3 `bedrockLatencyMs` is genuinely unavailable on the primary lane

tasks.md T1.3 flagged this as needing investigation. Resolved: the OpenAI-compatible response
carries no server-side latency field and **no `x-amzn-*` latency header** either. So
`bedrockLatencyMs` and `overheadMs` are `None` on the primary lane, and the UI renders those two
tiles as *"n/a on this lane"* — never `0`, which would read as fast.

Consequence: the model-vs-network latency attribution that design §5.1 relies on is only possible
on the **fallback** lane. That compounds the CloudWatch gap in §4.4 below.

### 4.4 `runtime_client` collides with a Lambda built-in — worked locally, 502'd deployed

The nastiest defect of the build, and it passed every local test. The fallback lane died in
production with:

```
AttributeError: module 'runtime' has no attribute 'converse'
```

Cause: inside the AWS Lambda python3.12 environment, **`runtime_client` is already in
`sys.modules`** — the runtime interface client aliases that name to a module whose `__name__` is
`runtime`. A cached `sys.modules` entry wins over `/var/task`, no matter that our file is first on
`sys.path`. So `import runtime_client` silently returned AWS's module, and `_TRANSPORTS["runtime"]`
pointed at something with no `converse` attribute.

It was invisible locally because no such module exists outside Lambda, so `smoke.sh` (which imports
the handler in-process) passed on the very code that was broken in production. Downloading and
diffing the deployed package showed identical source — which ruled out a stale zip and pointed at
the environment.

Fixed by renaming the module to **`converse_client.py`**, which also names the API rather than the
endpoint. The reason is recorded in that file's docstring so nobody renames it back.

**Fixed, not just noted:** `smoke.sh` now tests the **deployed** site through CloudFront with a real
Cognito login. A gate that does not exercise the real deployment is not a gate. `--local` keeps the
old in-process mode for pre-deploy iteration, and prints a warning saying it proves nothing about
production.

### 4.5 Login wiped the password field before reading it

Reported from the browser as *"Sign-in failed. Missing required parameter PASSWORD"*. The request
shape was correct; the field was empty by the time it was read.

`doSignIn` set the busy flag and called `render()` **before** reading the inputs. `render()` rebuilds
that section with `innerHTML`, which destroys the inputs and recreates them from the markup — where
the password is `value=""`. So Cognito received `PASSWORD: ""`, and it reports an empty required
string as *"Missing required parameter"*, which reads like a malformed request rather than a wiped
field.

Username masked the problem: its markup hardcodes `value="demo"`, so it survived the re-render and
only the password came back blank.

Confirmed by reproducing both cases directly against the IDP endpoint:

```
PASSWORD "<demo password>" ->  AuthenticationResult, IdToken 1034 chars
PASSWORD ""                ->  InvalidParameterException - Missing required parameter PASSWORD
```

Fixed by reading both fields before the first `render()`, plus an explicit empty-password guard so
the failure names itself instead of round-tripping to Cognito. **This class of bug is worth watching
for anywhere else in the page:** any handler that re-renders before reading DOM state has it.

## 5. Escalation rate — measured, but not yet meaningful

**85.5 %** across the 7-sample corpus, against the **15 %** design §6.2 and §9.3 assume.

This is a corpus-composition artifact, not a finding: the demo corpus is deliberately threat-heavy
(5 of 7 samples are malicious or suspicious by construction) because its job is to show the pipeline
working, not to be representative mail. The gate's reasons break down as
`verdict=malicious` 45, `url_present_despite_benign` 9.

**The real rate can only come from a real production mail mix**, and design §9.3 is right that it is
the single most valuable number this PoC can produce — it drives both the quota ask and the cost
model, with a 5 %→30 % swing moving daily cost by ~50 %. The harness now measures it; it needs real
input. Worth obtaining a representative sample.

## 6. Still open

| # | Item | Status |
|---|---|---|
| T0.2 | Lambda Function URL `RESPONSE_STREAM` permitted? | **Not run.** Skipped deliberately — it only unlocks the optional real-SSE upgrade, and API Gateway is the primary path. Two independent assertions in the user's own deployed code say SCPs block it, and no FURL exists in the account. |
| T0.3 | Minimal mantle IAM action | **Not run.** Both prefixes granted (`bedrock-mantle:*` + `bedrock:*`), which tasks.md documents as safe but not minimal. An Administrator session structurally cannot answer this. |
| T7.1 | Distributed 700 RPS run | **Not run.** Needs sign-off, ~$400. Until then sustained RPS is extrapolated. |
| R3 | Zero CloudWatch metrics for Gemma 4 | Confirmed again. Cross-check possible only on the fallback lane (14 metrics vs 0). |
| R4 | No PrivateLink for `*.api.aws` | Unchanged. Could invalidate the primary lane post-PoC if residency forbids internet transit. |
| R5 | `data_retention: provider_data_share` on the Gemma 4 catalog entry | Unchanged, unanswered. Should be settled **before** any real consumer email is scanned. |

## 6a. Public API endpoint and run history (added 2026-09-13)

### The endpoint

```bash
curl -X POST https://email-scan.example.com/api/public/scan \
  -H "x-api-key: $(cd infra && terraform output -raw public_api_key)" \
  -H 'Content-Type: application/json' \
  -d '{"text":"From: ceo@acme.example.com\nSubject: Wire transfer\n\nSend $48,500 today."}'
```

Accepts either `{"email":{"from","subject","body","attachments"}}` or `{"text":"<raw email>"}` —
the text form parses leading `From:`/`Subject:`/`To:` headers and treats the rest as the body.
Optional `modelId` and `region`. Returns the normal scan body plus `runId`.

**Reachable from anywhere**, no Cognito, CORS open (`Access-Control-Allow-Origin: *`) so browser
callers work. It must be called through the CloudFront domain, not the `execute-api` hostname —
CloudFront injects the `x-origin-secret` the Lambda still requires.

### Verified behaviour

| Case | Result |
|---|---|
| Valid key, structured email | **200** + `runId`, verdict, timings |
| Valid key, `{"text": ...}` form | **200**, headers parsed out of the text |
| Wrong key | **401** |
| No key | **401** |
| `OPTIONS` preflight | **204** with `access-control-allow-origin: *` |
| Over daily cap | **429**, `"3 of 3 used"`, `Retry-After: 23297`, CORS headers present |
| `GET /api/runs` unauthenticated | **401** (stays behind the Cognito JWT) |

### Three independent brakes

1. **API Gateway route throttle** — 10 req/s, 20 burst, on `POST /api/public/scan` only. Rejected
   before the Lambda is invoked. UI routes get a separate 50/100 default so a demo is never
   rate-limited by an abuse control aimed at strangers.
2. **`x-api-key`**, compared with `hmac.compare_digest` on bytes, failing closed if the env var is
   missing. 401 rather than 403, deliberately — 403 already means "missing origin secret", and
   conflating the two makes debugging miserable.
3. **Lambda daily cap** — 5,000/day, one atomic conditional `UpdateItem`. Brake 1 alone is not
   enough: 10 req/s sustained for 24h is 864,000 scans.

The cap increment is **conditional on being under the cap**, so the counter parks at the ceiling
instead of climbing while someone hammers a rejected key. The first implementation used an
unconditional `ADD`, which was equally safe but reported nonsense — *"5 of 2 used"* — and that
number is what the 429 body and the UI show. The condition is evaluated server-side inside the same
`UpdateItem`, so atomicity is unchanged.

### Persistence

DynamoDB `email-scan-runs`, PAY_PER_REQUEST, single table:

- `pk="RUN"`, `sk="<iso8601>#<8-hex>"` — one row per scan; a descending Query on one partition is
  "most recent first" with no GSI. Fine at PoC volume; would need a date-sharded `pk` at scale.
- `pk="QUOTA"`, `sk="<YYYY-MM-DD>"` — the atomic daily counter, same table so there is one resource
  to provision and one to tear down.
- `expiresAt` TTL, **verified 7.0 days out**, TTL status ENABLED.

IAM is scoped to that table ARN with exactly `PutItem`, `GetItem`, `UpdateItem`, `Query` — no
`Scan`, no `DeleteItem`, no wildcards.

**Every run is recorded, from both entry points** — verified live: `{'ui': 2, 'api': 14}`.

### Data at rest — the open question this widens

Per your decision, each run stores the **complete email**: full sender address, subject, body and
attachment names. That is what makes the run-history tab (now SCAN RESULTS) able to show what was
actually scanned, and it
means **consumer email content sits at rest in DynamoDB for 7 days** while design §10 R5 — the
account-level `data_retention: provider_data_share` mode on the Gemma 4 catalog entry — is still
open and unanswered. The TTL bounds the exposure; it does not remove it.

This should be settled before any real consumer mail goes through the public endpoint. Switching
to metadata-only is a small change: stop populating the `email` field in `runs_store.record_run`.

### The edge trap this would have hit

CloudFront's `/api/*` behaviour uses a legacy `forwarded_values` header **allowlist**, which was
`["Authorization", "Content-Type"]`. `x-api-key` was therefore being **stripped at the edge**, and
every public call would have 401'd for a reason nobody would guess from the error. Confirmed against
the live distribution before building, and `x-api-key` added to the allowlist.

Also confirmed live rather than assumed: the api origin sets no `OriginPath`, so CloudFront forwards
the viewer path verbatim and the gateway route keys must carry the literal `/api` prefix
(`POST /api/public/scan`). `GET /api/api/health` returning `no route for GET /api/health` is the
proof — the Lambda strips one `/api` and echoes the remainder. A route key of `POST /public/scan`
would never match and would fall through to `$default` → JWT → 401.

### UI

> **Superseded 2026-09-13.** RUNS has been split into **SCAN RESULTS** (the run history and the
> session strip) and **API INTEGRATION** (endpoint, credentials, limits, worked curl and Python),
> and MIGRATION was removed entirely. The description below is what was built at the time.

A fourth tab, **RUNS**, between BENCHMARK and MIGRATION: the public endpoint panel with a
copy-to-clipboard curl and live `N / 5000 today` usage, a summary strip (totals by source, escalated
count, p50 TTFT), and a table of every run — expandable to the full stored email, Stage 1 signals,
and Stage 2 action. The stored body is attacker-controlled text from a public endpoint, so it is
escaped through the existing `esc()` helper on render; that is a security requirement, not polish.

## 7. Reproducing

```bash
export AWS_PROFILE=aws AWS_REGION=us-east-1

./deploy.sh                 # plan review gate, then apply; refuses on any destroy/update
./smoke.sh                  # MANDATORY before a demo — cold TTFT is ~4x warm
python3 bench/driver.py --lane primary  --concurrency 4 --duration 25
python3 bench/driver.py --lane fallback --concurrency 8 --duration 25
python3 bench/report.py bench/results/results.json
```

`deploy.sh` will refuse to apply if the plan destroys or modifies anything, if it references the
live demos, or if `terraform.tfvars` is missing `project = "email-scan"`.

### Interpreter note

**`python3.12` is not installed on this machine** — local Python is 3.14.7 (Homebrew) with 3.9.6 as
the system fallback, so `smoke.sh` and `bench/driver.py` fall through to 3.14.7. The Lambda runs
**3.12**.

This is now covered for the application code, because `smoke.sh` in its default remote mode
exercises the deployed 3.12 Lambda rather than a local import — which is precisely the gap that let
the §4.4 collision through. It remains a caveat on the **benchmark numbers only**: `bench/driver.py`
calls Bedrock directly from the laptop, so its TTFT and E2E figures carry 3.14.7's HTTP stack, not
3.12's. The measured deltas are far smaller than the margins we are reporting against, but install
3.12 before quoting these numbers as production-representative.
