"""06 — Benchmark harness and the 700 RPS methodology.

Sources: docs/design.md §6.3, §6.4, §8.1–§8.5 and docs/tasks.md T2.1–T2.3, T7.1.
Run from the repo root:  python3 docs/arch-diagram/src/06_benchmark.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from diagrams import Diagram, Cluster, Edge
from diagrams.aws.ml import Bedrock
from diagrams.aws.network import CloudFront, APIGateway
from diagrams.aws.compute import Lambda, Fargate
from diagrams.aws.management import Cloudwatch
from diagrams.programming.language import Python
from diagrams.generic.blank import Blank

from _style import (GRAPH_ATTR, NODE_ATTR, EDGE_ATTR, cluster, title,
                    PRIMARY, FALLBACK, PATH, GOOD, BAD, GATED, MUTED, OUT)


def box(label, ink=PATH, fill="white"):
    """A compact text box.

    Blank's transparent icon is 256 px square, which reserves a ~2.7 in node and
    leaves big empty gaps. Dropping the image and fixing the size gives a plain
    labelled box sized to its text.
    """
    lines = label.split("\n")
    w = max(1.7, 0.082 * max(len(ln) for ln in lines) + 0.30)
    h = 0.21 * len(lines) + 0.28
    return Blank(label, image="", shape="box", style="rounded,filled",
                 fillcolor=fill, color=ink, fontcolor=ink, labelloc="c",
                 fixedsize="true", width=f"{w:.2f}", height=f"{h:.2f}")


with Diagram(
    title("Benchmark Harness and Throughput Methodology",
          "design §8 · tasks T2.1–T2.3 · calls Bedrock directly · SDK retries disabled · 5 s warm-up discarded"),
    filename=f"{OUT}/06-benchmark-harness",
    outformat="png",
    show=False,
    direction="LR",
    # newrank=true is what makes the rank=same on the §8.4 cluster below take effect.
    graph_attr={**GRAPH_ATTR, "ranksep": "1.05", "nodesep": "0.45", "newrank": "true"},
    node_attr=NODE_ATTR,
    edge_attr=EDGE_ATTR,
):
    # --- the harness itself (§8.1) -----------------------------------------
    with Cluster("Harness — bench/  (offline, one laptop)", graph_attr=cluster("default")):
        corpus = box("bench/corpus/ — 8 samples\nexpected_verdict +\nshould_escalate")
        schemas = Python("backend/schemas.py\nstrict json_schema\nSHARED with the Lambda")
        driver = Python("bench/driver.py\nThreadPoolExecutor + ramp\ntotal_max_attempts=1\ndiscard 5 s warm-up")

    # --- what the harness deliberately does NOT go through (§8.1.1) --------
    with Cluster("NOT in the bench path", graph_attr=cluster("muted")):
        cdn = CloudFront("CloudFront\ndemo path only")
        api = APIGateway("API Gateway\n30 s cap · demo path only")
        fn = Lambda("Lambda email-scan\ndemo path only")

    # --- the two lanes under test ------------------------------------------
    with Cluster("PRIMARY lane", graph_attr=cluster("primary")):
        g4 = Bedrock("google.gemma-4-26b-a4b\nbedrock-mantle\n/openai/v1/chat/completions")

    with Cluster("FALLBACK lane", graph_attr=cluster("fallback")):
        g3 = Bedrock("google.gemma-3-27b-it\nbedrock-runtime\nConverseStream")

    # --- measured single-client ceilings (§6.4) -----------------------------
    with Cluster("Single-client ceilings (§6.4)", graph_attr=cluster("default")):
        ceil_m = box("MANTLE\n40 conc -> 38.1 RPS, 0 errors\n50 -> 47.9 % errors\n100 -> 81.8 % errors",
                     ink=PRIMARY, fill="#FFFBEB")
        ceil_r = box("RUNTIME\n150 conc -> 62.1 RPS, 0 errors\n300 -> 76 RPS, 2.32 % errors",
                     ink=FALLBACK, fill="#EFF6FF")

    # --- three-way error taxonomy (§8.3) — three distinct sinks -------------
    with Cluster("service_throttle", graph_attr=cluster("good")):
        b_thr = box("429 / 503 / ThrottlingException\n= a real Bedrock limit\nZERO in every test", ink=GOOD)

    with Cluster("client_transport", graph_attr=cluster("bad")):
        b_tr = box("ConnectionReset · ReadTimeout\n· NewConnectionError\n= OUR generator saturated\nthe only thing that failed",
                   ink=BAD)

    with Cluster("application", graph_attr=cluster("muted")):
        b_app = box("4xx validation ·\nschema-parse failure\n= a bug in our request", ink=PATH)

    # --- observability asymmetry (§8.5) ------------------------------------
    with Cluster("CloudWatch cross-check — FALLBACK lane only (§8.5)", graph_attr=cluster("bad")):
        cw = Cloudwatch("AWS/Bedrock by ModelId\ngemma-3-27b-it: 14 metrics\ngemma-4-26b-a4b: 0 metrics")

    # --- the licence to extrapolate (§8.4 step 1) --------------------------
    with Cluster("Licence to extrapolate", graph_attr=cluster("good")):
        flat = box("server latency load-independent\np50 2289 -> 2251 ms\np95 3249 -> 3234 ms\nas offered load rose 50 %",
                   ink=GOOD)

    # --- the five-step methodology (§8.4) ----------------------------------
    # rank=same keeps the five steps in one column, so the ordered chain reads
    # top-to-bottom instead of stretching the page five ranks wider.
    with Cluster("§8.4 — earning the 700 RPS number  (steps 1 -> 5, upward)",
                 graph_attr={**cluster("default"), "rank": "same"}):
        # declared bottom-up so graphviz orders the column 1 -> 5 downwards
        s5 = box("5 · §5.3 ramp,\nhold 15 min\nper step")
        s4 = box("4 · quota math\n58.3M of the fixed\n100M TPM")
        s3 = box("3 · Little's Law\n~1 s E2E ->\n~700 in-flight")
        s2 = box("2 · single-client\nceiling: 40 / 150")
        s1 = box("1 · latency is\nload-independent")
        # graphviz orders this rank=same column bottom-up, so the chain climbs 1 -> 5.
        s1 >> Edge(color=PATH) >> s2 >> Edge(color=PATH) >> s3 >> Edge(color=PATH) >> s4 >> Edge(color=PATH) >> s5

    with Cluster("GATED — T7.1, needs sign-off", graph_attr=cluster("gated")):
        far = Fargate("~5 Fargate tasks, 4 vCPU\n~18 workers x 40 conc\n= ~700 in-flight\n~$400")
        caveat = box("until it runs, sustained RPS is\nEXTRAPOLATED, not measured", ink=GATED)

    # --- reporting (T2.2) --------------------------------------------------
    with Cluster("Reporting — bench/report.py (T2.2)", graph_attr=cluster("default")):
        results = box("results.json\np50/p95/p99 TTFT + E2E\nachieved RPS\nescalation rate")
        report = Python("bench/report.py\nmarkdown + chart JSON")
        with Cluster("client_transport empty", graph_attr=cluster("good")):
            verdict = box("pass / fail\nverdict emitted", ink=GOOD)
        with Cluster("client_transport non-empty", graph_attr=cluster("bad")):
            refuse = box("red banner, NO verdict —\nnever quote a saturated client\nas a Bedrock limit", ink=BAD)

    # --- harness wiring ----------------------------------------------------
    corpus >> Edge(label="8 fixed requests") >> driver
    schemas >> Edge(label="measures the same request\nthe demo makes (§8.1.2)") >> driver
    schemas >> Edge(label="one schema module,\nboth callers", color=MUTED, style="dotted") >> fn

    # deliberate bypass — the harness never enters the web app
    driver >> Edge(label="BYPASSED on purpose:\nno CloudFront / API GW distortion (§8.1.1)",
                   color=MUTED, style="dotted", arrowhead="none") >> cdn
    cdn >> Edge(color=MUTED, style="dotted") >> api >> Edge(color=MUTED, style="dotted") >> fn

    # direct calls to Bedrock, one lane each
    driver >> Edge(label="direct — SigV4,\ncap concurrency 40", color=PRIMARY, penwidth="2.5") >> g4
    driver >> Edge(label="direct — boto3,\ncap concurrency 150", color=FALLBACK, penwidth="2.0",
                   style="dashed") >> g3

    # --- lanes -> measured ceilings ----------------------------------------
    g4 >> Edge(color=PRIMARY, penwidth="2.0") >> ceil_m
    g3 >> Edge(color=FALLBACK, penwidth="1.8", style="dashed") >> ceil_r

    # --- lanes -> error taxonomy -------------------------------------------
    g4 >> Edge(label="ConnectionReset\nat conc 50+", color=BAD, penwidth="2.0") >> b_tr
    g3 >> Edge(label="2.32 % at conc 300,\nall client-side", color=BAD, style="dashed") >> b_tr
    g4 >> Edge(label="InvocationThrottles = 0 —\nthe doc's 429/503 backoff\nnever fired (§6.3)",
               color=GOOD, style="dotted") >> b_thr
    g4 >> Edge(label="output parsed via\nschemas.extract_json", color=MUTED, style="dotted") >> b_app

    # --- lanes -> observability --------------------------------------------
    g3 >> Edge(label="14 metrics — independently\nvalidates our client TTFT", color=GOOD,
               style="dotted") >> cw
    g4 >> Edge(label="0 metrics — no cross-check\nfor the primary model", color=BAD,
               penwidth="2.0", style="dashed") >> cw

    # --- taxonomy -> reporting ---------------------------------------------
    b_thr >> Edge(color=GOOD, style="dotted") >> results
    b_tr >> Edge(label="the bucket that\ngates the verdict", color=BAD, penwidth="2.0") >> results
    b_app >> Edge(color=MUTED, style="dotted") >> results
    results >> Edge() >> report
    report >> Edge(label="empty", color=GOOD, penwidth="2.0") >> verdict
    report >> Edge(label="non-empty ->\nrefuse (T2.2 hard rule)", color=BAD, penwidth="2.2") >> refuse

    # --- methodology wiring ------------------------------------------------
    ceil_r >> Edge(label="conc 100 vs 150", color=FALLBACK, style="dashed") >> flat
    flat >> Edge(label="licences horizontal\nscale-out", color=GOOD, penwidth="2.2") >> s1
    ceil_m >> Edge(label="40 safe per worker", color=PRIMARY, style="dashed") >> s2
    s3 >> Edge(label="fan-out plan", color=GATED, penwidth="2.2") >> far
    s5 >> Edge(label="run the ramp on a\ndistributed generator", color=GATED, style="dashed") >> far
    far >> Edge(color=GATED, style="dotted") >> caveat
