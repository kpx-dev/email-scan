#!/usr/bin/env bash
#
# Deploy the email-scan PoC.
#
# Hardened from an internal prior-art deploy script (tasks.md T4.1). That version was `set -e`
# only, did not export AWS_PROFILE, and ran `terraform apply -auto-approve` with no plan
# review. All three are fixed here.
#
# Usage:
#   ./deploy.sh              # plan, show it, ask, then apply
#   ./deploy.sh --yes        # skip the interactive confirm (plan is still produced and shown)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

AUTO_APPROVE=false
[[ "${1:-}" == "--yes" ]] && AUTO_APPROVE=true

# Set EXPECTED_ACCOUNT to refuse to run anywhere else. Leave empty to skip the check.
readonly EXPECTED_ACCOUNT="${EXPECTED_ACCOUNT:-}"

say() { printf '\n\033[1m=== %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mFATAL: %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- preflight
say "Preflight"

# The Cognito null_resource shells out to the AWS CLI at apply time and does NOT inherit the
# provider's credentials, so the shell must carry a working profile.
: "${AWS_PROFILE:?AWS_PROFILE is not set. Run: export AWS_PROFILE=aws}"
# The region the STACK lives in, read from tfvars rather than defaulted.
#
# This was `${AWS_REGION:-us-east-1}`, which only fills the variable in when it is UNSET -- so an
# operator whose shell already exported a different region (a very common thing) silently ran every
# `aws` call in this script against the wrong one. Cognito is regional, so smoke.sh's initiate-auth
# then fails against a user pool that does not exist there, and the error names the client id rather
# than the region, which sends you looking in the wrong place entirely.
#
# tfvars is the single source of truth for where the stack is, so read it and say so when it
# disagrees with the environment. Note this is the STACK region and not necessarily the Bedrock
# region -- see var.bedrock_region.
_TFVARS_REGION="$(sed -n 's/^[[:space:]]*region[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' \
                  infra/terraform.tfvars 2>/dev/null | head -1)"
if [[ -n "${AWS_REGION:-}" && -n "$_TFVARS_REGION" && "$AWS_REGION" != "$_TFVARS_REGION" ]]; then
  echo "note: AWS_REGION=$AWS_REGION in your shell, but this stack is in $_TFVARS_REGION (infra/terraform.tfvars). Using $_TFVARS_REGION."
fi
export AWS_REGION="${_TFVARS_REGION:-${AWS_REGION:-us-east-1}}"

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
if [[ -n "$EXPECTED_ACCOUNT" && "$ACCOUNT" != "$EXPECTED_ACCOUNT" ]]; then
  die "wrong account: got $ACCOUNT, expected $EXPECTED_ACCOUNT"
fi
echo "  account   $ACCOUNT"
echo "  profile   $AWS_PROFILE"
echo "  region    $AWS_REGION"

# tasks.md 0.4 rule 1: a terraform.tfstate copied from another stack would put THAT stack
# under this one's management, and the next apply would mutate it. If you bootstrapped this
# from an existing stack, set EXPECTED_STATE_LINEAGE to that stack's lineage to refuse it.
if compgen -G "infra/terraform.tfstate*" > /dev/null; then
  if [[ -n "${FOREIGN_STATE_LINEAGE:-}" ]] \
     && grep -q "$FOREIGN_STATE_LINEAGE" infra/terraform.tfstate 2>/dev/null; then
    die "infra/terraform.tfstate carries a foreign lineage. Delete it; do not apply."
  fi
  echo "  state     present"
else
  echo "  state     none yet (first deploy)"
fi

# tasks.md 0.4 rule 2: aws_route53_record is an UPSERT, so a stale domain_name silently re-points
# a live A-alias and shows NO destroy in the plan.
# `[^"]*`, not `.*`: a greedy capture runs to the LAST quote on the line, so a trailing
# comment containing a quote -- which the tfvars project line has -- silently swallowed the
# comment into the value and failed the prefix check below with a nonsense name.
PROJECT="$(sed -n 's/^[[:space:]]*project[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' infra/terraform.tfvars | head -1)"
[[ -n "$PROJECT" ]] \
  || die "infra/terraform.tfvars does not set project — every resource name derives from it, and the module default may collide with another stack"
grep -q "domain_name *= *\"$PROJECT\." infra/terraform.tfvars \
  || die "infra/terraform.tfvars domain_name does not start with the project prefix ($PROJECT.)"
echo "  tfvars    project=$PROJECT + matching domain_name OK"

# tasks.md T3.7: archive_file zips source_dir verbatim, so stale .pyc files change
# output_base64sha256 on every run and force a spurious Lambda update.
find backend -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
find backend -name '*.pyc' -delete 2>/dev/null || true
echo "  backend   __pycache__ purged"

# ---------------------------------------------------------------- plan
say "Terraform init + validate"
terraform -chdir=infra init -input=false
terraform -chdir=infra validate

say "Terraform plan"
terraform -chdir=infra plan -input=false -out=tfplan

# The review gate (tasks.md T3.13).
#
# ORIGINALLY create-only: any update was fatal. That was right for the first apply and wrong from
# the second onwards -- it made the script unable to ship a code change to a stack it owns, which
# is the normal case. Updates are now allowed and LISTED, attribute by attribute, so the operator
# confirmation below is a real review rather than a keystroke.
#
# What is still fatal, and why each one:
#
#   delete    unchanged. Nothing this script runs may destroy a resource.
#   REPLACE   NEW, and it is the whole reason allowing updates needs care. A replace is a destroy
#             plus a create on the same address, and Terraform reports it as ["delete","create"] or
#             ["create","delete"] -- so a gate that only greps for a bare ["delete"] would wave one
#             through. On this stack a replace is data loss with a friendly name: replacing
#             aws_dynamodb_table.runs drops every persisted scan, and replacing the Cognito pool
#             invalidates every user. The old gate blocked these only as a side effect of blocking
#             everything; now it is explicit.
#   outside   a changed address that is not in this configuration. Belt and braces -- Terraform
#             structurally cannot modify what its own state does not track -- but it costs two
#             lines and it fails loudly if this script is ever pointed at the wrong -chdir.
#   route53   unchanged: every DNS record created must sit under our own project prefix. An UPSERT
#             on someone else's record shows no destroy, so this is the only place to catch it.
say "Plan review gate"
terraform -chdir=infra show -json tfplan > /tmp/email-scan-tfplan.json
PROJECT="$PROJECT" python3 - <<'PY'
import json, os, sys

RED, BOLD, OFF = "\033[31m", "\033[1m", "\033[0m"

plan = json.load(open('/tmp/email-scan-tfplan.json'))
changes = plan.get('resource_changes', [])


def actions(c):
    return c['change']['actions']


# A replace is destroy+create on one address, in either order. Matched on the SET so neither
# ordering slips past, and taken out of `delete`/`create` so it cannot be double-reported.
replace = [c for c in changes if set(actions(c)) == {'create', 'delete'}]
replace_addrs = {c['address'] for c in replace}
create = [c for c in changes if actions(c) == ['create']]
update = [c for c in changes if actions(c) == ['update']]
delete = [c for c in changes if actions(c) == ['delete'] and c['address'] not in replace_addrs]

print(f"  to add     {len(create)}")
print(f"  to change  {len(update)}")
print(f"  to replace {len(replace)}")
print(f"  to destroy {len(delete)}")


def changed_keys(c):
    """Top-level attributes whose value actually differs. Feeds the printed review."""
    before = c['change'].get('before') or {}
    after = c['change'].get('after') or {}
    unknown = c['change'].get('after_unknown') or {}
    keys = sorted(set(before) | set(after) | set(unknown))
    out = []
    for k in keys:
        if unknown.get(k):
            out.append(k + ' (known after apply)')
        elif before.get(k) != after.get(k):
            out.append(k)
    return out


if create:
    print("\n  creating:")
    for c in create:
        print(f"    + {c['address']}")
if update:
    print("\n  changing in place:")
    for c in update:
        keys = changed_keys(c)
        shown = ', '.join(keys[:6]) + (f", +{len(keys) - 6} more" if len(keys) > 6 else '')
        print(f"    ~ {c['address']}")
        print(f"        {shown or '(no attribute diff reported)'}")

fatal = []
if delete:
    fatal.append(f"plan destroys {len(delete)} resource(s): " +
                 ", ".join(c['address'] for c in delete))
if replace:
    fatal.append(
        f"plan REPLACES {len(replace)} resource(s), which is a destroy plus a create: " +
        ", ".join(c['address'] for c in replace) +
        ". On this stack that is data loss: replacing the DynamoDB table drops every persisted "
        "scan, and replacing the Cognito pool invalidates every user. If a replace is genuinely "
        "intended, do it deliberately and outside this script."
    )

# Every changed address must belong to this configuration. `configuration` lists what the code
# declares; a changed address outside it means this script is pointed somewhere it does not own.
declared = {r['address'] for r in
            (plan.get('configuration', {}).get('root_module', {}).get('resources') or [])}
if declared:
    for c in changes:
        # Strip an index suffix so count/for_each instances match their declared address.
        base = c['address'].split('[')[0]
        if base not in declared:
            fatal.append(f"changed resource is not declared in this configuration: {c['address']}")

project = os.environ.get('PROJECT', '')
for c in create + update + replace:
    if c['type'] == 'aws_route53_record':
        name = ((c['change'].get('after') or {}).get('name') or '')
        if name and project and not name.startswith(project):
            fatal.append(f"route53 record outside the project prefix {project!r}: {name}")

if fatal:
    print(f"\n{RED}{BOLD}PLAN REVIEW FAILED{OFF}")
    for f in fatal:
        print("  - " + f)
    sys.exit(1)

# `changes` also carries every no-op resource, so counting it here printed "29 change(s)" directly
# under "to change 1" -- a summary line that contradicts the numbers above it is worse than no
# summary line. Count only what actually moves.
acting = len(create) + len(update) + len(replace) + len(delete)
if not acting:
    print("\n  gate       PASS (nothing to do -- infrastructure already matches the code)")
else:
    print(f"\n  gate       PASS (no deletes, no replaces, {acting} change(s) all declared "
          f"in this configuration)")
    print("             Read the list above before answering the prompt.")
PY

if [[ "$AUTO_APPROVE" != true ]]; then
  say "Confirm"
  read -r -p "Apply this plan to account $ACCOUNT? [y/N] " reply
  [[ "$reply" == "y" || "$reply" == "Y" ]] || die "aborted by operator"
fi

# ---------------------------------------------------------------- apply
say "Terraform apply"
terraform -chdir=infra apply -input=false tfplan

FRONTEND_BUCKET="$(terraform -chdir=infra output -raw frontend_bucket)"
CF_DIST_ID="$(terraform -chdir=infra output -raw cloudfront_distribution_id)"
COGNITO_CLIENT_ID="$(terraform -chdir=infra output -raw cognito_client_id)"
COGNITO_POOL_ID="$(terraform -chdir=infra output -raw cognito_user_pool_id)"
SITE_URL="$(terraform -chdir=infra output -raw cloudfront_url)"

# Positive check, not a denylist. The only distribution this script may invalidate is the
# one THIS stack's state owns; anything else means a stale output or the wrong workspace, and
# invalidating a stranger's distribution is confusing at best.
OWNED_DIST="$(terraform -chdir=infra state show aws_cloudfront_distribution.main 2>/dev/null \
              | sed -n 's/^[[:space:]]*id[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)"
[[ -n "$CF_DIST_ID" && ( -z "$OWNED_DIST" || "$CF_DIST_ID" == "$OWNED_DIST" ) ]] \
  || die "output distribution $CF_DIST_ID is not the one in state ($OWNED_DIST) -- refusing to invalidate"

# ---------------------------------------------------------------- frontend
say "Inject runtime config and upload the SPA"

# deploy-time config reaches the SPA by sed on the literal closing head tag, so index.html must
# contain exactly one.
heads="$(grep -c '</head>' frontend/index.html)"
[[ "$heads" == "1" ]] || die "frontend/index.html has $heads </head> tags; the sed injection needs exactly 1"

sed "s|</head>|<script>window.COGNITO_CLIENT_ID='${COGNITO_CLIENT_ID}';window.COGNITO_POOL_ID='${COGNITO_POOL_ID}';window.AWS_REGION='${AWS_REGION}';</script></head>|" \
  frontend/index.html > frontend/index_deploy.html

aws s3 cp frontend/index_deploy.html "s3://${FRONTEND_BUCKET}/index.html" \
  --content-type 'text/html; charset=utf-8' \
  --cache-control 'public, max-age=60, must-revalidate'

# The Benchmark tab fetches /results.json at the site root. Without this the object is absent,
# and because the OAC bucket policy grants s3:GetObject and nothing else -- no s3:ListBucket --
# S3 answers a missing key 403 AccessDenied rather than 404. That reads like a permissions bug
# and is not one, so ship the file whenever the harness has produced one. Optional on purpose:
# the SPA carries built-in runs, so a deploy with no local harness output still charts.
if [[ -f bench/results/results.json ]]; then
  aws s3 cp bench/results/results.json "s3://${FRONTEND_BUCKET}/results.json" \
    --content-type 'application/json; charset=utf-8' \
    --cache-control 'public, max-age=60, must-revalidate'
else
  say "No bench/results/results.json -- skipping; the Benchmark tab will fall back to its built-in runs"
fi

say "Invalidate CloudFront"
aws cloudfront create-invalidation --distribution-id "$CF_DIST_ID" --paths '/*' \
  --query 'Invalidation.{Id:Id,Status:Status}' --output table

# ---------------------------------------------------------------- done
say "Deployed"
cat <<EOF
  URL        $SITE_URL
  login      $(terraform -chdir=infra output -raw cognito_demo_username 2>/dev/null || echo demo) / the cognito_demo_password from infra/terraform.tfvars
  bucket     $FRONTEND_BUCKET
  dist       $CF_DIST_ID

  A new distribution takes 5-15 min to reach Deployed, and ACM DNS validation adds 2-5 min.
  Check before testing:
    aws cloudfront get-distribution --id $CF_DIST_ID --query 'Distribution.Status'

  Then run ./smoke.sh before showing anyone — cold TTFT is ~4x warm.
EOF
