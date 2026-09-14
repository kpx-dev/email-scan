# Email Scan PoC — Build Tasks

Companion to [`design.md`](design.md). That document decides *what and why*; this one is the
executable *how*, in order, with the exact AWS resources, Terraform files, and verification
commands for each step.

**Account** 123456789012 · **Profile** `aws` (SSO) · **Region** us-east-1 · **Target**
https://email-scan.example.com

Every fact below was verified against the live account and the real prior-art files. Where the
inventory contradicted `design.md`, the correction is called out inline and listed in §9.

---

## 0. Conventions and preflight

### 0.1 Task format

Each task has an **ID**, a **Do**, a **Verify** (a command whose output proves it worked), and a
**Done when**. Do not advance past a failed Verify.

### 0.2 Estimates

Effort is engineer-hours of focused work. Total: **~30 h** across 8 phases, plus a gated ~2 h
throughput run. Phases 1–2 have no AWS dependency and can proceed in parallel with nothing.

### 0.3 Shell preamble — required in every terminal

```bash
export AWS_PROFILE=aws          # deploy.sh does NOT export this; the Cognito
export AWS_REGION=us-east-1     # local-exec provisioner inherits the shell, not the provider
aws sts get-caller-identity     # must show 123456789012 before anything else
```

### 0.4 The two rules that protect the pre-existing stacks

Other live stacks share this account and the single Route53 zone. Exactly two mistakes can damage
them:

1. **Never copy `terraform.tfstate`.** The prior-art `terraform/` directory holds a 54 KB local,
   unversioned `terraform.tfstate` (+ `.backup`) with **no remote backend**. A `cp -r terraform/ infra/`
   carries that stack's state in, and the next `apply` starts *managing and mutating* the stack it
   was copied from. **Copy `*.tf` files individually** (T3.1).
2. **Never leave a stale `domain_name` in `terraform.tfvars`.** `aws_route53_record` issues an
   **UPSERT**. A leftover `domain_name` inherited from the prior art silently re-points that
   stack's A-alias at our new distribution — and shows **no destroy in the plan**, so review will
   not catch it.

> **Trap the design doc missed:** `var.project` is **not set** in the prior-art `terraform.tfvars`.
> That stack runs on the variable's *default* value (`variables.tf:5-7`), and bucket, Lambda, IAM
> role, IAM policy, Cognito pool, Cognito client, API and OAC names **all** derive from it. If our
> tfvars omits `project`, Terraform tries to create resources under that default prefix and
> collides with the existing stack. **Setting `project = "email-scan"` is mandatory, not cosmetic.**

---

## 1. Phase 0 — Preflight and spikes (~2 h)

Two unknowns gate architecture decisions. Settle them **before** writing any Terraform.

### T0.1 — Confirm the account is clean for `email-scan`

**Do:** verify no colliding resource exists.

```bash
aws s3api head-bucket --bucket email-scan-frontend-123456789012 2>&1 | grep -q 404 && echo "bucket free"
aws acm list-certificates --region us-east-1 \
  --query "CertificateSummaryList[?DomainName=='email-scan.example.com']"      # -> []
aws route53 list-resource-record-sets --hosted-zone-id Z0144406FST7ZJQSDZNB \
  --query "ResourceRecordSets[?starts_with(Name,'email-scan')]"                # -> []
aws cloudfront list-distributions \
  --query "DistributionList.Items[?contains(Aliases.Items[0],'email-scan')]"   # -> []
aws iam get-role --role-name email-scan-lambda 2>&1 | grep -q NoSuchEntity && echo "role free"
```

**Done when:** all five report free. *(Verified free at design time; re-check, it is 30 seconds.)*

### T0.2 — SPIKE: is Lambda Function URL `RESPONSE_STREAM` permitted?

This decides whether the UI can truly stream (design §7.2). Evidence says no — the user's own
deployed code asserts it twice — in a prior-art stack's deploy script and in its Lambda handler —
and **zero** Function URLs exist in the account, but `organizations:ListPolicies`
is denied from this member account, so it cannot be read directly.

**Do:** create a throwaway function, add a Function URL with `RESPONSE_STREAM`, `curl -N` it, delete it.

```bash
# minimal throwaway
mkdir -p /tmp/furlspike && cat > /tmp/furlspike/index.mjs <<'EOF'
export const handler = awslambda.streamifyResponse(async (e, responseStream) => {
  for (let i = 0; i < 5; i++) { responseStream.write(`chunk ${i}\n`); await new Promise(r => setTimeout(r, 300)); }
  responseStream.end();
});
EOF
cd /tmp/furlspike && zip -q f.zip index.mjs
ROLE=$(aws iam get-role --role-name <any existing lambda execution role> --query Role.Arn --output text)
aws lambda create-function --function-name furl-spike-delete-me --runtime nodejs22.x \
  --handler index.handler --role "$ROLE" --zip-file fileb://f.zip --region us-east-1
aws lambda create-function-url-config --function-name furl-spike-delete-me \
  --auth-type NONE --invoke-mode RESPONSE_STREAM --region us-east-1
# -> if this errors with AccessDenied/explicit deny, the SCP blocks it. Record the verbatim error.
```

**Verify:** `curl -N <function-url>` shows chunks arriving 300 ms apart, not all at once.

**Teardown — do this immediately, in the same sitting:**

```bash
aws lambda delete-function-url-config --function-name furl-spike-delete-me --region us-east-1
aws lambda delete-function --function-name furl-spike-delete-me --region us-east-1
rm -rf /tmp/furlspike
```

**Done when:** the answer is recorded in `docs/results.md` as YES or NO with the verbatim error, and
the throwaway is deleted. **NO is the expected answer and costs us nothing** — API Gateway is the
primary path either way (T3.x). YES only unlocks the optional T5.5 upgrade.

### T0.3 — SPIKE: which IAM action actually authorises the mantle call?

Design §7.5 flags this as risk R1. Our SSO Administrator session allows everything, so it
*structurally cannot* answer the question. `iam simulate-custom-policy` cannot either — it returns
`allowed` for invented action names, so it validates nothing.

**Do:** create a deliberately least-privilege role, assume it, call mantle with only
`bedrock-mantle:*`; then repeat with only `bedrock:InvokeModel*`.

```bash
ACCT=123456789012
cat > /tmp/trust.json <<EOF
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
  "Principal":{"AWS":"arn:aws:iam::${ACCT}:root"},"Action":"sts:AssumeRole"}]}
EOF
cat > /tmp/mantle-only.json <<'EOF'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Resource":"*",
  "Action":["bedrock-mantle:CallWithBearerToken","bedrock-mantle:Get*",
            "bedrock-mantle:List*","bedrock-mantle:CreateInference"]}]}
EOF
aws iam create-role --role-name mantle-iam-spike-delete-me \
  --assume-role-policy-document file:///tmp/trust.json
aws iam put-role-policy --role-name mantle-iam-spike-delete-me \
  --policy-name mantle-only --policy-document file:///tmp/mantle-only.json
# assume it, then POST https://bedrock-mantle.us-east-1.api.aws/openai/v1/chat/completions
# with model google.gemma-4-26b-a4b and record 200 vs AccessDeniedException.
```

**Teardown:**

```bash
aws iam delete-role-policy --role-name mantle-iam-spike-delete-me --policy-name mantle-only
aws iam delete-role --role-name mantle-iam-spike-delete-me
```

**Done when:** the minimal sufficient action set is written into `docs/results.md` and into T3.6's
IAM policy. Until then T3.6 grants **both** prefixes, which is safe but not minimal.

