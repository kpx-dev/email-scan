"""05 — Auth and defence in depth: every layer a request must clear, and what each one blocks.

Facts come from docs/design.md §7.4 / §7.5 and docs/tasks.md T0.3, T3.5, T3.9, T3.10, T3.12, T4.3.
Run from the repo root:  python3 docs/arch-diagram/src/05_auth.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from diagrams import Diagram, Cluster, Edge
from diagrams.aws.network import CloudFront, APIGateway
from diagrams.aws.compute import Lambda
from diagrams.aws.storage import SimpleStorageServiceS3
from diagrams.aws.security import Cognito, IAMRole
from diagrams.onprem.client import Users
from diagrams.generic.blank import Blank

from _style import (GRAPH_ATTR, NODE_ATTR, EDGE_ATTR, cluster, title,
                    PRIMARY, FALLBACK, PATH, GOOD, BAD, GATED, MUTED, OUT)


def note(label, pen=PATH):
    """A plain text box. Blank carries no icon, so size it from the label instead
    of the 1.9in icon square (the shared node_attr sets fixedsize=true)."""
    return Blank(label, shape="box", style="rounded,filled", fillcolor="white",
                 color=pen, fontcolor=pen, penwidth="1.7", fontsize="10",
                 labelloc="c", fixedsize="false", width="0.1", height="0.1",
                 margin="0.18,0.13", image="")


with Diagram(
    title("Email Scan PoC — Auth and Defence in Depth",
          "Cognito JWT at API Gateway · x-origin-secret at the Lambda · S3 via OAC only "
          "— account 123456789012 / us-east-1"),
    filename=f"{OUT}/05-auth-security",
    outformat="png",
    show=False,
    direction="LR",
    graph_attr={**GRAPH_ATTR, "ranksep": "1.35", "nodesep": "0.5"},
    node_attr=NODE_ATTR,
    edge_attr=EDGE_ATTR,
):
    demo = Users("Browser SPA\ndemo / <demo password>")

    # --- 1 · sign-in --------------------------------------------------------
    with Cluster("1 · Sign-in", graph_attr=cluster("aws")):
        pool = Cognito("Cognito user pool\nInitiateAuth USER_PASSWORD_AUTH\nIdToken -> sessionStorage")

    # --- 2 · edge -----------------------------------------------------------
    with Cluster("2 · Edge", graph_attr=cluster("aws")):
        cdn = CloudFront("CloudFront\nemail-scan.example.com")

    # --- 6 · S3 origin ------------------------------------------------------
    with Cluster("6 · S3 origin", graph_attr=cluster("good")):
        s3 = SimpleStorageServiceS3("S3 — SPA\nSid AllowCloudFrontOAC\nCloudFront OAC only")

    # --- bypass attempt A: no token at all ---------------------------------
    with Cluster("Bypass A — no token", graph_attr=cluster("bad")):
        b_anon = note("GET /api/health via CloudFront\nno Authorization header", BAD)

    # --- 4 · API Gateway + the JWT authorizer we add -----------------------
    with Cluster("4 · API Gateway HTTP API", graph_attr=cluster("aws")):
        api = APIGateway("HTTP API\n$default route\nauthorization_type = JWT")
        with Cluster("we add this", graph_attr=cluster("good")):
            authz = note("JWT authorizer  email-scan-jwt\naudience = Cognito client id\nissuer = cognito-idp.us-east-1\n.amazonaws.com/<pool id>", GOOD)

    # --- bypass attempt B: skip CloudFront entirely ------------------------
    with Cluster("Bypass B — skip CloudFront", graph_attr=cluster("bad")):
        b_direct = note("curl the API Gateway URL\ndirect, no CloudFront", BAD)

    # --- 5 · Lambda + the fail-closed origin-secret check we add -----------
    with Cluster("5 · Lambda", graph_attr=cluster("aws")):
        fn = Lambda("Lambda  email-scan\nrawPath dispatch")
        with Cluster("we add this", graph_attr=cluster("good")):
            secret = note("hmac.compare_digest on BYTES\nx-origin-secret\nmissing ORIGIN_SECRET -> 500\nfails closed, never allows", GOOD)

    # --- IAM ---------------------------------------------------------------
    with Cluster("Lambda execution role — grant BOTH prefixes", graph_attr=cluster("gated")):
        iam_mantle = IAMRole("bedrock-mantle:\nCallWithBearerToken\nGet* · List* · CreateInference")
        iam_rt = IAMRole("bedrock: InvokeModel\nInvokeModelWithResponseStream\nConverse · ConverseStream")
        iam_note = note("minimal mantle action\nUNPROVEN — risk R1,\nspike T0.3 settles it", GATED)

    # --- documented auth fallback ------------------------------------------
    with Cluster("Documented fallback T3.12 — count 0, off by default", graph_attr=cluster("gated")):
        cffn = note("aws_cloudfront_function.basic_auth\ncloudfront-js-2.0 · viewer-request\non BOTH behaviours", GATED)
        cfnote = note("credential base64-baked into\nthe function body:\nobfuscation, not encryption", GATED)

    # --- the authorised path ------------------------------------------------
    demo >> Edge(label="1 · InitiateAuth\nUSER_PASSWORD_AUTH") >> pool
    demo >> Edge(label="2 · Authorization:\nBearer IdToken", penwidth="2.4") >> cdn
    cdn >> Edge(label="default behaviour") >> s3
    cdn >> Edge(color=GOOD, penwidth="2.4",
                label="3 · injects x-origin-secret\non the api-gateway ORIGIN only") >> api
    pool >> Edge(color=MUTED, style="dotted", label="token trust\naud + iss") >> authz
    api >> Edge(label="4 · validate JWT") >> authz
    authz >> Edge(color=GOOD, label="AWS_PROXY\npayload format 2.0") >> fn
    fn >> Edge(color=GOOD, label="5 · validate\nx-origin-secret") >> secret

    # --- IAM grants ---------------------------------------------------------
    secret >> Edge(color=PRIMARY, penwidth="2.4", label="allowed -> mantle call") >> iam_mantle
    secret >> Edge(color=FALLBACK, penwidth="2.0", style="dashed",
                   label="allowed -> runtime call") >> iam_rt
    iam_mantle >> Edge(color=GATED, style="dotted", label="R1") >> iam_note

    # --- rejections ---------------------------------------------------------
    b_anon >> Edge(color=BAD, penwidth="2.4", label="rejected 401\nJWT authorizer") >> authz
    b_direct >> Edge(color=BAD, penwidth="2.4", label="rejected 403\nno x-origin-secret") >> secret

    # --- gated fallback -----------------------------------------------------
    cffn >> Edge(color=GATED, style="dashed",
                 label="alternative to Cognito:\nflip one variable") >> cdn
    cffn >> Edge(color=GATED, style="dotted") >> cfnote
