# email-scan

A proof-of-concept that classifies email for phishing and social-engineering threats using
Amazon Bedrock, in a two-stage pipeline: a fast triage pass on every message, then a deeper
analysis only on the ones a server-side gate escalates.

> **Not an official AWS project.** This is a personal proof-of-concept. It is not an AWS
> product, position, endorsement or recommendation, and nothing here is a commitment of any
> kind. Latency figures, throughput ceilings, quota values and pricing are point-in-time
> measurements taken in one AWS account in **September 2026** — treat them as a snapshot, not
> a specification. Provided as-is, with no warranty and no support.

## What is here

| Path | What it is |
|---|---|
| `backend/` | The Lambda: two model lanes, the escalation gate, DynamoDB run history |
| `frontend/index.html` | Single-file SPA, no build step and no dependencies |
| `infra/` | Terraform: CloudFront, S3, API Gateway, Lambda, Cognito, DynamoDB, ACM, Route53 |
| `bench/` | Load harness that calls Bedrock directly, plus an 8-sample corpus |
| `docs/design.md` | The design, and what measurement contradicted in the source assumptions |
| `docs/tasks.md` | The build plan: every AWS resource created, every Terraform edit |
| `docs/results.md` | What was actually measured, and where reality differed from the design |
| `docs/arch-diagram/` | Seven architecture diagrams, generated from Python so they stay in sync |

## The interesting parts

The measurements are the point, not the code. In particular:

- **Two Bedrock transports.** One model is reachable only on an OpenAI-compatible route; the
  other only via `Converse`. Model, endpoint and path are mutually exclusive, and a single
  wrong path segment returns a 400 that reads like a model-availability problem. `docs/design.md` §3.2
  has the full matrix.
- **Three of the four latency levers the source guidance recommended turned out to be
  unavailable or ineffective** for these models, and the targets were met without any of them.
  `docs/design.md` §5.
- **The throughput limit is requests-per-minute, not tokens-per-minute**, and the two are not
  interchangeable. `docs/design.md` §6.
- **A two-stage pipeline is a throughput strategy, not only a latency one** — at a ~15%
  escalation rate the expensive stage needs a fraction of the quota headroom. §6.2.
- **What the smoke gate caught that local tests could not**: a module name that collides with
  one the Lambda runtime pre-loads, so the code worked locally and failed only once deployed.
  `docs/results.md` §4.

## The UI

Four tabs. `docs/design.md`, `docs/tasks.md` and `docs/results.md` are the historical design, build
plan and measurement record; each carries a dated **Superseded** note where the UI has moved on
since. This table is the current state.

| Tab | What it does |
|---|---|
| **SCAN** | Pick a corpus sample, paste an email, or **upload a `.eml` / `.txt` / `.json` file**. Choose the model and region, **edit the system prompt live**, then press **Simple Scan** or **Deep Scan**. Each button runs one stage in one request, so the latency on each card is that stage alone; a comparison table sits underneath. |
| **SCAN RESULTS** | This session's scans with percentiles, and below it every scan the backend has persisted — both buttons, the pipeline route and the public API — badged by source, with the full stored email on each expanded row. |
| **BENCHMARK** | Charts a harness `results.json`. Four built-in runs ship inside the page (two measured, two marked illustrative), and **Download sample results.json** emits an annotated template of the required format. |
| **API INTEGRATION** | The three routes, their **two different credentials**, the key, the limits, and worked curl and Python covering region, model, prompt, token, tier and attachment overrides, plus posting a file straight off disk. |

### Uploading email

The Scan tab reads the file **in the browser**; the file is never uploaded anywhere, only the text
it contains, and only when you press a scan button. `.eml` and `.txt` are split on their leading
`From:` / `To:` / `Subject:` lines by a direct port of the backend's `parse_pasted_email`, so the
fields show exactly what the API would have derived from the same bytes (there is a parity test
over both implementations). `.json` carrying `{from, subject, body, attachments}` loads as-is.

Attachment files contribute **their names only** — nothing binary is parsed or uploaded. The
filename *is* the signal: a double extension like `viewer_setup.pdf.exe` is the whole finding.

The API takes the same input three ways: `{"email": {...}}`, `{"text": "<raw paste>"}`, or a raw
body with `content-type: message/rfc822` (options then move to the query string). The raw form
exists because a real message is full of quotes, backslashes and newlines, and hand-escaping one
into JSON is the step most likely to go wrong — `--data-binary @message.eml` just works.