> Note: Gemma 4 has **no foundation-model ARN** (`get-foundation-model` rejects the identifier), so
> the mantle statement **cannot** be resource-scoped to a model ARN. `Resource: "*"` is the only
> option, not laziness.

### T0.4 — Scaffold the repo

**Do:** create the tree from design §11.1. `git init` is already done (branch `main`, one commit).

```bash
cd "$(git rev-parse --show-toplevel)"
mkdir -p backend bench/corpus frontend infra docs
printf 'lambda.zip\nbackend.zip\n__pycache__/\n*.pyc\n.terraform/\n*.tfstate\n*.tfstate.*\ninfra/terraform.tfvars\nbench/results/\nfrontend/index_deploy.html\n' >> .gitignore
```

**Verify:** `git status --short` shows only intended additions.

**Note:** `__pycache__/` and `*.pyc` in `.gitignore` are **functionally required**, not tidiness —
see T3.7.

**Do NOT copy `plan.md` or `README.md` from the prior art.** Its `plan.md:559`
concludes *"Gemma 4 does not exist"* and `:6` picks eu-west-1 — both now known false. Leaving a
document that contradicts `design.md` on its two most load-bearing facts, in the same repo, is a
real hazard for whoever reads it next. `design.md` supersedes both.

**Pin the interpreter for local verification.** `backend/__pycache__/*.cpython-314.pyc` shows the
prior art last ran under **Python 3.14**; the Lambda runtime is **3.12**. Nothing in the code uses
3.13+ syntax, so it will run — but every local `python -c` check below must invoke **`python3.12`**
explicitly, or you are testing a different interpreter than production.

---

## 2. Phase 1 — Application code (~8 h)

Ported from an internal prior-art stack's `backend/` (verified working in us-east-1). All four files are
small: 153 + 241 + 111 + 68 = 573 lines total.

### T1.1 — `backend/model_registry.py` — rewrite (111 → ~90 lines)

**Do:** port from the prior art's `backend/model_registry.py`, changing:

| Change | Why |
|---|---|
| `REGION = "eu-west-1"` → `"us-east-1"` | **Functional, not cosmetic.** Verified: `google.gemma-4-26b-a4b` returns **HTTP 404 `not_found_error` in eu-west-1**, 200 in us-east-1 (489 ms) and eu-central-1 (758 ms). The prior art's region would silently break the primary lane. |
| Add `google.gemma-4-26b-a4b` as the primary entry | The target model |
| **Add keys** `endpoint`, `path`, `regions`, `supports_json_schema` | The `(model, endpoint, path, region)` tuple is the routing rule (design §3.2) |
| Drop `eu.anthropic.claude-haiku-…`, `eu.amazon.nova-lite-…` | `eu.` prefixes are invalid in us-east-1 |
| Drop `openai.gpt-oss-*` entries | Out of scope; keeps the A/B to one axis |
| Keep `supports_prompt_caching = False` on **every** Gemma | This flag is what stops a hard `AccessDeniedException` (design §5.3) |
| Keep `supports_latency_optimized = False` everywhere | `ValidationException` for all candidates (design §5.4) |
| Keep `resolve()` fallback-on-unknown-id behaviour — but **fail explicitly on a bad `(model, region)` pair** | Falling back on an unknown *model id* protects a live demo. Silently falling back on an unsupported *region* would make the region dropdown a lie. |
| Add `unsupported_reason` strings holding the **verbatim** `AccessDeniedException` / `ValidationException` text | T5.4 shows these on hover; the real error is more persuasive than a claim |

Target shape:

```python
REGION = "us-east-1"

MODELS = [
    {
        "id": "google.gemma-4-26b-a4b",
        "label": "Gemma 4 26B-A4B (Google) — the doc's model",
        "endpoint": "mantle",                              # bedrock-mantle.{region}.api.aws
        "path": "/openai/v1/chat/completions",             # NOT /v1/... -> 400
        "style": "openai",
        "regions": ["us-east-1", "eu-central-1"],          # eu-west-1 -> 404, verified
        "supports_json_schema": True,
        "supports_prompt_caching": False,   # automatic & opportunistic (42%); no parameter to send
        "supports_latency_optimized": False,
        "observed_latency_ms": 424,
        "notes": "Primary. Invisible to list-foundation-models. SigV4 service name 'bedrock'.",
    },
    {
        "id": "google.gemma-3-27b-it",
        "label": "Gemma 3 27B (Google) — fallback",
        "endpoint": "runtime",                             # bedrock-runtime, Converse/ConverseStream
        "path": None,
        "style": "converse",
        "regions": ["us-east-1", "eu-central-1", "eu-west-1"],
        "supports_json_schema": False,      # Converse has no equivalent -> fence parser required
        "supports_prompt_caching": False,   # cachePoint -> AccessDeniedException
        "supports_latency_optimized": False,
        "observed_latency_ms": 273,
        "notes": "Plain base model ID. NO us./eu. prefix — none exists for Gemma.",
    },
]
DEFAULTS = {"classify": "google.gemma-4-26b-a4b", "deep-scan": "google.gemma-4-26b-a4b"}
```

**Verify:** `python -c "import model_registry as m; print(m.resolve(None,'classify')['id'])"`
**Done when:** every entry's `regions` list is empirically true (T1.7 asserts it).

### T1.2 — `backend/schemas.py` — new (~90 lines)

**Do:** define `STAGE1_SCHEMA` and `STAGE2_SCHEMA` exactly per design §4.3, with
`"strict": True` and `additionalProperties: False`.

**Also relocate `extract_json()` and `_FENCE_RE` here** from
the prior art's `backend/bedrock_client.py:51-79`. Colocating the schemas and the parser means
`bench/` gets both with one import, and there is exactly one parser in the repo.

**This module is imported by both the Lambda and `bench/driver.py`** — that shared import is what
makes the demo's numbers and the harness's numbers comparable, and it is the whole reason schemas
are not inlined in the handler.

**Verify:** `python3.12 -c "import json,schemas; json.dumps(schemas.STAGE1_SCHEMA); print(schemas.extract_json('\`\`\`json\n{\"a\":1}\n\`\`\`'))"`

### T1.3 — `backend/mantle_client.py` — new (~140 lines)

The primary lane. **Zero pip dependencies:** `botocore.auth.SigV4Auth` + stdlib `urllib` only, both
already in the `python3.12` runtime. Do **not** hand-roll SigV4.

**Do:** implement `chat(model, system_prompt, user_text, max_tokens, temperature, response_format, service_tier=None)`
returning **the same dict contract** as `bedrock_client.converse()` so the handler is transport-agnostic:

```python
{"raw": str, "stopReason": str,
 "usage": {"inputTokens", "outputTokens", "totalTokens",
           "cacheReadInputTokens", "cacheWriteInputTokens"},
 "timings": {"ttftMs", "e2eMs", "bedrockLatencyMs", "overheadMs", "otps"}}
```

Requirements:
- `POST https://bedrock-mantle.{region}.api.aws{path}`, SigV4 signed with service name **`bedrock`**
  (not `bedrock-mantle` — verified).
