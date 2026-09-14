"""Output schemas for the two stages, plus the one JSON parser and the one gate.

Imported by both the Lambda and `bench/driver.py`. That shared import is the whole
point: it is what makes the demo's numbers and the harness's numbers measurements of
the *same request* rather than two similar ones (design 8.1).

Four things live here, and all four for the same reason -- there must be exactly one of
each in the repo, and both the Lambda and the harness must use it:

    STAGE1_SCHEMA / STAGE2_SCHEMA   the request the demo and the harness both send
    extract_json                    the fence-stripping parser (load-bearing on Gemma 3)
    format_email                    the email -> prompt-text rendering
    should_escalate                 design 4.4's escalation gate

The last two were mirrored copies in handler.py and bench/driver.py, and they had
already drifted: the harness's URL regex matched only `http://` and `www.`, while the
handler's also matched raw-IP hosts, punycode and host-with-path. The escalation rate is
the single most valuable number this PoC produces (design 9.3) and it is computed by the
harness from *its* gate against *its* rendering, so a drifted mirror does not produce a
slightly-off number -- it produces a number that does not describe production at all.

`strict: true` is not a stylistic choice. Measured on google.gemma-4-26b-a4b:

  response_format omitted                     ```-fenced JSON, non-deterministic
                                              field names -- 856ms
  {"type": "json_object"}                     unfenced but arbitrary field names, and
                                              400s unless the word "json" appears in
                                              messages
  {"type": "json_schema", "strict": true}     schema-exact, unfenced, and fastest -- 543ms

So the strict schema is simultaneously the fastest and the most robust option, and
there is no trade-off to make (design 4.3).

The Gemma 3 fallback lane has **no json_schema equivalent** -- Converse offers no such
parameter -- so on that lane shape is prompt-enforced only and `extract_json` below is
load-bearing rather than defensive theatre. It lives here next to the schemas so
`bench/` gets the schemas and the parser from a single import.
"""

import json
import re

# Design 4.3, verbatim. `additionalProperties: False` plus a complete `required` list
# are both mandatory under strict mode -- a partial `required` is rejected outright.
STAGE1_SCHEMA = {
    "name": "stage1_verdict",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["verdict", "confidence", "signals"],
        "properties": {
            "verdict": {"type": "string", "enum": ["benign", "suspicious", "malicious"]},
            "confidence": {"type": "number"},
            "signals": {
                "type": "array",
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["signal", "detail"],
                    "properties": {
                        "signal": {"type": "string"},
                        "detail": {"type": "string"},
                    },
                },
            },
        },
    },
}

# Design 4.1's deep-scan fields. `disagreesWithStage1` is a deliberate design choice
# rather than a nicety: it makes the two-stage pipeline legible, because when Stage 2
# overturns Stage 1 the demo shows the pipeline earning its second call.
STAGE2_SCHEMA = {
    "name": "stage2_deep_scan",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "verdict",
            "riskScore",
            "threatTypes",
            "senderAnalysis",
            "urls",
            "socialEngineeringTactics",
            "attachmentFindings",
            "recommendedAction",
            "disagreesWithStage1",
            "reasoning",
        ],
        "properties": {
            "verdict": {"type": "string", "enum": ["benign", "suspicious", "malicious"]},
            "riskScore": {"type": "integer", "description": "0-100, higher is worse"},
            "threatTypes": {
                "type": "array",
                "maxItems": 4,
                "items": {
                    "type": "string",
                    "enum": [
                        "phishing",
                        "bec",
                        "malware",
                        "spam",
                        "scam",
                        "credential-harvesting",
                        "none",
                    ],
                },
            },
            "senderAnalysis": {"type": "string"},
            "urls": {
                "type": "array",
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["url", "verdict", "reason"],
                    "properties": {
                        "url": {"type": "string"},
                        "verdict": {
                            "type": "string",
                            "enum": ["safe", "suspicious", "malicious"],
                        },
                        "reason": {"type": "string"},
                    },
                },
            },
            "socialEngineeringTactics": {
                "type": "array",
                "maxItems": 6,
                "items": {
                    "type": "string",
                    "enum": [
                        "urgency",
                        "authority",
                        "fear",
                        "scarcity",
                        "impersonation",
                        "reward",
                        "none",
                    ],
                },
            },
            "attachmentFindings": {
                "type": "array",
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "risk", "reason"],
                    "properties": {
                        "name": {"type": "string"},
                        "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                        "reason": {"type": "string"},
                    },
                },
            },
            "recommendedAction": {
                "type": "string",
                "enum": ["allow", "warn", "quarantine", "block"],
            },
            # True when Stage 2's verdict differs from the Stage 1 verdict it was given.
            "disagreesWithStage1": {"type": "boolean"},
            "reasoning": {"type": "string"},
        },
    },
}

SCHEMAS = {
    "classify": STAGE1_SCHEMA,
    "deep-scan": STAGE2_SCHEMA,
}


