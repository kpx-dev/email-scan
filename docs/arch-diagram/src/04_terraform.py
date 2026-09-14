"""04 — Terraform resource map: one cluster per .tf file, and what each file creates.

Facts come only from docs/tasks.md §0.4 (the two safety rules), §4.1 (file manifest)
and §4.2 (the resource inventory), plus T3.1–T3.13.

Run from the repo root:  python3 docs/arch-diagram/src/04_terraform.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from diagrams import Diagram, Cluster, Edge
from diagrams.aws.network import CloudFront, Route53, APIGateway
from diagrams.aws.compute import Lambda
from diagrams.aws.storage import SimpleStorageServiceS3
from diagrams.aws.security import Cognito, CertificateManager, IAMRole, IAM
from diagrams.aws.management import CloudwatchLogs
from diagrams.generic.blank import Blank

from _style import (GRAPH_ATTR, NODE_ATTR, EDGE_ATTR, cluster, title,
                    PATH, GOOD, BAD, GATED, MUTED, OUT)

# diagrams pins every node to width=1.4in with fixedsize=true, so long resource
# names collide with their neighbours. Widen locally (does not touch _style.py).
W = "3.0"


def icon(cls, label):
    """An AWS icon node carrying a Terraform address + the exact resource name.

    Do NOT pass width= here. `diagrams` scales the icon image to the node box, so
    overriding width makes the label render on top of the icon instead of beneath
    it. Default sizing puts the label below the image correctly.
    """
    return cls(label)


def note(label, h="0.4", w=W):
    """A text-only box (Terraform data source, non-AWS resource, annotation).

    image="" is load-bearing: diagrams' Blank ships a 256x256 transparent PNG that
    inflates every box to a ~1.6in square, which is what made this diagram sprawl.
    Clearing it lets the box size to its text.

    Do not add shape/fillcolor here — a drawn box with a forced height renders as a
    large empty rectangle with the text pushed to the bottom edge.
    """
    return Blank(label, image="", width=w, height=h, labelloc="c")


with Diagram(
    title("Terraform Resource Map — what gets created",
          "infra/ · 10 .tf files (7 copied + 3 new) + terraform.tfvars · "
          "plan gate T3.13: 24–25 to add, 0 to change, 0 to destroy"),
    filename=f"{OUT}/04-terraform-resources",
    outformat="png",
    show=False,
    direction="TB",
    graph_attr={**GRAPH_ATTR, "ranksep": "1.1", "nodesep": "1.0"},
    node_attr=NODE_ATTR,
    edge_attr=EDGE_ATTR,
):
    # --- the two rules that protect the pre-existing stacks (§0.4) ----------
    with Cluster("§0.4  exactly two mistakes can damage a pre-existing stack",
                 graph_attr=cluster("bad")):
        w1 = note("1 · never copy terraform.tfstate\n"
                  "cp -r carries the source state in;\n"
                  "the next apply mutates that stack", w="4.4")
        w2 = note("2 · never leave a stale domain_name\n"
                  "route53 UPSERT hijacks the existing\n"
                  "A-alias — no destroy in the plan", w="4.4")
        w3 = note("var.project NOT set in prior-art tfvars\n"
                  "it falls back to the variable default\n"
                  "MUST set project = \"email-scan\"", w="4.4")

    # --- where the HCL comes from ------------------------------------------
    with Cluster("prior art  —  an internal stack's terraform/",
                 graph_attr=cluster("muted")):
        src = note("7 *.tf · 453 lines HCL · 29 blocks\n"
                   "local terraform.tfstate 54 KB\nno remote backend")

    # --- inputs -------------------------------------------------------------
    with Cluster("terraform.tfvars — NEW (8 lines)", graph_attr=cluster("good")):
        tfv = note("project = \"email-scan\"\n"
                   "domain_name = email-scan.example.com\nbasic_auth_enabled = false")

    with Cluster("main.tf — copied (~30)", graph_attr=cluster("aws")):
        prov = note("providers: aws ~> 5.0, both kept\n+ archive + random")
        rnd = note("random_password.origin_secret\n48 chars · special = false")

    with Cluster("variables.tf — copied + extend (~60)", graph_attr=cluster("aws")):
        vproj = note("var.project\ndrives every resource name")
        vnew = note("type on all 6 · 4 new vars\n"
                    "validation: domain_name rejects\npre-existing stack hostnames")

    # --- storage ------------------------------------------------------------
    with Cluster("s3.tf — copied, cut half (~20)", graph_attr=cluster("aws")):
        bucket = icon(SimpleStorageServiceS3,
                      "aws_s3_bucket.frontend\nemail-scan-frontend-\n123456789012")
        pab = note("public_access_block.frontend\nall 4 blocks true")
        bpol = note("bucket_policy.frontend\nSid AllowCloudFrontOAC")

    with Cluster("explicitly NOT created — deleted from the copy",
                 graph_attr=cluster("muted")):
        del1 = note("aws_s3_bucket.images\n+ its public_access_block\n"
                    "+ _cors_configuration.images")
        del2 = note("the IAM s3:* statement\napply fails if it is kept")

    # --- auth ---------------------------------------------------------------
    with Cluster("cognito.tf — copied (~60)", graph_attr=cluster("aws")):
        pool = icon(Cognito, "user_pool.main\nemail-scan\ntier ESSENTIALS")
        client = icon(Cognito, "user_pool_client.main\nemail-scan-client")
        cuser = note("null_resource.cognito_user\ndemo / <demo password>\n"
                     "+ triggers · count-gated")

    # --- compute ------------------------------------------------------------
    with Cluster("lambda.tf — copied, split (~90)", graph_attr=cluster("aws")):
        role = icon(IAMRole, "aws_iam_role.lambda\nemail-scan-lambda")
        lpol = icon(IAM, "iam_role_policy.lambda\nemail-scan-lambda-policy\n"
                         "+ bedrock-mantle · − s3:*")
        latt = icon(IAM, "policy_attachment.\nlambda_basic\nAWSLambdaBasicExecutionRole")
        fn = icon(Lambda, "aws_lambda_function.api\nemail-scan\npython3.12 · 512 MB")
        zipf = note("data.archive_file.lambda\n../backend\nexcludes __pycache__")

    with Cluster("cloudwatch.tf — NEW (~15)", graph_attr=cluster("good")):
        lg = icon(CloudwatchLogs, "cloudwatch_log_group\n/aws/lambda/email-scan\nretention 14 d")

    # --- front door ---------------------------------------------------------
    with Cluster("apigateway.tf — NEW (~70), moved out of lambda.tf:79-117",
                 graph_attr=cluster("good")):
        api = icon(APIGateway, "apigatewayv2_api.api\nemail-scan-api")
        authz = icon(APIGateway, "_authorizer.jwt  NEW\nemail-scan-jwt\nadded — the copied\npattern had none")
        route = icon(APIGateway, "_route.default\n$default\nauthorization_type JWT")
        integ = icon(APIGateway, "_integration.lambda\nAWS_PROXY\ntimeout 30 000 ms")
        stage = icon(APIGateway, "_stage.default\n$default · auto_deploy")
        perm = icon(Lambda, "lambda_permission.apigw\nSid AllowAPIGateway")

    with Cluster("cloudfront.tf — copied (~150)", graph_attr=cluster("aws")):
        acm = icon(CertificateManager, "acm_certificate.main\n"
                                       "email-scan.example.com\nDNS · us-east-1")
        cval = icon(Route53, "route53_record.\ncert_validation\nCNAME · TTL 60")
        acmv = note("acm_certificate_validation.main\nvalidation gate")
        oac = note("origin_access_control.frontend\nemail-scan-oac")
        dist = icon(CloudFront, "cloudfront_distribution.main\nemail-scan.example.com\n"
                                "compress=false on /api/*\ncustom_error_response cut")
        r53 = icon(Route53, "route53_record.main\nA-ALIAS\nzone Z2FDTNDATAQYW2")

    with Cluster("cloudfront_function.tf — NEW (~50)", graph_attr=cluster("gated")):
        cff = note("cloudfront_function.basic_auth\nemail-scan-basic-auth\n"
                   "count = 0 — off by default")

    with Cluster("outputs.tf — copied + extend (~40)", graph_attr=cluster("aws")):
        outs = note("drop images_bucket\nadd cognito_issuer")

    # --- provenance and the guard rails ------------------------------------
    src >> Edge(label="copy the 7 *.tf file-by-file", color=MUTED, style="dashed") >> prov
    w3 >> Edge(label="must be set", color=BAD, style="dotted") >> tfv
    w2 >> Edge(label="stale value", color=BAD, style="dotted") >> tfv
    tfv >> Edge(label="project") >> vproj

    # var.project names bucket, Lambda, IAM role + policy, pool, client, API and OAC
    vproj >> Edge(label="every name derives\nfrom var.project", color=PATH) >> bucket

    # --- real references ----------------------------------------------------
    bucket >> Edge(color=MUTED) >> pab
    bucket >> Edge(color=MUTED) >> bpol
    bucket >> Edge(color=MUTED, style="dotted", label="never created") >> del1

    pool >> Edge(color=MUTED) >> client
    client >> Edge(color=MUTED) >> cuser

    lpol >> Edge(color=MUTED) >> role
    latt >> Edge(color=MUTED) >> role
    role >> Edge(color=MUTED) >> fn
    zipf >> Edge(label="output_base64sha256", color=MUTED) >> fn
    fn >> Edge(label="log group name\nmust match", color=GOOD, style="dotted") >> lg

    fn >> Edge(label="invoke_arn") >> integ
    fn >> Edge(color=MUTED) >> perm
    api >> Edge(color=MUTED) >> route
    api >> Edge(color=MUTED) >> stage
    route >> Edge(color=MUTED) >> integ
    authz >> Edge(label="authorizer_id", color=GOOD, penwidth="2.0") >> route
    pool >> Edge(label="jwt issuer", color=GOOD) >> authz
    client >> Edge(label="jwt audience", color=GOOD) >> authz

    bucket >> Edge(label="S3 origin — OAC only") >> dist
    oac >> Edge(color=MUTED) >> dist
    rnd >> Edge(label="custom_header\nx-origin-secret") >> dist
    rnd >> Edge(label="env ORIGIN_SECRET", style="dashed", color=MUTED) >> fn
    api >> Edge(label="api-gateway origin\norigin_read_timeout 120") >> dist
    acm >> Edge(color=MUTED) >> acmv
    cval >> Edge(color=MUTED) >> acmv
    acmv >> Edge(label="viewer_certificate", color=MUTED) >> dist
    cff >> Edge(label="function_association\non both behaviours", color=GATED,
                style="dashed") >> dist
    dist >> Edge(label="A-ALIAS") >> r53
    tfv >> Edge(label="domain_name — the UPSERT hazard", color=BAD,
                style="dashed", penwidth="2.0") >> r53
    dist >> Edge(color=MUTED, style="dotted") >> outs