- **Stream** (`"stream": true`) and parse SSE line by line: iterate `data: ` lines, read
  `choices[0].delta.content` (**not** Converse's `contentBlockDelta.delta.text`), terminate on
  `data: [DONE]`. Stamp `t_first` on the first content delta — that is TTFT, the headline metric.
- **Send `"stream_options": {"include_usage": true}`.** Omit it and the streamed response carries
  **no `usage` object at all** — which silently zeroes input/output tokens *and* `otps` (because
  OTPS is gated on `out_tokens`). This is the single easiest way to ship a demo whose token and
  cost panels read zero.
- **Record a per-delta timeline** `[{tMs, text}, ...]`, not just concatenated text. The prior art's
  loop discards inter-token gaps, and T5.3's "replay from server timeline" **cannot be built
  without them**. Capture this now or redo the client later.
- Map OpenAI usage → the contract: `usage.prompt_tokens` → `inputTokens`,
  `completion_tokens` → `outputTokens`, `usage.prompt_tokens_details.cached_tokens` →
  `cacheReadInputTokens`. Normalise **here**, not in the handler, so the UI needs no lane awareness.
- **`bedrockLatencyMs`: investigate before assuming `None`.** The OpenAI-shape body has no
  equivalent of Converse's `metadata.metrics.latencyMs`. Check the response headers for an
  `x-amzn-*` latency field first. If none exists, `bedrockLatencyMs` and `overheadMs` are `None` on
  this lane — do not fabricate them — but note this blanks **2 of 5** metric tiles on the headline
  model, which weakens the latency story. Resolve it here, not at T5.2.
- Module-scope connection pool and frozen credentials (connection reuse, design §5.1).
- Pass `response_format` straight through when `model["supports_json_schema"]`.
- **`read_timeout` ≤ 25 s** — see T3.5: API Gateway hard-caps at 30 s, so a 90 s read timeout just
  guarantees the gateway 504s first while the Lambda keeps billing.

**Verify:**
```bash
cd backend && python -c "
import model_registry as m, mantle_client as c, prompts, schemas
r = c.chat(m.BY_ID['google.gemma-4-26b-a4b'], prompts.STAGE1_SYSTEM,
           'From: security@paypa1-verify.com\nSubject: Urgent\nhttp://bit.ly/x9',
           200, 0, {'type':'json_schema','json_schema':schemas.STAGE1_SCHEMA})
print(r['raw']); print(r['timings'])"
```
**Done when:** `raw` is **unfenced** schema-exact JSON and `ttftMs` < 1000 warm.

### T1.4 — `backend/converse_client.py` — port (153 → ~150 lines)

**Do:** copy the prior art's `backend/bedrock_client.py` **nearly verbatim** — its
instrumentation (lines 106–152) is the single most valuable thing in the prior art and is already
correct. Preserve exactly: `t0` / `t_first` / `t_end`, `ttftMs`, `e2eMs`, `otps`,
`bedrockLatencyMs` from `event["metadata"]["metrics"]["latencyMs"]`, and `overheadMs = e2e - bedrockLatencyMs`.
That last field is what attributes latency between the model and everything else — keep it.

Also keep `extract_json()` (lines 51–79) verbatim: the fence-stripper with an
outermost-brace-pair fallback. **It is still load-bearing** — this lane has no `json_schema`, and
Gemma 3 fences its output.

Changes:

| Change | Why |
|---|---|
| `temperature` default `0.1` → `0` | design §4.5 |
| `max_tokens` default `500` → caller-supplied (200 / 600) | design §4.5 |
| Delete the `prompt_caching` branch (lines 92–93) | `cachePoint` → `AccessDeniedException` on Gemma. Deleting beats gating. |
| Delete the `latency_optimized` branch (lines 103–104) | `ValidationException` for all candidates |
| Keep `_CONFIG` retries (6 attempts, connect 5 s, tcp_keepalive) but cut `read_timeout` 90 s → **25 s** | doc §5.4, and T3.5's 30 s gateway ceiling makes 90 s meaningless |
| Move `extract_json` + `_FENCE_RE` out to `schemas.py` | One parser in the repo, importable by `bench/` (T1.2) |
| Fix `overheadMs`: `if bedrock_latency_ms else None` → `is not None` | A legitimate `0` currently reads as "absent" |
| Add the per-delta timeline (as T1.3) | T5.3 needs inter-token gaps on **both** lanes |
| Rename file `bedrock_client.py` → `converse_client.py` | Names the lane, now that there are two |

**Verify:** same probe as T1.3 against `google.gemma-3-27b-it`; expect possibly-fenced JSON that
`extract_json` recovers.

### T1.5 — `backend/prompts.py` — rewrite (68 → ~110 lines)

**Do:** replace the prior-art prompts with design §4.2's Stage 1 and Stage 2 system prompts verbatim.

The prior art's prompts are ~120 tokens and have **no injection guard at all** — this is a rewrite,
not a tweak. Also **drop `GENERATE_SYSTEM` and `SAMPLE_KINDS`** (prior art `prompts.py:45-68`): the
static 8-sample corpus replaces AI sample generation, which deletes a route, a prompt, and a
nondeterministic failure mode from demo day.

**Both prompts must end with the prompt-injection guard.** This is a threat-scanning product —
scanned content is adversarial by definition. The email is passed **only** as a `user` message and
**never** spliced into the system block.

> **Security bug to fix while porting:** prior art `handler.py:70` lets the client override the
> system prompt via `body["systemPrompt"]`. **Remove that override entirely** — otherwise the
> injection guard is client-deletable, which defeats the whole point of having one.

Drop the inline JSON-shape-in-prose blocks on the Gemma 4 path (`response_format` enforces shape),
but **keep an equivalent instruction for the Gemma 3 lane**, which has no schema enforcement. One
prompt body plus a lane-conditional suffix keeps the two lanes comparable.

**Verify:** `python3.12 -c "import prompts; assert 'untrusted data' in prompts.STAGE1_SYSTEM"`
**Done when:** Stage 1 is ~800 tokens (matches doc §2.4's cacheable-prefix claim) and sample 6
(prompt-injection) does not subvert the verdict.

### T1.6 — `backend/handler.py` — port + rework (241 → ~260 lines)

**Do:** port the prior art's route dispatch and rework routing + the escalation gate.

Keep: dispatch on `event["rawPath"]`, the stage-1-into-stage-2 threading
(`f"Stage 1 classification: {json.dumps(prior)}\n\nAnalyze this email in detail:\n{user_text}"`),
and the per-stage `maxTokens` defaults.

Add / change:

| Change | Detail |
|---|---|
| **`/api` prefix strip** | CloudFront forwards `/api/*` verbatim; the house API Gateway uses a `$default` catch-all. Normalise: `path = event["rawPath"].removeprefix("/api")`. Missing this is a silent 404 on every call. |
| **Transport dispatch** | `mantle_client` when `model["endpoint"] == "mantle"`, else `converse_client`. The handler must not know anything else about the difference. |
| **Server-side escalation gate** | Implement design §4.4 `should_escalate()` exactly, including the URL and attachment regex clauses that fire *even when the model says benign*. Return the reason string to the UI. |
| **Replace `x-api-key` auth — and make it fail closed** | Prior art used a shared secret (`handler.py:221-224`) guarded by `if API_SECRET:`, so an **unset env var silently authorises every request** (`handler.py:20`). Replace with the `x-origin-secret` HMAC check (T3.9) that **500s when the env var is missing**, never allows. The Cognito JWT is verified by the API Gateway authorizer, not here. |
| **`temperature` default `0.1` → `0.0`** | `handler.py:88` defaults to 0.1. Non-deterministic output means repeated runs disagree with each other in front of an audience. |
| **Surface the exception class** | `handler.py:106-111` swallows everything into a 502 with the message text, so a `ValidationException` from a bad `response_format`, an `AccessDeniedException` from a missing mantle IAM action (R1), and a real model error are indistinguishable. Add a machine-readable `errorClass` field — T5.4's greyed-toggle hover text needs the verbatim error. |
| **Drop the `/generate-sample` route and `_CORS`** | Static corpus replaces it (T1.5); `/api/*` is same-origin so CORS is dead code. `ALLOWED_ORIGIN` defaulted to `"*"` — do not carry that forward even unused. |
| **Startup validation** | At cold start, probe the configured `(model, endpoint, path, region)` triple once and fail loudly with a readable message. A bad deploy should not surface as a failed user request. |
| **Routes** | `POST /scan` (both stages + gate), `POST /classify`, `POST /deep-scan`, `GET /models`, `GET /health` |

**Verify:**
```bash
cd backend && python -c "
import handler, json
r = handler.lambda_handler({'rawPath':'/api/scan','headers':{'x-origin-secret':'x'},
  'body':json.dumps({'email':'From: security@paypa1-verify.com\nhttp://bit.ly/x9'})}, None)
print(json.dumps(json.loads(r['body']), indent=2)[:600])"
```
**Done when:** a phishing sample returns `verdict: malicious`, escalates with a reason, and both
stages report timings.

### T1.7 — Local matrix test

**Do:** assert every `(model, region)` pair in the registry.

**Verify:** 2 models × their declared regions all return 200, and `eu-west-1` × Gemma 4 returns
**404** (proving the `regions` list is honest rather than optimistic).

---

## 3. Phase 2 — Benchmark harness (~4 h)

### T2.1 — `bench/driver.py` — port (285 → ~330 lines)

**Do:** port the prior art's `loadtest/driver.py`. Keep the `ThreadPoolExecutor` concurrency
model, the `--ramp` doubling, the percentile computation, and — critically — the
`total_max_attempts=1` retry-disable (`make_client`, lines 109–123) so throttles stay **visible**
instead of being silently absorbed by the SDK.

Changes:

| Change | Why |
|---|---|
| Add the mantle transport | The primary lane must be measurable, not just the fallback |
| `import schemas` from `backend/` | Measure the *same* request the demo makes (design §8.1) |
| **Three-way error taxonomy** | Design §8.3. The prior art stops at "first 429/503" — which **never fires here**. Buckets: service-throttle / client-transport / application. |
| Discard a 5 s warm-up window | Cold TTFT is ~4× warm and would poison p50 |
| Cap `--concurrency` guidance | 40 on mantle, 150 on runtime (design §6.4) |
| Emit `results.json` | Consumed by the UI's Benchmark tab. Prior art emits CSV only, so **there is no existing file to build T5.4's charts against** — this task gates that one. |
| Record escalation rate | The single most valuable number the PoC produces (design §9.3) |
| **Parse the model output** | Prior art `one_request` (`driver.py:126-156`) **never parses the response**, so a run where 100 % of responses were malformed JSON reports as **0 % errors**. Parse via `schemas.extract_json` and bucket failures as `application`. |
| **Add `--region` and `--lane`** | Prior art takes region only from the registry (`driver.py:258`) and has no endpoint axis at all — §8.4's methodology and the eu-central-1 cross-check both need them. |
| **Replace the `(1,2,4,8)` ramp with `--steps`** | Those multipliers overshoot the known ceilings (mantle fails at 50, runtime at 300), so the default run burns tokens generating client-side errors. Cap the mantle lane at **40**. |
| **Unify the percentile algorithm** | `driver.py:163` uses `int(round(...))`; the UI's `RunHistory.tsx:8` uses `Math.ceil(...)`. They **disagree at small n** — exactly the regime the UI runs in. Pick nearest-rank `ceil` and use it in both. |

**Verify:** `python3.12 bench/driver.py --model google.gemma-4-26b-a4b --concurrency 10 --duration 30`
**Done when:** `results.json` has populated percentiles and all three buckets present (two at zero).

> Note on accuracy: the prior art's `wall` includes a post-`stop_at` drain and the top-up loop can
> over-submit up to `concurrency` requests past the deadline, so achieved RPS is slightly
> **understated** on short runs. Fine — but know it before quoting a number from a 20 s phase.

### T2.2 — `bench/report.py` — new (~150 lines)

**Do:** render `results.json` to markdown + the UI's chart JSON.

**Hard rule:** `report.py` **refuses to print a pass/fail verdict** on any run whose
client-transport bucket is non-empty. A saturated load generator must never be reported as a Bedrock
limit.

**Verify:** feed it a run with injected client errors; confirm it emits the red banner and no verdict.

### T2.3 — `bench/corpus/` — 8 samples

**Do:** **6 of the 8 already exist** in the prior art's `frontend/src/samples.ts` (179 lines) and
map straight across: its phishing → #1, BEC → #2, malware attachment → #3, newsletter → #5,
delivery-scam/crypto → #7, `EMPTY_EMAIL` → #8 ("paste your own"). Domains are already `example.*` —
keep that discipline.

**Two must be written new**, and they are the two that matter most:
- **#4 legitimate password reset** — the hardest benign. Looks exactly like a phish and must come
  back benign.
- **#6 prompt-injection body** — proves the T1.5 guard holds. Put the injection in a **header** as
  well as the body: `_format_email` handles a `headers` dict but nothing in the prior art ever
  populated it, so that branch is untested.

Size to doc §2.4 (2–5 KB, ~500–2 000 input tokens). Each is a JSON file with `expected_verdict` and
`should_escalate`.

The one that earns its place most: **#2 BEC wire request** — no URL, no attachment, so it is the
answer to *"isn't this just a URL blocklist?"*

**Verify:** all 8 produce the expected verdict and escalation decision.

---

## 4. Phase 3 — Terraform (~6 h)

### 4.1 What I create — file manifest

Copied from an internal prior-art `terraform/` stack (7 files, 453 lines HCL, 29 blocks), then edited.
**Copy file-by-file — never `cp -r`** (§0.4).

| File | Lines | Origin | What changes |
|---|---:|---|---|
| `infra/main.tf` | ~30 | copy | Add `archive` + `random` to `required_providers`; keep both providers |
| `infra/variables.tf` | ~60 | copy + extend | Add `type` to all 6 (house has none), add validation, add 4 new vars |
| `infra/s3.tf` | ~20 | copy, **cut half** | **Delete** the `images` bucket, its PAB, and its CORS config |
| `infra/cloudfront.tf` | ~150 | copy | `compress=false` on `/api/*`; **delete both `custom_error_response`**; add `custom_header`; add `origin_read_timeout` |
| `infra/lambda.tf` | ~90 | copy, **split** | IAM + packaging + function only. API Gateway **moves out** → `apigateway.tf` |
| **`infra/apigateway.tf`** | ~70 | **new** | API GW resources moved from `lambda.tf:79-117`, **plus the JWT authorizer** |
| `infra/cognito.tf` | ~60 | copy | Add `triggers`; add `count` for the auth-fallback toggle |
| **`infra/cloudwatch.tf`** | ~15 | **new** | Explicit Lambda log group with retention |
| **`infra/cloudfront_function.tf`** | ~50 | **new** | Basic-auth fallback (design §7.4), `count`-gated, default off |
| `infra/outputs.tf` | ~40 | copy + extend | Drop `images_bucket`; add `cognito_issuer` (the JWT authorizer needs it) |
| `infra/terraform.tfvars` | 8 | **new** | **Must set `project`** (§0.4) |
| `deploy.sh` | ~60 | copy + harden | Repo root, not `infra/` |
| `smoke.sh` | ~50 | **new** | Pre-demo warm-up gate |

> **Correction to design §11.2:** it lists an `apigateway.tf` edit, but **that file does not exist**
> in the house stack — all four `aws_apigatewayv2_*` resources plus `aws_lambda_permission` live in
> `lambda.tf:79-117`. T3.5 **creates** it by moving them, which makes the design's reference true.

### 4.2 What AWS resources get launched

**24 Terraform-managed resources + 1 implicit log group**, all in us-east-1 except the ACM cert's
control plane (also us-east-1 — required for CloudFront). Every predicted name verified free (T0.1).

| # | Service | Terraform resource | Exact name / identifier |
|---:|---|---|---|
| 1 | S3 | `aws_s3_bucket.frontend` | `email-scan-frontend-123456789012` |
| 2 | S3 | `aws_s3_bucket_public_access_block.frontend` | all 4 blocks `true` |
| 3 | S3 | `aws_s3_bucket_policy.frontend` | Sid `AllowCloudFrontOAC` |
| 4 | Cognito | `aws_cognito_user_pool.main` | name `email-scan` → id `us-east-1_XXXXXXXXX` |
| 5 | Cognito | `aws_cognito_user_pool_client.main` | `email-scan-client` → 26-char id |
| 6 | Cognito | `null_resource.cognito_user` | user `demo`, pw `<demo password>`, permanent |
| 7 | IAM | `aws_iam_role.lambda` | `email-scan-lambda` |
| 8 | IAM | `aws_iam_role_policy.lambda` | `email-scan-lambda-policy` (inline) |
| 9 | IAM | `aws_iam_role_policy_attachment.lambda_basic` | `AWSLambdaBasicExecutionRole` |
| 10 | Lambda | `aws_lambda_function.api` | `email-scan`, python3.12, 512 MB, 120 s |
| 11 | Logs | `aws_cloudwatch_log_group.lambda` **(new)** | `/aws/lambda/email-scan`, **retention 14 d** |
| 12 | API GW | `aws_apigatewayv2_api.api` | `email-scan-api` → 10-char id |
| 13 | API GW | `aws_apigatewayv2_integration.lambda` | `AWS_PROXY`, `timeout_milliseconds = 30000` |
| 14 | API GW | `aws_apigatewayv2_authorizer.jwt` **(new)** | `email-scan-jwt`, JWT |
| 15 | API GW | `aws_apigatewayv2_route.default` | `$default`, **`authorization_type = "JWT"`** |
| 16 | API GW | `aws_apigatewayv2_stage.default` | `$default`, `auto_deploy = true` |
| 17 | Lambda | `aws_lambda_permission.apigw` | Sid `AllowAPIGateway` |
| 18 | ACM | `aws_acm_certificate.main` | `email-scan.example.com`, DNS-validated, **us-east-1** |
| 19 | Route53 | `aws_route53_record.cert_validation` | `_<32hex>.email-scan.example.com` CNAME, TTL 60 |
| 20 | ACM | `aws_acm_certificate_validation.main` | validation gate |
| 21 | CloudFront | `aws_cloudfront_origin_access_control.frontend` | `email-scan-oac` → `E…` |
| 22 | CloudFront | `aws_cloudfront_distribution.main` | alias `email-scan.example.com` → `d<13>.cloudfront.net` |
| 23 | Route53 | `aws_route53_record.main` | A-ALIAS → distribution, zone `Z2FDTNDATAQYW2` |
| 24 | random | `random_password.origin_secret` **(new)** | 48 chars, no specials |
| 25 | CloudFront | `aws_cloudfront_function.basic_auth` **(new, `count=0`)** | `email-scan-basic-auth` — **off by default** |

**Explicitly NOT created** (deleted from the copied stack): `aws_s3_bucket.images`,
its `public_access_block`, and `aws_s3_bucket_cors_configuration.images` — plus the `s3:*` statement
in the IAM policy that references them. *Design §11.2 omits these; they are dead weight for a PoC
that streams everything through `/api/*`.*

**Quota headroom** — no constraint anywhere: 2/500 CloudFront distributions, 5/2500 ACM certs,
7/1000 Cognito pools, 1000/1000 unreserved Lambda concurrency.

### T3.1 — Copy the Terraform, file by file

```bash
cd "$(git rev-parse --show-toplevel)"
S=<path to the prior-art terraform directory>
for f in main.tf variables.tf s3.tf cloudfront.tf lambda.tf cognito.tf outputs.tf; do
  cp "$S/$f" infra/$f
done
cp "$S/../deploy.sh" ./deploy.sh
```

**Verify — this is the safety gate, do not skip:**
```bash
ls infra/ | grep -i tfstate     # MUST be empty
test ! -e infra/terraform.tfstate && echo "SAFE: no state copied"
```
**Done when:** 7 `.tf` files present and **zero** state files.

### T3.2 — `infra/terraform.tfvars`

```hcl
region                = "us-east-1"
project               = "email-scan"          # MANDATORY — see §0.4. Never leave it at the default.
domain_name           = "email-scan.example.com"
hosted_zone_name      = "example.com"
cognito_demo_username = "demo"
cognito_demo_password = "<demo password>"     # set locally; terraform.tfvars is gitignored
basic_auth_enabled    = false                 # design §7.4 fallback, off unless Cognito misbehaves
```

**Verify:** `grep -c 'project.*email-scan' infra/terraform.tfvars` → `1`

### T3.3 — `infra/variables.tf`

**Do:** add `type` to all 6 house variables (none has one), add the 4 new vars
(`basic_auth_enabled/username/password`, `log_retention_days`), and add a **validation block on
`domain_name`** that rejects anything not ending `.example.com` and rejects the pre-existing
stacks' hostnames outright — a cheap guard against the §0.4 UPSERT hazard.

### T3.4 — `infra/s3.tf` — cut the images bucket

**Do:** delete `aws_s3_bucket.images`, `aws_s3_bucket_public_access_block.images`,
`aws_s3_bucket_cors_configuration.images`. Keep `force_destroy = true` on `frontend` (a PoC should
tear down cleanly).

### T3.5 — `infra/apigateway.tf` — new, with the JWT authorizer

**Do:** move `aws_apigatewayv2_api`, `_integration`, `_route`, `_stage`, and
`aws_lambda_permission.apigw` out of `lambda.tf:79-117` into a new `apigateway.tf`. Then add the
authorizer the house stack lacks and wire the route to it:

```hcl
resource "aws_apigatewayv2_authorizer" "jwt" {
  api_id           = aws_apigatewayv2_api.api.id
  authorizer_type  = "JWT"
  name             = "${var.project}-jwt"
  identity_sources = ["$request.header.Authorization"]
  jwt_configuration {
    audience = [aws_cognito_user_pool_client.main.id]
    issuer   = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.main.id}"
  }
}

resource "aws_apigatewayv2_route" "default" {
  api_id             = aws_apigatewayv2_api.api.id
  route_key          = "$default"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorization_type = "JWT"                              # house stack: absent -> login was cosmetic
  authorizer_id      = aws_apigatewayv2_authorizer.jwt.id
}

resource "aws_apigatewayv2_integration" "lambda" {
  api_id                 = aws_apigatewayv2_api.api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 30000    # HARD API Gateway ceiling — make it visible in code
}
```

> **The 30 s ceiling is a real design constraint.** The Lambda is configured `timeout = 120`, so a
> two-stage scan exceeding 30 s returns **504 at the gateway while the Lambda keeps billing**.
> Measured Stage 1 + Stage 2 is ~2.5–3.3 s, so there is room — but a cold start (~1.6 s) stacked on
> both stages plus one slow retry eats it. `GET /health` must stay trivially fast.
>
> **Align the whole timeout chain** so the innermost limit fires first and yields a readable error
> instead of a bare gateway 504:
> `read_timeout 25 s` (T1.3/T1.4) < `Lambda timeout 29 s` (T3.6) < `API Gateway 30 s` <
> `CloudFront origin_read_timeout 120 s` (T3.8). The prior art's 90 s read timeout inverts this.

**Also:** drop the copied `cors_configuration` from `aws_apigatewayv2_api` — `/api/*` is same-origin
behind CloudFront, so it is dead weight (design §7.1).

### T3.6 — `infra/lambda.tf` — IAM and packaging

**Do:** replace the house IAM policy's bedrock statement. The house version
(`lambda.tf:18-47`) grants **only** `bedrock:InvokeModel` — the fallback `ConverseStream` lane 403s,
and there is nothing at all for mantle.

```hcl
{
  Effect = "Allow", Resource = "*"          # Gemma 4 has NO foundation-model ARN to scope to
  Action = ["bedrock-mantle:CallWithBearerToken", "bedrock-mantle:Get*",
            "bedrock-mantle:List*", "bedrock-mantle:CreateInference"]
},
{
  Effect = "Allow"
  Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream",
            "bedrock:Converse", "bedrock:ConverseStream"]
  Resource = ["arn:aws:bedrock:*::foundation-model/*",
              "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*"]
}
```

Also: **delete the `s3:*` statement** (it references the now-deleted images bucket, so `apply` fails
otherwise); retarget `data.archive_file.lambda` `source_dir` to `"${path.module}/../backend"` and
`output_path` to `"${path.module}/../backend.zip"`; and replace the `environment.variables` block
(drop `IMAGES_BUCKET`, add `PRIMARY_MODEL_ID`, `FALLBACK_MODEL_ID`, `ORIGIN_SECRET`).

**Narrow the policy** once T0.3 reports the minimal action set.

### T3.7 — Guard the packaging hash

`data.archive_file` zips `source_dir` **verbatim, including `__pycache__`**. Once T1.x has run
`python -c` locally, `.pyc` files exist and land in the package — changing `output_base64sha256` on
every run and forcing a spurious Lambda update on every `apply`.

**Do:** add `excludes = ["__pycache__", "*.pyc"]` to the `archive_file` block, and have `deploy.sh`
run `find backend -name __pycache__ -type d -exec rm -rf {} +` first. Belt and braces.

**Verify:** `terraform plan` twice with no code change → second plan reports **no changes**.

### T3.8 — `infra/cloudfront.tf`

Three edits. **Keep the legacy `forwarded_values` blocks.**

> **Correction to design §7.1/§11.2:** the design implies managed cache policies
> (`CachingDisabled`, `AllViewerExceptHostHeader`). The **stack this was copied from does not use
> them** — it uses legacy `forwarded_values` with inline TTLs and references **zero** policy IDs.
> Those IDs came from a different prior-art stack. Mixing `forwarded_values` and `cache_policy_id`
> in one behaviour is a
> **hard Terraform error**. Do not migrate; there is no benefit here, and the existing
> `/api/*` behaviour already forwards `Authorization`, so the new JWT authorizer works unchanged.

1. **`compress = false`** on the `/api/*` `ordered_cache_behavior` (house sets `true` on both).
   Harmless while responses are buffered JSON; breaks the moment SSE lands.
2. **Delete both `custom_error_response` blocks** (403→200 and 404→200, `cloudfront.tf:105-116`).
   They are **distribution-wide**, so a genuine 403/404 from the API origin — including a 401 from
   our new authorizer — gets rewritten into `index.html` **with HTTP 200**. This is a maddening
   failure mode to debug live: the app loads, looks fine, and every API call silently returns HTML.
3. **Add to the `api-gateway` origin:** `custom_header { name = "x-origin-secret" value = random_password.origin_secret.result }`
   plus `origin_read_timeout = 120` and `origin_keepalive_timeout = 5`.

### T3.9 — `x-origin-secret` end to end

**Do:** `random_password.origin_secret` (48 chars, `special = false` — keep it a clean HTTP header
token) → CloudFront origin `custom_header` → Lambda env var → validated in `handler.py` with
`hmac.compare_digest`.

Harden the ported check — API Gateway payload format 2.0 **lowercases every header name**, so the
prior art's `.title()` fallback is dead code, and `compare_digest` raises `TypeError` on non-ASCII
`str`, so compare **bytes**:

```python
def _origin_allowed(event):
    presented = (event.get("headers") or {}).get("x-origin-secret")
    if not presented:
        return False
    return hmac.compare_digest(presented.encode("utf-8", "surrogateescape"),
                               ORIGIN_SECRET.encode("utf-8"))
```

**Verify:** `curl` the API Gateway URL directly → **403**; through CloudFront with a valid JWT → 200.

### T3.10 — `infra/cognito.tf`

**Do:** add the `triggers` block the house stack lacks, and gate on the auth fallback:

```hcl
resource "null_resource" "cognito_user" {
  count      = var.basic_auth_enabled ? 0 : 1
  depends_on = [aws_cognito_user_pool.main, aws_cognito_user_pool_client.main]
  triggers = {                                   # without this, password changes silently no-op
    pw   = var.cognito_demo_password
    user = var.cognito_demo_username
    pool = aws_cognito_user_pool.main.id
  }
  provisioner "local-exec" { command = <<-EOT
      ... house command, unchanged ...
  EOT }
}
```

**Two traps to know:** the provisioner shells out to the AWS CLI at apply time and does **not**
inherit the provider's credentials — so `apply` must run with `AWS_PROFILE=aws` exported and a live
SSO session. It also ends `2>/dev/null || true`, which **hides a genuine failure**; after apply,
always confirm the user independently:

```bash
aws cognito-idp admin-get-user --user-pool-id <pool> --username demo \
  --query '{S:UserStatus,E:Enabled}'          # -> CONFIRMED / true
```

**Cost note:** the pool is created at tier `ESSENTIALS` (a **billed** tier — it is the inherited
house default). Consider `user_pool_tier = "LITE"` if PoC cost matters, but confirm
the pinned `aws ~> 5.0` provider supports the argument first.

### T3.11 — `infra/cloudwatch.tf` — new

**Do:** add an explicit `aws_cloudwatch_log_group` for `/aws/lambda/email-scan` with
`retention_in_days = 14`.

The house pattern leaves this **implicit and unmanaged** — in the prior art the Lambda log group
has retention `null` = **never expire**, survives `terraform destroy`, and accrues cost forever. A
benchmark run at high concurrency writes a lot of log lines.

### T3.12 — `infra/cloudfront_function.tf` — new, `count = 0`

**Do:** write the documented auth fallback (design §7.4) as a `count`-gated
`aws_cloudfront_function` + `dynamic "function_association"` on **both** behaviours, ported from a
prior-art stack's edge-auth implementation. Runtime
`cloudfront-js-2.0`; credential base64-baked into the body (CloudFront Functions get no env vars and
no network access — this is obfuscation, not encryption).

Default **off**. Its value is that flipping one variable is a 5-minute recovery on demo day if the
Cognito provisioner misbehaves.

### T3.13 — `terraform plan` — the review gate

```bash
cd infra && terraform init && terraform plan -out=tfplan
terraform show -json tfplan | python3 -c "
import json,sys
p=json.load(sys.stdin)
for c in p.get('resource_changes',[]):
    acts=c['change']['actions']
    if acts!=['no-op']: print(acts, c['address'], '->', c['change']['after'].get('name') or c['change']['after'].get('bucket') or '')
"
```

**Done when — all four must hold:**
1. Plan is **create-only**: 24–25 to add, **0 to change, 0 to destroy**.
2. **Zero** resources belonging to any pre-existing stack appear anywhere.
3. The `aws_route53_record.main` name is `email-scan.example.com` — and nothing else.
4. Exactly one `aws_route53_record` for the app plus one for cert validation.

---

## 5. Phase 4 — Deploy (~2 h)

### T4.1 — `deploy.sh` — harden the copied script

The house script (41 lines, repo root) is `set -e` only, does not export `AWS_PROFILE`, and runs
`terraform apply -auto-approve` with **no plan review**.

**Do:** add `set -euo pipefail`; keep `AWS_PROFILE` as an assertion rather than a silent default;
add `terraform validate`; replace `-auto-approve` with **apply of the reviewed `tfplan`**; add the
`__pycache__` purge (T3.7); keep the Cognito-ID `sed` injection and the CloudFront invalidation.

**Invalidation hazard:** the house script invalidates `/*` using
`terraform output -raw cloudfront_distribution_id`. Run from the wrong directory or with a stale
output and it invalidates a **different, pre-existing** distribution. Harmless but confusing —
assert the returned ID is the one this stack just created.

### T4.2 — Apply

```bash
./deploy.sh                             # from the repo root
```

**This is a 10–20 minute operation**, not seconds: ACM DNS validation takes 2–5 min and the
CloudFront distribution takes 5–15 min to reach `Deployed`.

**Verify:**
```bash
aws acm describe-certificate --region us-east-1 --certificate-arn "$(cd infra && terraform output -raw acm_certificate_arn)" \
  --query 'Certificate.Status'                                    # -> ISSUED
aws cloudfront get-distribution --id "$(cd infra && terraform output -raw cloudfront_distribution_id)" \
  --query 'Distribution.Status'                                   # -> Deployed
```

### T4.3 — Verify the deployment

```bash
curl -sI https://email-scan.example.com | head -1                 # -> HTTP/2 200
curl -s  https://email-scan.example.com/api/health                # -> 401 (authorizer working)
curl -sI "$(cd infra && terraform output -raw api_gateway_url)/health" | head -1   # -> 403 (origin secret working)
```

**Do not run T4.3 before T4.2's two status checks pass** — a `curl` against a not-yet-`Deployed`
distribution fails with a TLS or 403 error that looks like a real bug.

**Done when:** the SPA loads over TLS, unauthenticated API calls are rejected, and direct
API-Gateway access is rejected.

### T4.4 — Confirm the demo login

**Do:** log in at the URL with `demo` / `<the cognito_demo_password from terraform.tfvars>`, then run one scan.
**Verify:** independently confirm the Cognito user (T3.10) — the provisioner swallows errors.

---

## 6. Phase 5 — Frontend (~5 h)

### T5.1 — `frontend/index.html` — Scan tab

**Do:** single no-build HTML file (no npm, no Vite, no bundler), laid out per design §7.3. Cognito
login via `InitiateAuth` `USER_PASSWORD_AUTH` posted directly to
`https://cognito-idp.us-east-1.amazonaws.com/`, IdToken in `sessionStorage`, sent as
`Authorization: Bearer` on every `/api/*` call. API base is the relative string `'/api'` — same
origin, so **no CORS**.

Runtime config (`window.COGNITO_CLIENT_ID`, `window.COGNITO_POOL_ID`) arrives via `deploy.sh`'s `sed`
on the literal `</head>`. **Keep exactly one `</head>` in the file** or the injection misbehaves silently.

### T5.2 — Latency strip and result panel

**Do:** transliterate `LatencyStrip.tsx` (88 lines) — the highest-value UI file in the prior art.
Five metric tiles: TTFT (pass ≤ 1 000 ms), E2E (pass ≤ 3 000 ms Stage 1 / 10 000 ms Stage 2),
Bedrock server latency, Overhead, Output rate — plus the token/cache/cold-start row.

**Port the hover hints verbatim.** They are the artifact that makes the numbers defensible in the
room, e.g. *"E2E minus Bedrock's own latency: network, TLS, SDK, Lambda. If this is large, the model
isn't the problem."* Rewriting them from scratch produces worse copy.

Also transliterate `RunHistory.tsx` (session percentiles — turns the Scan tab into a mini-benchmark
you can quote) and `ResultPanel.tsx`'s evidence sections: verdict chip row, **parse-error banner**
(still live for the Gemma 3 lane), URL table, attachment table, and especially the
**show-prompt / show-raw disclosures** — *"here is the exact prompt we sent and the exact bytes that
came back"* is the most convincing element in the existing UI.

`styles.css` (171 lines) ports **as-is** into a `<style>` block — no framework, no preprocessor, and
it already carries the `--pass`/`--fail`/`--benign`/`--susp`/`--mal` tokens.

> **Field-name trap that renders nothing:** `ResultPanel.tsx:58,64-66` renders `r.reason` and
> `r.categories` — Stage 1 fields that the new `STAGE1_SCHEMA` **does not emit** (it emits
> `signals[]` of `{signal, detail}`). Transliterating without remapping gives a panel that silently
> shows an empty Stage 1. Remap while porting.

**If `bedrockLatencyMs` is `None` on the primary lane** (resolved at T1.3), render tiles 3–4 as
`n/a on this lane` — not `—` and never `0`. A missing field must not read as a fast one.

Also port `api.ts:24-31`'s non-JSON error branch (`res.text()` → try-parse → throw with a text
excerpt). Eight lines that pay for themselves the first time CloudFront returns the HTML shell
instead of JSON.

### T5.3 — Escalation and streaming replay

**Do:** render the escalation arrow **with the server's reason string** on it (design §4.4), so the
gate is visible rather than magic. Replay the token timeline with real recorded inter-token gaps,
badged **"replayed from server timeline"** — honest labelling is what makes the authoritative,
server-measured TTFT number credible.

### T5.4 — Benchmark and Migration tabs

> **Superseded 2026-09-13.** The Migration tab described below was removed. The Benchmark tab
> remains and gained four built-in sample runs plus a downloadable `results.sample.json`
> template. Kept as the historical record of the build plan.

**Do:** Benchmark tab charts `results.json` (percentile bars, RPS vs concurrency, three-way error
taxonomy, red banner on client-transport errors). Migration tab renders the incumbent-API → Bedrock
request diff and the live cost panel (design §9).

Greyed-out toggles (prompt caching, `latency=optimized`) must show the **verbatim
`ValidationException`/`AccessDeniedException` text** on hover. Showing the real error is more
persuasive than a claim.

The region dropdown offers **us-east-1 and eu-central-1 only** — verified: `eu-west-1` returns
**404** for Gemma 4, so offering it would be a live-demo failure.

### T5.5 — *(conditional)* Real SSE

**Only if T0.2 returned YES.** Swap the replay for real streaming. Requires `compress = false`
(already done, T3.8). If T0.2 said NO, skip — nothing is lost.

---

## 7. Phase 6 — Measure and document (~3 h)

### T6.1 — `smoke.sh`

**Do:** the 4-step pre-demo gate from design §7.6: cold-start both lanes, **assert Stage 1
TTFT < 1 s**, assert both models answer in both regions, assert the escalation gate fires on the
phishing sample and not on the benign one. Exit non-zero on any failure.

Cold TTFT is ~1.6 s vs ~0.4 s warm — a **4× effect**. If the first user-visible scan is cold it
reads as *"Bedrock is slow."* This script exists to make that impossible.

### T6.2 — Measurement runs

**Do:** ramp both lanes to their ceilings (mantle 25→40→50, runtime 100→150→300), 3 runs each,
warm-up discarded. Capture the **escalation rate** across the corpus.

**Verify:** cross-check the *fallback* lane's client-measured TTFT against CloudWatch
`AWS/Bedrock TimeToFirstToken` for `ModelId=google.gemma-3-27b-it`.

> **This cross-check is impossible on the primary lane.** Verified: `AWS/Bedrock` emits **0 metrics**
> for `ModelId=google.gemma-4-26b-a4b` vs **14** for `google.gemma-3-27b-it`. The design's promise of
> a CloudWatch table must be **scoped to the fallback lane only** — and the gap raised as production
> risk R3.

### T6.3 — `docs/results.md`

**Do:** fill design §2's traceability table with measured numbers, record both spike answers (T0.2,
T0.3), and state plainly that sustained RPS is **extrapolated, not measured**, unless T7.1 ran.