### Models

The dropdown offers **every Gemma model this account can see**: the two hand-verified lanes first,
then anything else `bedrock:ListFoundationModels` reports, each labelled *discovered, unverified*.
That distinction is load-bearing — a discovered model means the entitlement exists, not that this
PoC has ever called it, measured its latency, or tested it outside the one region it was listed in.
Discovery adds and never replaces: the primary model has no foundation-model ARN and is invisible to
that API, so a list without it must not be read as the model being gone. If the Lambda role lacks
`bedrock:ListFoundationModels` the call degrades to the verified registry with the exception shown
in the UI.

No display name anywhere contains a provider name. Model **IDs** keep their `google.` prefix,
because that is the literal Bedrock identifier and sending anything else is a 400.

### Amazon Bedrock only

Every model is invoked on Amazon Bedrock — `bedrock-mantle.{region}.api.aws` or
`bedrock-runtime.{region}.amazonaws.com`. No Vertex AI, AI Studio or
`generativelanguage.googleapis.com` endpoint is reachable from any code path in this repo;
`google.` in a model ID is Bedrock's namespace for the provider. Each scan response carries the
exact host it used in `servedVia`, and the UI renders that string rather than asserting it.

### Editing the system prompt

The Scan tab's editor changes the **instruction body only**. The server re-appends the JSON
shape (on the Converse lane, which has no `response_format`) and then the prompt-injection
guard, always last — so an edit can retune the analysis and cannot delete the guard on a
product whose input is adversarial by definition. Honoured on `/scan`, `/classify` and
`/deep-scan`; **ignored on `/public/scan`**, where a key-holder supplying system instructions
would have a general-purpose LLM proxy on your Bedrock quota. `smoke.sh` step 6 asserts the
guard survives an override.

### Regions

The default is **`eu-central-1`** — an EU default, in the UI and in the API, overridable
per-request with `"region"`. Three facts come with it:

- It is the only EU region the primary model answers in. `eu-west-1` returns 404 for it.
- It is slower than N. Virginia on the same model and prompt: **758 ms against 489 ms**. That
  is the price of the EU default, and the UI says so next to the selector.
- The **fallback model cannot run there at all** (Frankfurt has zero Gemma models on
  `bedrock-runtime`), so the Lambda logs a fallback-lane warning at every cold start in this
  default. The two lanes have disjoint EU regions, and `us-east-1` is the only region that can
  A/B them — a real constraint on an EU story, not a quirk of this stack.

`var.bedrock_region` is deliberately separate from `var.region`: where the stack runs and where
Bedrock is called are two decisions, so an EU model default does not drag the API Gateway,
DynamoDB table and Cognito pool to Frankfurt with it.

## Running it

You need an AWS account, a Route53 public hosted zone you control, Terraform, and Bedrock
model access in your region.

```bash
cp infra/terraform.tfvars.example infra/terraform.tfvars   # then edit it
export AWS_PROFILE=<your-profile> AWS_REGION=us-east-1

./deploy.sh        # plan review gate, then apply
./smoke.sh         # warm both lanes and assert the latency targets before demoing

python3 bench/driver.py --lane primary --concurrency 4 --duration 25
python3 bench/report.py bench/results/results.json
```

`deploy.sh` refuses to apply a plan that destroys or modifies anything, and refuses if
`terraform.tfvars` is missing `project`.

### Costs

It calls Bedrock, so it costs money per scan, and it provisions CloudFront, Lambda, API
Gateway, DynamoDB and Cognito. Idle cost is small; a load test is not. `docs/results.md` has
the measured per-request figures. **Run `terraform destroy` when you are done.**

### Security notes if you deploy this

- `infra/terraform.tfvars` is gitignored and holds your demo password. Generate your own.
- There is an **optional public endpoint** (`POST /api/public/scan`) guarded only by an API
  key, a per-route rate limit and a daily cap. It is reachable by anyone with the key. Read
  `docs/results.md` §6a before enabling it, and rotate the key if it is ever exposed.
- Run history persists the **full email body** by default, with a 7-day TTL. If that is not
  acceptable for your data, stop populating the `email` field in `runs_store.record_run` —
  it is a one-field change.
- Terraform state holds the generated secrets in cleartext. Use an encrypted remote backend
  for anything beyond a throwaway.

## Licence

MIT — see [LICENSE](LICENSE).
