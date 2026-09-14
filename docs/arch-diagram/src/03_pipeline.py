"""03 - Two-stage scan pipeline: classify -> escalation gate -> deep scan.

Every label traces to docs/design.md sec 4 (shape, prompts, schemas, gate, budgets),
sec 5.1 (measured TTFT), sec 6.2 (the throughput reframe), sec 9.3 (cost sensitivity)
and docs/tasks.md T1.5 / T1.6 (client cannot override the system prompt).

Run from the repo root:  python3 docs/arch-diagram/src/03_pipeline.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from diagrams import Diagram, Cluster, Edge
from diagrams.aws.ml import Bedrock
from diagrams.onprem.client import Client
from diagrams.generic.blank import Blank

from _style import (GRAPH_ATTR, NODE_ATTR, EDGE_ATTR, cluster, title, INK,
                    PRIMARY, FALLBACK, PATH, GOOD, GATED, MUTED, OUT)


def box(label, pen=PATH, ink=INK, weight="1.6"):
    """A plain rounded box that sizes to its own label.

    Blank's default icon node is a fixed 1.4 x 1.9 transparent image, which leaves a
    big empty area and drops the text to the bottom edge. Dropping the image and
    letting Graphviz size the node keeps annotation boxes tight. (style="rounded,filled"
    is safe on NODES - the dark-box hazard noted in _style.py is a cluster-only trap.)
    """
    return Blank(label, image="", shape="box", style="rounded,filled",
                 fillcolor="white", color=pen, fontcolor=ink, penwidth=weight,
                 labelloc="c", fixedsize="false", width="0.1", height="0.1",
                 imagescale="false", margin="0.18,0.10")


# Two lanes, two colours, one box: HTML label so PRIMARY amber and FALLBACK blue keep
# their meaning without spending two nodes on an annotation.
LANE_LABEL = (
    f'<<font color="{PRIMARY}"><b>PRIMARY</b> Gemma 4 / mantle &#183; '
    f'json_schema strict, 543 ms</font><br/>'
    f'<font color="{FALLBACK}"><b>FALLBACK</b> Gemma 3 / runtime &#183; '
    f'no json_schema on Converse<br/>&#8594; fence-stripping parser stays '
    f'load-bearing</font>>'
)

with Diagram(
    title("Two-Stage Scan Pipeline",
          "design sec 4 - Stage 1 classify, server-side escalation gate, "
          "conditional Stage 2 deep scan"),
    filename=f"{OUT}/03-two-stage-pipeline",
    outformat="png",
    show=False,
    direction="TB",
    graph_attr={**GRAPH_ATTR, "ranksep": "0.95", "nodesep": "0.5"},
    node_attr=NODE_ATTR,
    edge_attr=EDGE_ATTR,
):
    email = Client("email text\n2-5 KB, headers + body")

    with Cluster("STAGE 1 - CLASSIFY - runs on ALL mail (sec 4.3, 4.5)",
                 graph_attr=cluster("aws")):
        s1 = Bedrock("Stage 1 classify\nmax_tokens 200, temperature 0\n"
                     "~900 in / ~60 out tokens")
        g1 = box("system prompt ~800 tok, static\n+ INJECTION GUARD (sec 4.2):\n"
                 "email is untrusted data, not instruction", GOOD)
        s1fmt = box("response_format = json_schema, strict: true\n"
                    "verdict, confidence, signals[]")
        lane = box(LANE_LABEL, MUTED)

    with Cluster("ESCALATION GATE - decided server-side, never by the client (sec 4.4)",
                 graph_attr=cluster("gated")):
        gate = box("should_escalate(stage1, email_text, parse_ok)\n"
                   "ANY ONE condition escalates", GATED, GATED, "2.6")
        esc = box("ESCALATE\n~15 % of traffic", GATED, GATED, "2.6")

    with Cluster("SHORT-CIRCUIT (sec 4.1)", graph_attr=cluster("good")):
        done = box("DONE - benign\none model call, no Stage 2", GOOD, GOOD, "2.2")

    with Cluster("STAGE 2 - DEEP SCAN - runs on ~15 % of mail (sec 4.1, 4.5)",
                 graph_attr=cluster("gated")):
        s2 = Bedrock("Stage 2 deep scan\nmax_tokens 600, temperature 0\n"
                     "~1400 in / ~300 out tokens")
        g2 = box("system prompt + the SAME guard (sec 4.2)\n"
                 "+ URL / attachment / social-engineering\nanalysis instructions", GOOD)
        s2out = box("URL analysis, social engineering\nrecommendedAction\n"
                    "disagreesWithStage1")

    resp = box("/api/scan response\nverdicts, signals, per-stage timings,\n"
               "escalation reason string")

    with Cluster("THE REFRAME (sec 6.2) - two-stage is a THROUGHPUT strategy, not just latency",
                 graph_attr=cluster("good")):
        thr = box("at 800 RPS ingest:  Stage 1 = 800 RPS, 46.1M TPM\n"
                  "but Stage 2 = only ~120 RPS, 12.2M TPM\n"
                  "58.3M of the 100M TPM ceiling = 42 % headroom", GOOD)
        thr2 = box("so the PoC must MEASURE the real escalation rate:\n"
                   "5 % -> 30 % swings daily cost by ~50 % (sec 9.3)", GATED, GATED)

    # --- stage 1 --------------------------------------------------------------
    email >> Edge(label="POST /api/scan\nemail as a USER message only -\n"
                        "never in the system block\n"
                        "client cannot override the prompt",
                  color=PATH, penwidth="2.2") >> s1
    g1 >> Edge(color=GOOD, style="dotted", constraint="false") >> s1
    s1 >> Edge(color=MUTED, style="dotted") >> s1fmt
    s1fmt >> Edge(color=MUTED, style="dotted") >> lane

    # Park the annotations above the gate so the spine stays vertical.
    lane >> Edge(style="invis") >> gate

    s1 >> Edge(label="target < 3 s\nMEASURED E2E p50 0.98 s, TTFT p50 503 ms",
               color=GOOD, fontcolor=GOOD, penwidth="2.2") >> gate

    # --- the gate: all five conditions from sec 4.4 ---------------------------
    for cond in ("parse_failed",
                 "verdict != benign",
                 "confidence\n< 0.85",
                 "url_present\n_despite_benign",
                 "attachment_present\n_despite_benign"):
        gate >> Edge(label=cond, color=GATED, fontcolor=GATED) >> esc

    gate >> Edge(label="benign, confidence >= 0.85,\nno URL, no attachment\n"
                       "~85 % of traffic",
                 color=GOOD, fontcolor=GOOD, penwidth="2.8") >> done

    # --- stage 2 --------------------------------------------------------------
    esc >> Edge(label="second model call\n~120 RPS when ingest is 800 RPS",
                color=GATED, penwidth="2.8") >> s2
    s1 >> Edge(label="Stage 1 output threaded in as prior context\n"
                     "(user message, not system)",
               color=PATH, style="dashed") >> s2
    s2 >> Edge(style="invis") >> g2
    g2 >> Edge(color=GOOD, style="dotted", constraint="false") >> s2

    s2 >> Edge(label="target 3-10 s\nMEASURED E2E ~1.5-2.3 s",
               color=GOOD, fontcolor=GOOD) >> s2out
    s2out >> Edge(color=GATED, penwidth="2.2") >> resp
    done >> Edge(color=GOOD, penwidth="2.2") >> resp

    # --- the throughput reframe ------------------------------------------------
    resp >> Edge(style="invis") >> thr
    thr >> Edge(color=MUTED, style="dotted") >> thr2