---

## 8. Phase 7 — Gated throughput run (~2 h, needs sign-off)

### T7.1 — Distributed 700 RPS attempt

**Blocked pending explicit approval — est. ~$400 in tokens and Fargate.**

**Do:** containerise `bench/driver.py`, run ~5 four-vCPU Fargate tasks (~18 workers × 40 concurrent
≈ 700 in-flight), execute the doc §5.3 ramp holding each step 15 min.

Rationale (design §8.4): server-side latency is **load-independent** — measured p50 2 289 → 2 251 ms
and p95 3 249 → 3 234 ms as offered load rose 50 % — which is the licence to scale horizontally.
Every failure observed so far was **client-side** transport exhaustion; `InvocationThrottles` stayed
at **zero** throughout.

**Done when:** sustained RPS is reported as *measured*, with the three-way taxonomy split out.

---

## 9. Corrections to `design.md` found during inventory

These are defects in the design doc that this task list supersedes. I will patch `design.md` to match.

| # | design.md says | Reality | Where handled |
|---|---|---|---|
| 1 | §11.2 edits `apigateway.tf` | **No such file.** API GW lives in `lambda.tf:79-117` | T3.5 creates it by moving them |
| 2 | §7.1/§11.2 imply managed cache policies (`CachingDisabled`) | the prior art uses **legacy `forwarded_values`**, zero policy IDs. Mixing the two is a hard TF error | T3.8 keeps legacy |
| 3 | §11.2 edit list is complete | Omits the `images` bucket, its PAB, its CORS config, **and** the IAM `s3:*` statement that references it — `apply` fails if the statement is left | T3.4, T3.6 |
| 4 | §11.2 treats `project` as one tfvars line among many | It is **not set** in the house tfvars; the source stack runs on the variable's default. Omitting it collides with that stack | §0.4, T3.2 |
| 5 | §12.1 pins `--version-id v6` | Current `DefaultVersionId` is **v9** (2026-08-04). The `BedrockMantleAPIs` text is unchanged, so the conclusion stands | doc fix only |
| 6 | Region flip described as us-east-1 ⇄ eu-central-1 | Correct — and `eu-west-1` must be **excluded**: Gemma 4 returns **404** there | T1.1, T5.4 |
| 7 | §8.5 promises a CloudWatch cross-check | Only possible on the **fallback** lane (0 vs 14 metrics) | T6.2 |
| 8 | Silent on the API Gateway 30 s ceiling vs Lambda `timeout = 120` | A >30 s two-stage scan returns 504 while the Lambda keeps billing; the prior art's 90 s `read_timeout` inverts the chain | T3.5, T1.3/T1.4 |
| 9 | §7.2 specifies "replay from server timeline" | Nothing in the prior art records inter-token gaps — the timeline must be **captured in the client** or the replay cannot be built | T1.3 |
| 10 | §5.3 cites the 42 % cache-hit figure from `cached_tokens` | On the OpenAI-compatible streaming route, `usage` is **absent entirely** unless `stream_options.include_usage` is sent — so the counter, token totals, **and** OTPS silently read zero | T1.3 |
| 11 | §7.3 shows a 5-tile latency strip | `bedrockLatencyMs` has no OpenAI-shape equivalent, so **2 of 5 tiles may be blank on the headline model** unless an `x-amzn-*` header supplies it | T1.3, T5.2 |
| 12 | Treats the prior-art prompts as a starting point to edit | `handler.py:70` lets the **client override the system prompt**, which makes the injection guard client-deletable. Must be removed, not just rewritten around | T1.5 |
| 13 | §8.3 describes a three-way taxonomy as an extension | The prior art also **never parses model output**, so a run of 100 % malformed JSON reports 0 % errors | T2.1 |
| 14 | §7.3 renders Stage 1 `signals[]` | `ResultPanel.tsx` renders `categories`/`reason`, which the new schema does not emit — silent empty panel | T5.2 |

