"""01 — Overall architecture: the whole email-scan PoC on one page.

Reference implementation for the diagram set. Other scripts follow this shape.
Run from the repo root:  python3 docs/arch-diagram/src/01_overview.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from diagrams import Diagram, Cluster, Edge
from diagrams.aws.ml import Bedrock
from diagrams.aws.network import CloudFront, Route53, APIGateway
from diagrams.aws.compute import Lambda
from diagrams.aws.storage import SimpleStorageServiceS3
from diagrams.aws.security import Cognito, CertificateManager
from diagrams.aws.management import CloudwatchLogs
from diagrams.aws.database import Dynamodb
from diagrams.onprem.client import Users, Client
from diagrams.programming.language import Python

from _style import (GRAPH_ATTR, NODE_ATTR, EDGE_ATTR, cluster, title,
                    PRIMARY, FALLBACK, PATH, GOOD, GATED, MUTED, OUT)

with Diagram(
    title("Email Scan PoC — Overall Architecture",
          "account 123456789012 · us-east-1 · https://email-scan.example.com · Cognito UI + public API-key endpoint"),
    filename=f"{OUT}/01-overview",
    outformat="png",
    show=False,
    direction="LR",
    graph_attr={**GRAPH_ATTR, "ranksep": "1.3", "nodesep": "0.6"},
    node_attr=NODE_ATTR,
    edge_attr=EDGE_ATTR,
):
    demo = Users("Demo user\ndemo / <demo password>")

    with Cluster("Edge", graph_attr=cluster("aws")):
        dns = Route53("Route53\nexample.com")
        cdn = CloudFront("CloudFront\nemail-scan.example.com")
        acm = CertificateManager("ACM cert\nus-east-1, DNS")

    with Cluster("Auth", graph_attr=cluster("muted")):
        pool = Cognito("Cognito pool\n+ JWT authorizer")

    with Cluster("Application  (us-east-1)", graph_attr=cluster("aws")):
        spa = SimpleStorageServiceS3("S3 — SPA\nOAC only")
        api = APIGateway("API Gateway\nHTTP API · 30s cap")
        fn = Lambda("Lambda  email-scan\npython3.12 · 512MB · 29s\n0 pip deps · 0 layers")
        logs = CloudwatchLogs("Logs\nretention 14d")

    with Cluster("PRIMARY lane", graph_attr=cluster("primary")):
        g4 = Bedrock("google.gemma-4-26b-a4b\nbedrock-mantle\n/openai/v1/chat/completions\nTTFT p50 503ms")

    with Cluster("FALLBACK lane / A-B", graph_attr=cluster("fallback")):
        g3 = Bedrock("google.gemma-3-27b-it\nbedrock-runtime\nConverseStream\nplain base model ID")

    with Cluster("Run history", graph_attr=cluster("aws")):
        ddb = Dynamodb("DynamoDB  email-scan-runs\npk RUN / QUOTA · 7d TTL\nfull email stored")

    with Cluster("Public API caller  (anyone, anywhere)", graph_attr=cluster("gated")):
        pub = Client("POST /api/public/scan\nx-api-key\n10 rps · 5000/day")

    with Cluster("Benchmark (offline)", graph_attr=cluster("good")):
        bench = Python("bench/driver.py\nshares backend/schemas.py")

    # --- viewer path -------------------------------------------------------
    demo >> Edge(label="HTTPS") >> dns >> Edge(label="A-ALIAS") >> cdn
    acm >> Edge(style="dotted", color=MUTED, label="TLS") >> cdn

    cdn >> Edge(label="default") >> spa
    cdn >> Edge(label="/api/*\n+ x-origin-secret\ncompress=false") >> api

    demo >> Edge(label="InitiateAuth", color=MUTED, style="dashed") >> pool
    pool >> Edge(label="Bearer JWT", color=MUTED, style="dashed") >> api

    api >> Edge(label="AWS_PROXY") >> fn
    fn >> Edge(color=MUTED, style="dotted") >> logs

    # The public lane. No Cognito: the gateway route is authorization_type NONE and the key
    # is checked in the Lambda. It still enters through CloudFront, because the Lambda
    # requires the x-origin-secret that only the distribution injects.
    pub >> Edge(label="no Cognito · CORS *\nroute auth NONE", color=GATED, penwidth="2.2") >> cdn

    # Every scan is recorded, from every entry point, and the SCAN RESULTS tab reads them back.
    fn >> Edge(label="record_run  (ui | api)", color=PATH, style="dashed") >> ddb
    ddb >> Edge(label="GET /api/runs\nJWT-protected", color=MUTED, style="dotted") >> fn

    # --- model lanes -------------------------------------------------------
    fn >> Edge(label="SigV4  service=bedrock\nresponse_format json_schema",
               color=PRIMARY, penwidth="2.5") >> g4
    fn >> Edge(label="boto3 Converse\nfence-stripping parser",
               color=FALLBACK, penwidth="2.0", style="dashed") >> g3

    # --- harness bypasses the web app entirely -----------------------------
    bench >> Edge(label="direct — no CloudFront,\nno API Gateway distortion",
                  color=GOOD, penwidth="2.2") >> g4
    bench >> Edge(color=GOOD, style="dashed", penwidth="1.6") >> g3
