"""Discover every Gemma model the account can actually see on Bedrock.

WHY THIS EXISTS, AND WHAT IT CANNOT DO

The registry in `model_registry.py` holds two models whose whole `(id, endpoint, path, region)`
tuple was verified by hand against live Bedrock. That is the reason it is trustworthy and it is
also its limit: it cannot list a model nobody has tried. Asking the UI to "offer every Gemma model
Bedrock supports" therefore has exactly one honest implementation -- ask Bedrock -- because the
alternative is guessing model IDs, and a guessed ID is a `ValidationException` in front of an
audience. That is precisely the failure the registry was written to prevent.

So: `bedrock:ListFoundationModels`, filtered to the Gemma family, merged over the registry.

TWO LIMITS THAT MUST NOT BE PAPERED OVER

  1. `list-foundation-models` DOES NOT SEE THE PRIMARY MODEL. google.gemma-4-26b-a4b has no
     foundation-model ARN at all and `get-foundation-model` rejects the identifier (design 3.1).
     Discovery cannot find it and never will; it is in the registry because someone called it
     successfully. So discovery ADDS to the registry and never replaces it -- a discovered list
     that came back without Gemma 4 must not be read as "Gemma 4 is gone".

  2. A DISCOVERED MODEL IS NOT A VERIFIED MODEL. Being listed means the account is entitled to
     see it. It does not mean it answers on the lane we would route it to, that it accepts our
     `response_format`, or that it works in this region. Every discovered entry is therefore
     marked `verified: False` and the UI labels it as unverified, rather than presenting it
     alongside two models whose numbers were measured.

Failure posture: never raises, never blocks. A refused or slow ListFoundationModels returns the
registry alone with a note, because a model dropdown that is two entries short is a worse outcome
than a scan tab that will not render.
"""

import logging
import os
import time

import boto3
from botocore.config import Config

import model_registry

log = logging.getLogger()

# The family this PoC is about. Matched case-insensitively against the model id and name, so
# `google.gemma-3-27b-it`, `gemma-2-9b-it` and any future `*gemma*` all land.
_FAMILY = "gemma"

# Short, and one attempt beyond the first. This runs inside GET /models, which the UI blocks its
# first paint on; a slow control-plane call must not become a slow login.
_CONFIG = Config(retries={"max_attempts": 2, "mode": "standard"},
                 connect_timeout=1, read_timeout=3)

# Module scope so a warm invocation reuses the client and its credentials.
_clients = {}

# Discovery is a control-plane fact that changes when someone requests model access, not per
# request. Cached for the life of the execution environment, with the timestamp kept so the UI can
# say how old the list is rather than implying it is live.
_CACHE = {"at": 0.0, "models": None, "note": None, "region": None}
_CACHE_TTL_SECONDS = 300


def _client(region):
    if region not in _clients:
        _clients[region] = boto3.client("bedrock", region_name=region, config=_CONFIG)
    return _clients[region]


def _is_gemma(entry):
    blob = " ".join(str(entry.get(k) or "") for k in ("modelId", "modelName", "providerName"))
    return _FAMILY in blob.lower()


def _display_label(model_id, model_name):
    """A human label with the provider stripped out of it.

    The model ID keeps its `google.` prefix because that is the literal Bedrock identifier and
    sending anything else is a 400 -- but nothing the UI *shows as a name* needs to carry it.
    `google.gemma-3-27b-it` becomes "Gemma 3 27B IT".
    """
    raw = (model_name or model_id or "").strip()
    # Drop a leading provider namespace: "google.gemma-3-27b-it" -> "gemma-3-27b-it".
    if "." in raw:
        raw = raw.split(".", 1)[1]
    # Drop an inference-profile region prefix if one ever appears: "eu.gemma-..." is handled by
    # the same split above; this catches "Gemma 3 27B (Google)"-style vendor suffixes.
    for noise in ("(Google)", "(google)", "Google"):
        raw = raw.replace(noise, "")
    words = [w for w in raw.replace("_", "-").replace(".", " ").split("-") if w]
    out = []
    for w in words:
        if w.lower() == "gemma":
            out.append("Gemma")
        elif w.isdigit():
            out.append(w)
        elif len(w) <= 3 and any(c.isdigit() for c in w):
            out.append(w.upper())        # 27b -> 27B, a4b -> A4B
        elif len(w) <= 3:
            out.append(w.upper())        # it -> IT
        else:
            out.append(w[:1].upper() + w[1:])
    return " ".join(out).strip() or (model_id or "unknown")