---

## 10. Teardown

```bash
cd infra                                # from the repo root
terraform destroy                       # review the plan: must destroy ONLY email-scan-* resources
```

Not covered by `destroy`, so handle manually:
- The Cognito `demo` user dies with the pool — no action.
- `/aws/lambda/email-scan` **is** managed (T3.11), so it goes. *This is the reason T3.11 exists.*
- `backend.zip` and `frontend/index_deploy.html` are local build artifacts — `git clean`.
- ACM cert and Route53 records are managed and will be destroyed.

**Standing cost if left running:** ~$1–3/month idle (Cognito ESSENTIALS tier + Route53 queries +
CloudFront/S3 at near-zero traffic). Bedrock is per-token, so idle cost is zero.

---

## 11. Summary

| Phase | Tasks | Effort | Blocked by |
|---|---|---:|---|
| 0 Preflight and spikes | T0.1–T0.4 | 2 h | — |
| 1 Application code | T1.1–T1.7 | 8 h | — (no AWS dependency) |
| 2 Benchmark harness | T2.1–T2.3 | 4 h | T1.2 (shared schemas) |
| 3 Terraform | T3.1–T3.13 | 6 h | T0.3 for the minimal IAM policy |
| 4 Deploy | T4.1–T4.4 | 2 h | Phase 3 |
| 5 Frontend | T5.1–T5.5 | 5 h | T4.3; T5.5 needs T0.2 = YES |
| 6 Measure and document | T6.1–T6.3 | 3 h | Phase 5 |
| 7 Throughput (gated) | T7.1 | 2 h | **Sign-off, ~$400** |