def response_format(stage):
    """The `response_format` value for a stage, in OpenAI-compatible shape.

    Only meaningful on the mantle lane; the caller gates on
    `model["supports_json_schema"]` before sending it.
    """
    return {"type": "json_schema", "json_schema": SCHEMAS[stage]}


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def extract_json(text):
    """Pull a JSON object out of model output.

    Necessary, not defensive theatre: in Step 0 testing 3 of 5 models wrapped their
    output in ```json fences despite being explicitly told not to -- including both
    Gemma variants, the likely production model.

    Returns (parsed_dict_or_None, error_string_or_None).
    """
    if not text:
        return None, "empty response"

    cleaned = _FENCE_RE.sub("", text.strip())
    try:
        return json.loads(cleaned), None
    except json.JSONDecodeError:
        pass

    # Fall back to the outermost brace pair, which survives leading/trailing prose.
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(cleaned[start : end + 1]), None
        except json.JSONDecodeError as exc:
            return None, f"JSON decode failed: {exc}"
    return None, "no JSON object found in response"


def format_email(email):
    """Render the email dict into the text actually sent to the model.

    Shared rather than mirrored: the gate below runs its regexes over exactly this
    string, so the harness measuring an escalation rate against a differently-rendered
    email is measuring a different pipeline. The `headers` branch matters -- corpus
    sample 06 plants a prompt injection in an X- header, and that is the only test that
    branch has.
    """
    parts = []
    if email.get("from"):
        parts.append(f"From: {email['from']}")
    if email.get("to"):
        parts.append(f"To: {email['to']}")
    if email.get("subject"):
        parts.append(f"Subject: {email['subject']}")
    for key, value in (email.get("headers") or {}).items():
        parts.append(f"{key}: {value}")
    attachments = email.get("attachments") or []
    if attachments:
        parts.append(f"Attachments: {', '.join(attachments)}")
    parts.append("")
    parts.append(email.get("body", ""))
    return "\n".join(parts)


# --------------------------------------------------------------------------------------
# The escalation gate (design 4.4)
# --------------------------------------------------------------------------------------
# Deliberately broad. These two regex clauses only decide whether to spend a second model
# call, so a false positive costs ~600 tokens while a false negative costs a missed deep
# scan on a phish the triage stage already got wrong.
#
# A bare sender domain must NOT match, or every email escalates and the gate stops being
# a gate -- hence the scheme / www / raw-IP-host / punycode / host-with-path alternatives
# rather than "anything with a dot in it".
#
# The raw-IP alternative has to require a path or a port, and this cost the corpus its only
# negative case before it was caught: `\b(?:\d{1,3}\.){3}\d{1,3}\b` on its own matches the
# dotted quad inside `Received: from mail-out-2... ([192.0.2.30]) by mx...`, which is a
# header every legitimately-delivered email carries. All 7 corpus samples matched on it,
# including 05-newsletter-benign -- the sample written specifically with no link and no
# attachment so that `should_escalate: false` has something to prove (design 12.2, and
# smoke.sh step 4 asserts it). An IP is a URL when it is being used as a host; a delivery
# header is not a URL, and `http://198.51.100.7/mfa` still matches on the scheme clause.
URL_RE = re.compile(
    r"https?://"
    r"|www\.[a-z0-9-]"
    r"|\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{2,5})?/"
    r"|\bxn--"
    r"|\b[a-z0-9][a-z0-9.-]*\.[a-z]{2,24}/",
    re.IGNORECASE,
)

# Matches the `Attachments:` line format_email renders, a raw MIME disposition header,
# and a bare risky filename in a pasted body.
#
# TRAP, found by running the harness: do NOT add `com` to the extension list. `.com` is a
# real Windows executable extension, and it also matches the tail of every domain in the
# corpus, so a benign newsletter from news@devtools-weekly.example.com escalates on
# "attachment_present_despite_benign" and the measured escalation rate goes to 100%.
ATTACHMENT_RE = re.compile(
    r"^\s*(?:attachments?|content-disposition)\s*:"
    r"|\w\.(?:exe|scr|bat|cmd|pif|vbs|jse?|jar|msi|dll|lnk|iso|img|apk"
    r"|zip|rar|7z|gz|tgz|tar|docm|dotm|xlsm|xltm|pptm|ppam"
    r"|docx?|xlsx?|pptx?|pdf|rtf|eml)\b",
    re.IGNORECASE | re.MULTILINE,
)

ESCALATE_BELOW_CONFIDENCE = 0.85


def should_escalate(stage1, email_text, parse_ok):
    """Design 4.4, verbatim in clause order. Returns the reason string, or None.

    The reason is rendered on the UI's escalation arrow, so the gate is visible rather
    than magic -- and the string is the artifact that proves escalation was a server-side
    decision. The same function computes the harness's escalation rate (design 9.3), so
    the number on the cost slide is the behaviour the Lambda actually ships.

    The last two clauses are the point: keying only on the model's verdict means a false
    negative on an email containing http://198.51.100.7/mfa silently skips the deep scan.
    They also mean most mail carrying a link escalates, which is precisely why the
    *measured* rate matters more than design 6.2's assumed 15%.
    """
    if not parse_ok:
        return "parse_failed"
    verdict = (stage1 or {}).get("verdict")
    if verdict != "benign":
        return f"verdict={verdict}"
    confidence = (stage1 or {}).get("confidence")
    if not isinstance(confidence, (int, float)) or confidence < ESCALATE_BELOW_CONFIDENCE:
        return f"low_confidence={confidence}"
    if URL_RE.search(email_text):
        return "url_present_despite_benign"
    if ATTACHMENT_RE.search(email_text):
        return "attachment_present_despite_benign"
    return None