def discover(region=None, force=False):
    """Return (models, note). `models` is a list of registry-shaped dicts; `note` may be None.

    Registry entries come first and always win on id collision -- their metadata is measured and a
    discovered entry's is not.

    EVERY OFFERED REGION IS PROBED, not just `region`. This started as a single call against the
    default Bedrock region and found nothing, which looked like "there are no other Gemma models"
    and was wrong: eu-central-1 is precisely the region that lists ZERO Gemma models (see
    model_registry's docstring), while us-east-1 and eu-west-1 each list three. Probing only the
    default therefore hid every discoverable model behind the one region guaranteed to have none.

    A discovered model's `regions` is the set of regions it was ACTUALLY listed in, which is what
    lets the UI grey out the pairs that do not exist -- and is why this cannot be one call.
    """
    region = region or model_registry.DEFAULT_REGION
    now = time.monotonic()
    if (not force and _CACHE["models"] is not None and _CACHE["region"] == region
            and now - _CACHE["at"] < _CACHE_TTL_SECONDS):
        return _CACHE["models"], _CACHE["note"]

    # The default first, so its label and `region` field win for a model listed in several.
    probe_regions = [region] + [r for r in model_registry.all_regions() if r != region]

    registry_ids = {m["id"] for m in model_registry.MODELS}
    found = {}          # id -> entry, accumulating `regions` across probes
    failures = []

    if os.environ.get("DISABLE_MODEL_DISCOVERY"):
        note = "Model discovery is disabled by DISABLE_MODEL_DISCOVERY; showing the verified registry only."
    else:
        note = None
        for probe in probe_regions:
            try:
                response = _client(probe).list_foundation_models(byProvider="Google")
            except Exception as exc:  # noqa: BLE001 -- a short dropdown beats a broken tab
                # AccessDeniedException here means the Lambda role is missing
                # bedrock:ListFoundationModels. Kept verbatim so it is fixable from the UI banner.
                log.warning("model discovery failed in %s: %s: %s", probe, type(exc).__name__, exc)
                failures.append(f"{probe} ({type(exc).__name__}: {exc})")
                continue

            for entry in (response.get("modelSummaries") or []):
                if not _is_gemma(entry):
                    continue
                model_id = entry.get("modelId") or ""
                # An inference-profile-only model must be called by profile ID, not base ID, and
                # this PoC's two lanes both send a base ID. Skipped rather than offered as a 400.
                inference_types = entry.get("inferenceTypesSupported") or []
                if inference_types and "ON_DEMAND" not in inference_types:
                    continue
                if not model_id or model_id in registry_ids:
                    continue

                if model_id in found:
                    found[model_id]["regions"].append(probe)
                    continue

                found[model_id] = {
                    "id": model_id,
                    "label": _display_label(model_id, entry.get("modelName")),
                    # Every discovered model is reached through Converse on bedrock-runtime. The
                    # mantle lane's `/openai/v1/*` route carries exactly one model that this API
                    # cannot see, so there is nothing to discover onto it.
                    "endpoint": "runtime",
                    "path": None,
                    "style": "converse",
                    "provider": entry.get("providerName") or "Google",
                    "region": probe,
                    # Only the regions it was actually listed in. Claiming the others would be
                    # inventing the very (model, region) facts the registry earned by testing.
                    "regions": [probe],
                    "supports_json_schema": False,
                    "supports_prompt_caching": False,
                    "supports_latency_optimized": False,
                    "unsupported_reason": {
                        "json_schema":
                            "Converse exposes no response_format parameter, so output shape is "
                            "prompt-enforced on this lane and the fence-stripping parser is "
                            "load-bearing.",
                    },
                    "observed_latency_ms": None,
                    "observed_latency_by_region_ms": {},
                    "verified": False,
                    "notes": ("Discovered via bedrock:ListFoundationModels. Served by Amazon "
                              "Bedrock over Converse. NOT verified by this PoC: no latency was "
                              "measured on it and no scan has been run against it, so treat a "
                              "result here as an experiment rather than as a measurement."),
                }

        if failures:
            note = ("Could not list Bedrock models in " + "; ".join(failures) +
                    ". Any model only available there is missing from this list.")

    ordered = sorted(found.values(), key=lambda e: e["id"])
    for entry in ordered:
        entry["regions"] = sorted(set(entry["regions"]))

    models = [dict(m, verified=True) for m in model_registry.MODELS] + ordered
    if ordered and not note:
        note = (f"{len(ordered)} additional Gemma model(s) discovered across "
                f"{', '.join(probe_regions)} and offered unverified.")

    _CACHE.update(at=now, models=models, note=note, region=region)
    return models, note
