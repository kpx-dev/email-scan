# --- DynamoDB: run history + the public-endpoint daily quota counter ---
#
# One table serves both, keyed by a constant partition:
#
#   pk "RUN"    sk "<iso8601>#<8-hex-runId>"   one item per scan, UI- or API-originated.
#                                              The sort key sorts chronologically, so a
#                                              descending Query on pk="RUN" is "most recent
#                                              first" with no GSI. A single hot partition is
#                                              correct at PoC volume (single-digit writes/s);
#                                              at real ingest rates this becomes pk =
#                                              "RUN#<date>" and a fan-out read.
#   pk "QUOTA"  sk "<YYYY-MM-DD>"              count, incremented atomically by the Lambda to
#                                              enforce PUBLIC_DAILY_CAP. A leaked x-api-key
#                                              cannot run an unbounded overnight bill even if
#                                              it stays under the gateway's per-second cap.
#
# PRIVACY, stated once: run items carry the FULL email body, full sender address, subject and
# attachment names, by explicit user decision, so the UI can show what was actually scanned.
# That means consumer email content at rest in this account while design.md 10 R5's
# `data_retention: provider_data_share` question is still open and unanswered. The 7-day TTL
# below is the only thing bounding that exposure.
resource "aws_dynamodb_table" "runs" {
  name = "${var.project}-runs"

  # PAY_PER_REQUEST: a demo table with no steady traffic should cost nothing when idle, and
  # a benchmark ramp should not need a provisioned-capacity guess.
  billing_mode = "PAY_PER_REQUEST"

  hash_key  = "pk"
  range_key = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  # Items self-delete 7 days after their expiresAt epoch-second stamp. DynamoDB TTL deletes
  # are asynchronous (typically within 48 h of expiry), so this is a retention ceiling of
  # roughly 7-9 days, not a hard 7-day guarantee. Say that rather than imply a promise.
  ttl {
    attribute_name = "expiresAt"
    enabled        = true
  }

  # point_in_time_recovery is deliberately NOT enabled, and cost is the smaller reason:
  # continuous backups bill per GB-month of backup size, which is near-zero here, but PITR
  # keeps a restorable copy of every write for 35 days. On a table whose whole retention
  # posture is a 7-day TTL over consumer email content, that would quietly quintuple the
  # window during which this PoC holds that content — the opposite of what the TTL is for.
  # A PoC losing its demo run history is also not an incident.
  #
  # Encryption at rest is on regardless: DynamoDB always encrypts with an AWS-owned key at no
  # charge. A customer-managed KMS key would add audit-grade key control (and ~$1/month); that
  # belongs in the production design alongside R5, not here.

  tags = {
    Project = var.project
    Purpose = "scan run history + public endpoint daily quota"
  }
}
