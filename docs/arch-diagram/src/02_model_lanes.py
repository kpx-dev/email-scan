"""02 — Model and transport routing: the (model, endpoint, path) rule from design §3.2.

The single most important operational fact in this PoC: model and path are MUTUALLY
EXCLUSIVE. On mantle, /openai/v1/* carries the Gemma 4 family and /v1/* carries Gemma 3,
so one wrong path segment returns a 400 that reads like a model-availability problem.

All seven combinations, their status codes, the verbatim errors, the measured latencies
and the region availability come from design §3.2 / §12.1 and tasks.md T1.1.
Run from the repo root:  python3 docs/arch-diagram/src/02_model_lanes.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from diagrams import Diagram, Cluster, Edge
from diagrams.aws.ml import Bedrock
from diagrams.aws.compute import Lambda
from diagrams.generic.blank import Blank

from _style import (GRAPH_ATTR, NODE_ATTR, EDGE_ATTR, cluster, title,
                    PRIMARY, FALLBACK, PATH, GOOD, BAD, MUTED, OUT)


def box(label, accent=PATH, width="2.4"):
    """A plain text box used for the path / route options.

    Blank ships a 256x256 transparent icon which, left in place, inflates every node
    into a giant square. image="" drops it so the box sizes to its text.
    """
    return Blank(label, image="", shape="box", style="rounded,filled", fillcolor="#FFFFFF",
                 color=accent, fontcolor=accent, penwidth="1.8",
                 fixedsize="false", labelloc="c", margin="0.18,0.11",
                 height="0.4", width=width)


def panel(rows):
    """A stack of coloured annotation rows as ONE node.

    Graphviz will not honour top-to-bottom ordering for several unconnected nodes in a
    cluster, so each panel is a single HTML-table node — deterministic order, compact.
    """
    cells = "".join(
        f'<tr><td align="left" border="1" color="{c}" bgcolor="#FFFFFF" cellpadding="7">'
        f'<font color="{c}" point-size="11">{t}</font></td></tr>'
        for c, t in rows
    )
    return Blank(f'<<table border="0" cellspacing="7" cellpadding="0">{cells}</table>>',
                 image="", shape="plaintext", fixedsize="false",
                 width="0", height="0", margin="0")


with Diagram(
    title("Model and Transport Routing  —  design §3.2",
          "model, endpoint and path are MUTUALLY EXCLUSIVE: a single wrong path segment is a 400 "
          "that reads like a model-availability problem<br/>"
          "all seven combinations verified live in account 123456789012"),
    filename=f"{OUT}/02-model-lanes",
    outformat="png",
    show=False,
    direction="LR",
    graph_attr={**GRAPH_ATTR, "ranksep": "1.7", "nodesep": "0.7"},
    node_attr=NODE_ATTR,
    edge_attr=EDGE_ATTR,
):
    # --- caller -------------------------------------------------------------
    fn = Lambda("Lambda  email-scan\nmodel_registry.py holds the\n"
                "(model, endpoint, path, region)\ntuple · validated at cold start")

    with Cluster(title("Legend"), graph_attr=cluster("muted")):
        panel([
            (GOOD, "green  —  verified 200"),
            (BAD, "red  —  verified error"),
            (PRIMARY, "amber  —  Gemma 4 lane"),
            (FALLBACK, "blue  —  Gemma 3 lane"),
            (MUTED, "grey  —  context"),
        ])

    # --- the two models -----------------------------------------------------
    with Cluster(title("PRIMARY lane", "the target model"),
                 graph_attr=cluster("primary")):
        g4 = Bedrock("google.gemma-4-26b-a4b")

    with Cluster(title("FALLBACK lane  /  A-B", "risk hedge · boto3 story"),
                 graph_attr=cluster("fallback")):
        g3 = Bedrock("google.gemma-3-27b-it")

    # --- the two endpoint hosts and their route options -----------------------
    with Cluster(title("bedrock-mantle.{region}.api.aws",
                       "/openai/v1/* carries Gemma 4 · /v1/* carries Gemma 3"),
                 graph_attr=cluster("aws")):
        m_openai = box("/openai/v1/chat/completions")
        m_v1 = box("/v1/chat/completions")

    with Cluster(title("bedrock-runtime.{region}.amazonaws.com",
                       "the only lane with CloudWatch metrics"),
                 graph_attr=cluster("aws")):
        r_openai = box("/openai/v1/chat/completions")
        r_conv = box("Converse / ConverseStream")

    # --- annotation panels (far right column) --------------------------------
    with Cluster(title("Gemma 4 region availability", "mantle /openai/v1 · per-request dropdown"),
                 graph_attr=cluster("muted")):
        regions = panel([
            (GOOD, "us-east-1 &#183; 200 &#183; 489 ms"),
            (GOOD, "eu-central-1 &#183; 200 &#183; 758 ms"),
            (BAD, "eu-west-1 &#183; 404 not_found_error"),
        ])

    with Cluster(title("Control-plane facts", "verified, and non-obvious"),
                 graph_attr=cluster("muted")):
        facts = panel([
            (GOOD, 'SigV4 signing service name is "bedrock",<br align="left"/>'
                   'NOT "bedrock-mantle" &#183; no API key<br align="left"/>'),
            (BAD, 'gemma-4 is invisible to<br align="left"/>'
                  'aws bedrock list-foundation-models<br align="left"/>'),
            (BAD, 'gemma-4 has no foundation-model ARN<br align="left"/>'
                  'so IAM cannot be resource-scoped<br align="left"/>'),
            (BAD, "ZERO Gemma inference profiles  —  0 of 75"),
        ])

    # --- registry dispatch ---------------------------------------------------
    fn >> Edge(label="mantle_client.py\nSigV4 + urllib SSE",
               color=PRIMARY, fontcolor=PRIMARY, penwidth="2.8") >> g4
    fn >> Edge(label="runtime_client.py\nboto3 ConverseStream",
               color=FALLBACK, fontcolor=FALLBACK, penwidth="2.2", style="dashed") >> g3

    # --- the seven verified combinations (design §3.2, verbatim) -------------
    # Each label is tagged with its model, so no edge in the crossing bundle is ambiguous.
    g4 >> Edge(label="gemma-4\n200  ·  424 ms",
               color=GOOD, fontcolor=GOOD, penwidth="3.0") >> m_openai
    g4 >> Edge(label='gemma-4  ·  400\n"isn\'t supported\non this route"',
               color=BAD, fontcolor=BAD, style="dashed") >> m_v1
    g4 >> Edge(label='gemma-4  ·  400\n"provided model\nidentifier is invalid"',
               color=BAD, fontcolor=BAD, style="dashed") >> r_openai
    g4 >> Edge(label="gemma-4\nValidationException\ninvalid identifier",
               color=BAD, fontcolor=BAD, style="dashed") >> r_conv

    g3 >> Edge(label="gemma-3\n200", color=GOOD, fontcolor=GOOD, penwidth="2.6") >> m_v1
    g3 >> Edge(label='gemma-3  ·  400\n"isn\'t supported\non this route"',
               color=BAD, fontcolor=BAD, style="dashed") >> m_openai
    g3 >> Edge(label="gemma-3  ·  200\nplain base ID\nno us. / eu. prefix",
               color=GOOD, fontcolor=GOOD, penwidth="2.6") >> r_conv

    # --- keep the annotation panels in the right-hand column ------------------
    m_openai >> Edge(style="invis") >> regions
    r_conv >> Edge(style="invis") >> facts
