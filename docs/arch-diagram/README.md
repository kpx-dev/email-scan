# Architecture Diagrams

Rendered AWS architecture diagrams for the email-scan PoC. Companion to
[`../design.md`](../design.md) (what and why) and [`../tasks.md`](../tasks.md) (how, in order).

Every label, resource name, latency figure, and error string in these diagrams is traceable to
those two documents, which in turn were verified live against account 123456789012 in us-east-1.

---

## The set

| # | Diagram | Answers |
|---|---|---|
| 01 | [Overall architecture](01-overview.png) | What is the whole system, end to end? |
| 02 | [Model and transport routing](02-model-lanes.png) | **Which (model, endpoint, path) combinations actually work?** |
| 03 | [Two-stage scan pipeline](03-two-stage-pipeline.png) | How does a scan flow, and when does it escalate? |
| 04 | [Terraform resource map](04-terraform-resources.png) | **Exactly which AWS resources get created, by which file?** |
| 05 | [Auth and defence in depth](05-auth-security.png) | What stops an unauthorised request, at each layer? |
| 06 | [Benchmark harness and methodology](06-benchmark-harness.png) | How are the latency and throughput numbers earned? |
| 07 | [Latency budget and timeout chain](07-latency-timeouts.png) | Where is latency measured, and which timeout fires first? |

**Start with 01** for the shape of the system. **02 and 04 are the two that prevent real mistakes** —
02 because a single wrong path segment produces a 400 that reads like a model-availability problem,
and 04 because two specific errors there can damage a pre-existing stack in the same account.

---

## Colour legend

Consistent across all seven diagrams:

| Colour | Meaning |
|---|---|
| **Amber** | The **primary** lane — `google.gemma-4-26b-a4b` on `bedrock-mantle` |
| **Blue** | The **fallback** lane — `google.gemma-3-27b-it` on `bedrock-runtime` |
| **Slate** | Ordinary request or data flow |
| **Green** | Verified working, or a target that is met |
| **Red** | Verified broken, unsupported, or blocked — with the verbatim error |
| **Violet** | Conditional: needs a decision, a spike result, or sign-off |
| **Grey** | Context, or something deliberately not built |

Red is used only where there is a real, reproduced error message behind it — not for "risky".

---

## Regenerating

Diagrams are generated from Python, so they are reviewable in git and cannot drift silently from a
hand-edited image.

```bash
cd "$(git rev-parse --show-toplevel)"

# one diagram
python3 docs/arch-diagram/src/01_overview.py

# all of them
for f in docs/arch-diagram/src/[0-9]*.py; do python3 "$f"; done
```

Requires `graphviz` (`brew install graphviz`) and the `diagrams` package
(`pip install diagrams`) — both already present on this machine (graphviz 16.0.0).

### Conventions for editing

`src/_style.py` holds every shared visual decision — colours, fonts, spacing, cluster styling. Change
a colour there, not in individual scripts. `src/01_overview.py` is the reference implementation;
copy its structure when adding a diagram.

Four traps, all hit while building this set. The first two are already solved in `_style.py`; the
other two bite inside individual scripts:

- **Do not use `style="rounded,filled"` on clusters.** Graphviz then fills from the pen colour
  rather than `bgcolor`, producing dark boxes with unreadable labels. Use `style="rounded"` and let
  `bgcolor` fill.
- **Do not set `splines="ortho"`.** It looks right but places edge labels badly, floating them into
  unrelated clusters. The shared config uses `polyline`, which keeps the boxy look and lands labels
  where they belong.
- **Pass `image=""` to `Blank`** when using it as a text box. `diagrams` ships `Blank` with a
  256×256 transparent PNG, which inflates every box to roughly a 1.6-inch square regardless of how
  little text it holds. Clearing the image lets the box size to its label.
- **Do not pass `width=` to an AWS icon node.** `diagrams` scales the icon image to the node box, so
  overriding the width makes the label render *on top of* the icon instead of beneath it. Leave the
  default sizing alone; long labels are fine.

Two cosmetic limits worth knowing rather than fighting: Graphviz's `mincross` ignores cluster
declaration order, so a lane may render above the one declared before it (labels and colour carry
the meaning, so this is harmless), and multi-row annotation panels are single HTML-table nodes in
some scripts — deterministic in order, but they cannot be edge endpoints.
