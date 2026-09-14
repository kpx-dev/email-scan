"""Shared visual conventions for the email-scan architecture diagrams.

Every diagram script imports from here so the set reads as one system: same fonts,
same spacing, same colour semantics. If you change a colour, change it here only.

Colour semantics — consistent across ALL diagrams:
    PRIMARY   amber   the Gemma 4 / bedrock-mantle lane (the target model)
    FALLBACK  blue    the Gemma 3 / bedrock-runtime lane (A-B comparison, risk hedge)
    PATH      slate   ordinary request/data flow
    GOOD      green   verified working, or a target that is met
    BAD       red     verified broken, unsupported, or blocked
    GATED     violet  conditional / requires a decision or sign-off
    MUTED     grey    context, annotations, things deliberately not built
"""

PRIMARY = "#D97706"   # amber-600
FALLBACK = "#2563EB"  # blue-600
PATH = "#475569"      # slate-600
GOOD = "#059669"      # emerald-600
BAD = "#DC2626"       # red-600
GATED = "#7C3AED"     # violet-600
MUTED = "#94A3B8"     # slate-400

FONT = "Helvetica"

INK = "#0F172A"  # slate-900, for body text

# Base graph attributes. Individual diagrams override `direction` and may add
# `nodesep`/`ranksep` when a layout needs more air.
#
# NOTE: do NOT set splines=ortho. Graphviz places edge labels badly under ortho
# (they float into unrelated clusters). polyline keeps the boxy look and labels land.
GRAPH_ATTR = {
    "fontname": FONT,
    "fontsize": "22",
    "labelloc": "t",
    "bgcolor": "white",
    "pad": "0.5",
    "nodesep": "0.45",
    "ranksep": "0.9",
    "splines": "polyline",
    "compound": "true",
}

NODE_ATTR = {
    "fontname": FONT,
    "fontsize": "11",
    "fontcolor": INK,
}

EDGE_ATTR = {
    "fontname": FONT,
    "fontsize": "10",
    "color": PATH,
    "fontcolor": PATH,
}


# Cluster styling by role. Use these so boundary boxes mean the same thing everywhere.
#
# NOTE: for Graphviz clusters, `style=filled` fills from `color` (the pen), NOT from
# `bgcolor` — which silently produces dark boxes with unreadable labels. Use
# style="rounded" and let `bgcolor` do the fill.
def cluster(kind="default"):
    base = {
        "fontname": FONT,
        "fontsize": "14",
        "style": "rounded",
        "penwidth": "2.0",
        "margin": "16",
        "labeljust": "l",
    }
    fills = {
        "default": ("#F8FAFC", PATH),
        "aws": ("#F6F8FA", PATH),
        "primary": ("#FEF3C7", PRIMARY),    # amber-100
        "fallback": ("#DBEAFE", FALLBACK),  # blue-100
        "good": ("#D1FAE5", GOOD),
        "bad": ("#FEE2E2", BAD),
        "gated": ("#EDE9FE", GATED),
        "muted": ("#F1F5F9", "#64748B"),
    }
    fill, pen = fills.get(kind, fills["default"])
    return {**base, "bgcolor": fill, "color": pen, "fontcolor": pen}


def title(main, sub=None):
    """Graphviz HTML-ish label: bold title with an optional smaller subtitle."""
    if not sub:
        return main
    return f"<<b>{main}</b><br/><font point-size='12' color='{PATH}'>{sub}</font>>"


OUT = "docs/arch-diagram"