**Total: ~30 h**, of which 28 h is unblocked once the two Phase-0 spikes land. Phases 1 and 2 need
no AWS resources and can start immediately.

### What is reused vs written new

The prior art contributes **2 012 lines across 20 files**. Roughly:

| | Source | Fate |
|---|---|---|
| **Reused near-verbatim** | `bedrock_client.py` instrumentation (L106–153), `extract_json` (L51–79), `LatencyStrip.tsx`, `RunHistory.tsx`, `styles.css`, `driver.py` concurrency skeleton, 6 of 9 samples | ~550 lines |
| **Reused with edits** | `model_registry.py` shape, `handler.py` helpers, `ResultPanel.tsx` evidence sections, `api.ts` error branch | ~350 lines |
| **Written new** | `mantle_client.py`, `schemas.py`, escalation gate, `/scan` route, 3-tab `index.html`, `report.py`, 2 samples, `smoke.sh` | ~900 lines |
| **Dropped** | Vite/React/TS scaffolding, `types.ts`, AI sample generator (route + prompt + UI), `requirements.txt`, all `cachePoint`/`performanceConfig` code, `plan.md`, `README.md` | ~600 lines |

Infrastructure contributes **453 lines of HCL across 7 files**, of which ~380 port unchanged; ~135
new lines cover the JWT authorizer, the log group, the CloudFront Function fallback, and the
`x-origin-secret` wiring.
