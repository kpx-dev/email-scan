"""07 — Latency budget and the timeout chain.

Two things on one page:
  A. where latency is measured (t0 / t_first / t_end, bedrockLatencyMs, overheadMs)
  B. the nested timeout chain, innermost limit first

Facts: design.md §2.2, §5.1–5.4, §7.6, §3.3 · tasks.md T1.3, T1.4, T3.5, T3.6, T3.8, T5.2, T6.1.
Run from the repo root:  python3 docs/arch-diagram/src/07_latency.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from diagrams import Diagram, Cluster, Edge
from diagrams.aws.ml import Bedrock
from diagrams.aws.network import CloudFront, APIGateway
from diagrams.aws.compute import Lambda
from diagrams.generic.blank import Blank

from _style import (GRAPH_ATTR, NODE_ATTR, EDGE_ATTR, cluster, title,
                    PRIMARY, FALLBACK, PATH, GOOD, BAD, GATED, MUTED, OUT)


def box(label, colour=PATH):
    """A plain outlined box (no AWS icon), pen and text in one semantic colour.

    The Diagram-level node defaults are icon-shaped (fixedsize, labelloc=b), so every
    one of them has to be turned off here or the text spills outside the frame.
    """
    return Blank(
        label,
        image="",
        shape="box",
        style="rounded",
        color=colour,
        fontcolor=colour,
        penwidth="1.6",
        fixedsize="false",
        labelloc="c",
        margin="0.20,0.14",
        width="1.8",
        height="0.5",
    )


with Diagram(
    title("Latency Budget and the Timeout Chain",
          "measured warm: TTFT p50 503ms · E2E p50 978ms · client overhead ~68ms in-region "
          "— both doc §2.2 targets met"),
    filename=f"{OUT}/07-latency-timeouts",
    outformat="png",
    show=False,
    direction="TB",
    graph_attr={**GRAPH_ATTR, "ranksep": "1.0", "nodesep": "0.5"},
    node_attr=NODE_ATTR,
    edge_attr=EDGE_ATTR,
):

    # =====================================================================
    # A — WHERE LATENCY IS MEASURED
    # =====================================================================
    with Cluster("A · Where latency is measured — instrumentation ported from bedrock_client "
                 "(T1.4), same timings contract on both lanes (T1.3)",
                 graph_attr=cluster("aws")):

        t0 = box("t0\nrequest issued", PATH)
        contract = box("one timings contract, both lanes\n{ttftMs, e2eMs, bedrockLatencyMs,\noverheadMs, otps}", MUTED)
        t0 >> Edge(color=MUTED, style="dotted", constraint="false", label="returns") >> contract

        with Cluster("PRIMARY lane", graph_attr=cluster("primary")):
            p_stream = Bedrock("Gemma 4 · bedrock-mantle\nSSE data: lines\nchoices[0].delta.content")

        with Cluster("FALLBACK lane", graph_attr=cluster("fallback")):
            f_stream = Bedrock("Gemma 3 · bedrock-runtime\nConverseStream\ncontentBlockDelta.delta.text")

        t_first = box("t_first\nfirst content delta", GOOD)
        t_end = box("t_end\nstream exhausted\n(data: [DONE])", GOOD)

        t0 >> Edge(color=PRIMARY, penwidth="2.5") >> p_stream
        t0 >> Edge(color=FALLBACK, penwidth="2.0", style="dashed") >> f_stream
        p_stream >> Edge(color=PRIMARY, penwidth="2.5") >> t_first
        f_stream >> Edge(color=FALLBACK, penwidth="2.0", style="dashed") >> t_first
        t_first >> Edge(label="remaining deltas\n(per-delta timeline kept)") >> t_end

        with Cluster("Derived — tiles 1–2 (T5.2)", graph_attr=cluster("good")):
            ttft = box("TTFT = t_first − t0\np50 503ms warm\ntarget < 1s   MET", GOOD)
            e2e = box("E2E = t_end − t0\np50 978ms\ntarget 3–10s   MET", GOOD)

        t_first >> Edge(color=GOOD, penwidth="2.2") >> ttft
        t_end >> Edge(color=GOOD, penwidth="2.2") >> e2e

        with Cluster("Attribution — tiles 3–4, FALLBACK lane only",
                     graph_attr=cluster("fallback")):
            blat = box("bedrockLatencyMs\nmetadata.metrics.latencyMs\n(server-reported)", FALLBACK)
            ohead = box("overheadMs = E2E −\nbedrockLatencyMs\n~68ms warm, in-region", FALLBACK)
            blat >> Edge(color=FALLBACK) >> ohead

        with Cluster("Metric gap — tiles 3–4, PRIMARY lane (T1.3)", graph_attr=cluster("bad")):
            gap = box("no OpenAI-shape equivalent\ncheck x-amzn-* headers first;\nif none → both fields null", BAD)
            tiles = box("2 of 5 latency tiles blank\nrender \"n/a on this lane\"\nnever 0, never —", BAD)
            gap >> Edge(color=BAD) >> tiles

        t_end >> Edge(color=FALLBACK, style="dashed", label="Converse metadata") >> blat
        t_end >> Edge(color=BAD, penwidth="2.0", label="mantle: no metadata.metrics") >> gap

        # Every number above is a WARM number — cold start is the caveat on all of them.
        with Cluster("Cold start — the warm numbers only hold after warm-up (§5.2 / §7.6, T6.1)",
                     graph_attr=cluster("gated")):
            cold = box("cold TTFT ~1.6s\nvs warm ~0.4s — 4×\nlargest live-demo risk", BAD)
            smoke = box("smoke.sh asserts\nStage 1 TTFT < 1s\nexits non-zero, pre-demo", GOOD)
            cold >> Edge(label="burn off before the\nfirst user-visible scan",
                         color=GOOD, penwidth="2.2") >> smoke

        ttft >> Edge(color=BAD, style="dashed", label="first invocation") >> cold

    # =====================================================================
    # B — THE TIMEOUT CHAIN
    # =====================================================================
    with Cluster("B · The timeout chain — nested so the innermost limit fires first and yields a "
                 "readable error (T3.5)", graph_attr=cluster("aws")):

        with Cluster("CloudFront  origin_read_timeout 120s  — outermost, never binds  (T3.8)",
                     graph_attr=cluster("muted")):
            cf = CloudFront("/api/* behaviour\ncompress = false")

            with Cluster("API Gateway HTTP API  30s  HARD cap (T3.5)", graph_attr=cluster("bad")):
                agw = APIGateway("timeout_milliseconds\n= 30000\nnot raisable")

                with Cluster("Lambda timeout 29s  (T3.6, house stack was 120s)",
                             graph_attr=cluster("default")):
                    fn = Lambda("email-scan\ntwo-stage handler")

                    with Cluster("botocore read_timeout 25s  (T1.3 / T1.4)",
                                 graph_attr=cluster("good")):
                        rt = box("innermost → fires first\nreadable error instead of\na bare gateway 504", GOOD)

        cf >> Edge(label="origin") >> agw >> Edge(label="AWS_PROXY") >> fn >> Edge(label="Bedrock call") >> rt

        inverted = box("prior art read_timeout 90s\nINVERTS the chain:\nouter limit fires first", BAD)
        bare504 = box("two-stage scan > 30s\n→ bare 504 at the gateway,\nLambda keeps billing", BAD)
        headroom = box("measured Stage 1 + Stage 2\n~2.5–3.3s → room, but a cold\nstart + one slow retry eats it", GATED)

        rt >> Edge(color=BAD, style="dashed", label="90s > 30s") >> inverted
        agw >> Edge(color=BAD, penwidth="2.2", label="on breach") >> bare504
        fn >> Edge(color=GATED, style="dotted", label="budget") >> headroom

    # =====================================================================
    # Levers named by the doc, not used
    # =====================================================================
    with Cluster("Latency levers named in the requirements doc — all three verified unavailable "
                 "or useless", graph_attr=cluster("bad")):
        lever1 = box("Prompt caching  (doc §4.3)\nGemma 3: AccessDeniedException\nGemma 4: automatic, 42% hit,\nno latency separation", BAD)
        lever2 = box("performanceConfig\nlatency = optimized  (doc §4.4)\nValidationException for\nevery candidate in us-east-1", BAD)
        lever3 = box("Cross-region inference\nprofiles  (doc §3.3 / §5.5)\n0 of 75 exist for Gemma", BAD)

    with Cluster("What actually delivers the target instead (design §5.1)",
                 graph_attr=cluster("good")):
        real = box("streaming  ·  max_tokens + strict schemas\nsame-region compute  ·  connection reuse\nwarm-up", GOOD)

    # ---- panel ordering only (invisible) ---------------------------------
    # Panel B goes under panel A; the cold-start and lever panels share panel B's
    # row as a right-hand column so the page does not become one tall ribbon.
    # Panels A and B are independent components, so Graphviz sets them side by side;
    # the two footnote panels are pinned underneath them.
    smoke >> Edge(style="invis") >> lever1
    inverted >> Edge(style="invis") >> real
