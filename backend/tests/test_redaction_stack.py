"""CDK template tests for the LLM CJK PII recall provisioning.

These are example/smoke tests (not Hypothesis) that synthesize `RedactionStack`
once and assert on the resulting CloudFormation template, guarding the three
things the LLM PII provisioning added to `backend/infra/redaction_stack.py` and the one
thing this feature must NOT have added:

- the AgentCore execution role grants `bedrock:InvokeModel`
  scoped to BOTH the inference-profile ARN (this account/Region) and the
  underlying foundation-model ARN, and to NO wildcard model resource.
- `LLM_PII_MODEL_ID` is passed to the AgentCore Runtime as
  an environment variable.
- the literal the stack ships satisfies the runtime tool's
  `llm_pii_detect._is_pinned_model_id`, and the value on the Runtime equals
  the stack constant, so stack and runtime agree by construction.
- the feature required no frontend interaction: the
  synthesized HTTP API still carries exactly its 8 pre-existing routes and
  none of them references the model id, a CDK-level proxy for "no new
  frontend request field or control was introduced".

The full-synth approach is used (`aws_cdk.assertions.Template.from_stack`)
with an explicit `env` so the `{region}`/`{account}` interpolations in the
InvokeModel ARNs resolve to concrete literal values rather than CDK tokens.
Synth stages `agent_bundle/package_clean` as an asset, so the stack is built
with the working directory pinned to the backend root (where `app.py` and
that bundle live); the synth is done once per module because it is not cheap.
"""
import os
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# The stack constants are the source of truth for the ARNs and env-var value
# these tests assert on: importing them (rather than re-typing the strings)
# turns each assertion into a drift guard against `redaction_stack.py`.
from infra.redaction_stack import (  # noqa: E402
    IMAGE_OCR_FOUNDATION_MODEL,
    IMAGE_OCR_MODEL_ID,
    LLM_PII_FOUNDATION_MODEL,
    LLM_PII_MODEL_ID,
)
from agent.tools.llm_pii_detect import _is_pinned_model_id  # noqa: E402
from agent.tools.ocr import _is_pinned_model_id as _ocr_is_pinned_model_id  # noqa: E402

# Concrete account/Region so the InvokeModel ARNs synthesize to literals.
TEST_ACCOUNT = "123456789012"
TEST_REGION = "us-west-2"

# The two ARNs the InvokeModel grant must carry, rebuilt from the stack's own
# Constants and the concrete test env.
EXPECTED_INFERENCE_PROFILE_ARN = (
    f"arn:aws:bedrock:{TEST_REGION}:{TEST_ACCOUNT}"
    f":inference-profile/{LLM_PII_MODEL_ID}"
)
EXPECTED_FOUNDATION_MODEL_ARN = (
    f"arn:aws:bedrock:*::foundation-model/{LLM_PII_FOUNDATION_MODEL}"
)

# The two ARNs the image-OCR InvokeModel grant must carry, rebuilt from the
# stack's own image-OCR constants and the concrete test env.
# NOTE: `IMAGE_OCR_MODEL_ID` is now retargeted
# to a Sonnet-class id and is therefore DIFFERENT from the Haiku
# `LLM_PII_MODEL_ID`. Previously (in the llm-image-ocr feature) the two were
# equal Haiku ids, so CDK deduped the two identical `bedrock:InvokeModel`
# PolicyStatements into one; now they are distinct, so the synthesized grant
# set must cover BOTH the Sonnet (image-OCR) ARNs AND the Haiku (LLM-PII) ARNs
# (whether CDK emits them as two statements or one combined resource list).
# These tests assert the grant *covers* the Sonnet ARNs — they do not assume
# any dedup with the LLM-PII grant.
EXPECTED_IMAGE_OCR_INFERENCE_PROFILE_ARN = (
    f"arn:aws:bedrock:{TEST_REGION}:{TEST_ACCOUNT}"
    f":inference-profile/{IMAGE_OCR_MODEL_ID}"
)
EXPECTED_IMAGE_OCR_FOUNDATION_MODEL_ARN = (
    f"arn:aws:bedrock:*::foundation-model/{IMAGE_OCR_FOUNDATION_MODEL}"
)

# Route table declared in `redaction_stack.py` ("API Design"): 8
# pre-existing routes + 2 detection-review-mode routes
# (GET /jobs/{jobId}/detections, POST /jobs/{jobId}/redact).
EXPECTED_ROUTE_COUNT = 15

RUNTIME_RESOURCE_TYPE = "AWS::BedrockAgentCore::Runtime"


@pytest.fixture(scope="module")
def template_json():
    """Synthesize `RedactionStack` once and hand back the template as a dict.

    The synth zips a ~100 MB code-asset bundle, so it is done a single time
    for the whole module. The working directory is pinned to `BACKEND_ROOT`
    for the duration of the synth because the stack references its assets by
    paths relative to the backend root (`lambdas`, `agent_bundle/...`); the
    original CWD is restored afterward so no other test module is affected.
    """
    import aws_cdk as cdk
    from aws_cdk.assertions import Template

    from infra.redaction_stack import RedactionStack

    original_cwd = os.getcwd()
    os.chdir(BACKEND_ROOT)
    try:
        app = cdk.App()
        stack = RedactionStack(
            app,
            "TestStack",
            admin_email="first-admin@example.com",
            app_url="https://app.example.com",
            env=cdk.Environment(account=TEST_ACCOUNT, region=TEST_REGION),
        )
        template = Template.from_stack(stack)
        return template.to_json()
    finally:
        os.chdir(original_cwd)


def _all_policy_statements(template):
    """Every IAM policy Statement anywhere in the synthesized template.

    Walks the template generically for any object carrying a list-valued
    ``Statement`` key (inline role policies, standalone ``AWS::IAM::Policy``
    documents, etc.), so the InvokeModel grant is found regardless of which
    construct emitted it or how CDK's policy minimizer packaged it.
    """
    statements = []

    def visit(node):
        if isinstance(node, dict):
            stmt = node.get("Statement")
            if isinstance(stmt, list):
                statements.extend(s for s in stmt if isinstance(s, dict))
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(template.get("Resources", {}))
    return statements


def _as_list(value):
    """Normalize a CFN scalar-or-list field (Action/Resource) to a list."""
    if isinstance(value, list):
        return value
    return [value]


def _invoke_model_statements(template):
    """All policy statements whose Action set includes ``bedrock:InvokeModel``."""
    result = []
    for stmt in _all_policy_statements(template):
        actions = _as_list(stmt.get("Action"))
        if "bedrock:InvokeModel" in actions:
            result.append(stmt)
    return result


def _resource_count(template, resource_type):
    return sum(
        1
        for resource in template.get("Resources", {}).values()
        if resource.get("Type") == resource_type
    )


def _runtime_properties(template):
    for resource in template.get("Resources", {}).values():
        if resource.get("Type") == RUNTIME_RESOURCE_TYPE:
            return resource.get("Properties", {})
    raise AssertionError(f"no {RUNTIME_RESOURCE_TYPE} resource in template")


# --- the scoped InvokeModel grant --------------------------


def test_invoke_model_grant_targets_both_expected_arns(template_json):
    """The execution role grants `bedrock:InvokeModel` on BOTH the
    inference-profile ARN and the underlying foundation-model ARN.
    A Converse call through a cross-Region inference
    profile authorizes against both, so both are required."""
    invoke_statements = _invoke_model_statements(template_json)
    assert invoke_statements, "no bedrock:InvokeModel statement was synthesized"

    granted_resources = set()
    for stmt in invoke_statements:
        granted_resources.update(
            r for r in _as_list(stmt.get("Resource")) if isinstance(r, str)
        )

    assert EXPECTED_INFERENCE_PROFILE_ARN in granted_resources
    assert EXPECTED_FOUNDATION_MODEL_ARN in granted_resources


def test_invoke_model_grant_has_no_wildcard_model_resource(template_json):
    """No `bedrock:InvokeModel` statement is scoped to a wildcard model
    resource — not `"*"`, not `foundation-model/*`, not `inference-profile/*`
    (grant no broader Bedrock model permission)."""
    invoke_statements = _invoke_model_statements(template_json)
    assert invoke_statements, "no bedrock:InvokeModel statement was synthesized"

    for stmt in invoke_statements:
        for resource in _as_list(stmt.get("Resource")):
            assert resource != "*", "InvokeModel must not be granted on '*'"
            if isinstance(resource, str):
                assert not resource.endswith("foundation-model/*"), resource
                assert not resource.endswith("inference-profile/*"), resource
                assert not resource.endswith(":*"), resource


# --- the model-id environment variable ---------------------


def test_runtime_environment_carries_model_id(template_json):
    """`LLM_PII_MODEL_ID` is present in the AgentCore Runtime's environment
    variables with the pinned value."""
    env_vars = _runtime_properties(template_json).get("EnvironmentVariables", {})

    assert "LLM_PII_MODEL_ID" in env_vars
    assert env_vars["LLM_PII_MODEL_ID"] == LLM_PII_MODEL_ID


# --- stack literal and runtime tool agree ------------------


def test_stack_model_id_is_pinned_per_runtime_predicate():
    """The literal the stack ships satisfies the runtime tool's pinned-id
    predicate. Needs no synth — it ties the CDK constant to
    `llm_pii_detect._is_pinned_model_id`, so the stack cannot ship a floating
    alias the runtime would reject as dormant."""
    assert _is_pinned_model_id(LLM_PII_MODEL_ID) is True
    assert _is_pinned_model_id(LLM_PII_FOUNDATION_MODEL) is True


def test_runtime_env_value_equals_stack_constant(template_json):
    """The value synthesized onto the Runtime equals the stack constant and
    is itself pinned, so stack and runtime agree by construction
    ."""
    env_vars = _runtime_properties(template_json).get("EnvironmentVariables", {})

    assert env_vars.get("LLM_PII_MODEL_ID") == LLM_PII_MODEL_ID
    assert _is_pinned_model_id(env_vars.get("LLM_PII_MODEL_ID")) is True
    # The grant's inference-profile ARN embeds the same pinned id.
    assert EXPECTED_INFERENCE_PROFILE_ARN.endswith(LLM_PII_MODEL_ID)


# --- no new frontend request field or control --------------


def test_no_new_api_route_was_introduced(template_json):
    """The synthesized HTTP API carries exactly the expected number of routes:
    8 original routes + 2 detection-review-mode routes
    (GET /jobs/{jobId}/detections, POST /jobs/{jobId}/redact) + 5 user
    management routes (GET/POST /users, PATCH/DELETE /users/{username},
    POST /users/{username}/resend-invite)."""
    assert _resource_count(template_json, "AWS::ApiGatewayV2::Route") == (
        EXPECTED_ROUTE_COUNT
    )


def test_no_api_route_references_the_model_id(template_json):
    """No API Gateway route or integration references the model id.
    Modest guard: the model id is an internal runtime
    environment variable, never a request parameter the frontend supplies."""
    for resource in template_json.get("Resources", {}).values():
        if resource.get("Type", "").startswith("AWS::ApiGatewayV2::"):
            rendered = repr(resource.get("Properties", {}))
            assert "LLM_PII_MODEL_ID" not in rendered
            assert LLM_PII_MODEL_ID not in rendered


# --- Multimodal LLM embedded-image OCR provisioning ---------
#
# These smoke tests guard what the image-OCR provisioning added to
# `backend/infra/redaction_stack.py` for the image-OCR path, mirroring the
# LLM-PII assertions above:
#
# They were reconciled for the in-image-redaction Sonnet retarget:
# `IMAGE_OCR_MODEL_ID` now names a Sonnet-class id, distinct from the Haiku
# `LLM_PII_MODEL_ID`, so the equality/dedup assumption from llm-image-ocr no
# longer holds.
#
# - the AgentCore execution role grants
#   `bedrock:InvokeModel` on the image-OCR (Sonnet) inference-profile ARN (this
#   account/Region) and the underlying foundation-model ARN, and on NO wildcard
#   model resource. Because `IMAGE_OCR_MODEL_ID` (Sonnet) now DIFFERS from
#   `LLM_PII_MODEL_ID` (Haiku), CDK no longer dedupes the two into one
#   statement: the synthesized grant set must cover the Sonnet ARNs AND the
#   Haiku ARNs. These tests assert the grant *covers* the Sonnet ARNs (via a
#   union over every InvokeModel statement), which is robust to whether CDK
#   packages them as one combined statement or two. (The no-wildcard guarantee
#   is covered globally by `test_invoke_model_grant_has_no_wildcard_model_resource`,
#   which checks every InvokeModel statement in the template.)
# - `IMAGE_OCR_MODEL_ID` is passed to the AgentCore
#   Runtime as an environment variable with the pinned Sonnet value.
# - the (Sonnet) literal the stack ships satisfies the
#   image-OCR runtime tool's `ocr._is_pinned_model_id`, and the value on the
#   Runtime equals the stack constant, so stack and runtime agree by
#   construction.


# --- the scoped image-OCR InvokeModel grant ----------------


def test_image_ocr_invoke_model_grant_targets_both_expected_arns(template_json):
    """The execution role grants `bedrock:InvokeModel` on BOTH the image-OCR
    (Sonnet) inference-profile ARN and its underlying foundation-model ARN.
    A Converse call through a cross-Region
    inference profile authorizes against both, so both are required.

    Post-Sonnet-retarget, `IMAGE_OCR_MODEL_ID` (Sonnet) DIFFERS
    from `LLM_PII_MODEL_ID` (Haiku), so CDK emits the Sonnet ARNs alongside the
    Haiku ARNs rather than deduping them into one. This asserts coverage of the
    Sonnet ARNs across the union of all InvokeModel statements, so it holds
    whether CDK packages them as one combined statement or two distinct
    ones."""
    invoke_statements = _invoke_model_statements(template_json)
    assert invoke_statements, "no bedrock:InvokeModel statement was synthesized"

    granted_resources = set()
    for stmt in invoke_statements:
        granted_resources.update(
            r for r in _as_list(stmt.get("Resource")) if isinstance(r, str)
        )

    assert EXPECTED_IMAGE_OCR_INFERENCE_PROFILE_ARN in granted_resources
    assert EXPECTED_IMAGE_OCR_FOUNDATION_MODEL_ARN in granted_resources
    # Sonnet retarget: the image-OCR ARNs must be present *in addition to* the
    # Haiku LLM-PII ARNs — the grant set covers both distinct model families,
    # with no dedup/equality between them.
    assert EXPECTED_INFERENCE_PROFILE_ARN in granted_resources
    assert EXPECTED_FOUNDATION_MODEL_ARN in granted_resources
    assert EXPECTED_IMAGE_OCR_INFERENCE_PROFILE_ARN != EXPECTED_INFERENCE_PROFILE_ARN
    assert EXPECTED_IMAGE_OCR_FOUNDATION_MODEL_ARN != EXPECTED_FOUNDATION_MODEL_ARN


# --- the image-OCR model-id environment variable -----------


def test_runtime_environment_carries_image_ocr_model_id(template_json):
    """`IMAGE_OCR_MODEL_ID` is present in the AgentCore Runtime's environment
    variables with the pinned value."""
    env_vars = _runtime_properties(template_json).get("EnvironmentVariables", {})

    assert "IMAGE_OCR_MODEL_ID" in env_vars
    assert env_vars["IMAGE_OCR_MODEL_ID"] == IMAGE_OCR_MODEL_ID


# --- stack literal and image-OCR runtime tool agree --------


def test_stack_image_ocr_model_id_is_pinned_per_runtime_predicate():
    """The image-OCR literal the stack ships satisfies the runtime tool's
    pinned-id predicate. Needs no synth — it ties the
    CDK constant to `ocr._is_pinned_model_id`, so the stack cannot ship a
    floating alias the image-OCR tool would reject as dormant."""
    assert _ocr_is_pinned_model_id(IMAGE_OCR_MODEL_ID) is True
    assert _ocr_is_pinned_model_id(IMAGE_OCR_FOUNDATION_MODEL) is True


def test_stack_image_ocr_model_id_is_sonnet_class_and_distinct_from_haiku():
    """The retargeted image-OCR id is a Sonnet-class model, distinct from the
    Haiku `LLM_PII_MODEL_ID`. The Sonnet
    retarget is what makes the localization geometry compliance-grade, and the
    two model families must be configured independently — so the image-OCR id
    is asserted to be Sonnet-class and to differ from the LLM-PII (Haiku) id.
    The pinned/versioned shape itself is covered by the predicate test above."""
    assert "claude-sonnet" in IMAGE_OCR_MODEL_ID
    assert "claude-sonnet" in IMAGE_OCR_FOUNDATION_MODEL
    # The retarget made the image-OCR model distinct from the LLM-PII (Haiku)
    # model; they are no longer the equal ids that CDK used to dedup.
    assert IMAGE_OCR_MODEL_ID != LLM_PII_MODEL_ID
    assert IMAGE_OCR_FOUNDATION_MODEL != LLM_PII_FOUNDATION_MODEL
    assert "claude-haiku" in LLM_PII_MODEL_ID


def test_runtime_env_image_ocr_value_equals_stack_constant(template_json):
    """The image-OCR value synthesized onto the Runtime equals the stack
    constant and is itself pinned per the image-OCR tool's own predicate, so
    stack and runtime agree by construction."""
    env_vars = _runtime_properties(template_json).get("EnvironmentVariables", {})

    assert env_vars.get("IMAGE_OCR_MODEL_ID") == IMAGE_OCR_MODEL_ID
    assert _ocr_is_pinned_model_id(env_vars.get("IMAGE_OCR_MODEL_ID")) is True
    # The grant's inference-profile ARN embeds the same pinned id.
    assert EXPECTED_IMAGE_OCR_INFERENCE_PROFILE_ARN.endswith(IMAGE_OCR_MODEL_ID)


# ---------------------------------------------------------------------------
# Data retention: documents and job records must not live forever.
# ---------------------------------------------------------------------------
def test_document_buckets_expire_objects(template_json):
    from infra.redaction_stack import DOCUMENT_RETENTION_DAYS

    buckets = [
        r for r in template_json["Resources"].values()
        if r["Type"] == "AWS::S3::Bucket"
        and "LifecycleConfiguration" in r.get("Properties", {})
    ]
    assert len(buckets) == 2
    for bucket in buckets:
        rules = bucket["Properties"]["LifecycleConfiguration"]["Rules"]
        expire = [r for r in rules if r.get("Status") == "Enabled"
                  and r.get("ExpirationInDays") == DOCUMENT_RETENTION_DAYS]
        assert expire


def test_jobs_table_has_ttl(template_json):
    tables = [
        r for r in template_json["Resources"].values()
        if r["Type"] == "AWS::DynamoDB::Table"
        and r["Properties"].get("TableName") == "RedactionJobs"
    ]
    assert tables[0]["Properties"]["TimeToLiveSpecification"] == {
        "AttributeName": "expiresAt", "Enabled": True,
    }


def test_every_log_group_expires_after_one_day(template_json):
    """Each Lambda function (app handlers and CDK custom-resource
    functions) and the AgentCore runtime log group get a 1-day retention
    that is removed with the stack. The LogRetention function is the only
    exception: it sets 1 day on its own log group."""
    resources = template_json["Resources"]
    retentions = [r["Properties"] for r in resources.values() if r["Type"] == "Custom::LogRetention"]
    assert retentions
    for props in retentions:
        assert props["RetentionInDays"] == 1
        assert props["RemovalPolicy"] == "destroy"

    covered = set()
    agent_groups = 0
    for props in retentions:
        parts = props["LogGroupName"]["Fn::Join"][1]
        if parts[0] == "/aws/lambda/":
            covered.add(parts[1]["Ref"])
        elif parts[0] == "/aws/bedrock-agentcore/runtimes/":
            agent_groups += 1
    functions = {
        logical_id for logical_id, r in resources.items()
        if r["Type"] == "AWS::Lambda::Function" and not logical_id.startswith("LogRetention")
    }
    assert functions and covered == functions
    assert agent_groups == 1
    assert len(retentions) == len(functions) + 1


def test_agent_delete_permission_is_scoped(template_json):
    import json as _json

    policies = [
        r for r in template_json["Resources"].values()
        if r["Type"] == "AWS::IAM::Role"
        and "AgentRedactionPolicy" in r["Properties"].get("Policies", [{}])[0].get("PolicyName", "")
    ]
    statements = policies[0]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    delete = [s for s in statements if "s3:DeleteObject" in _json.dumps(s["Action"])]
    assert len(delete) == 1
    resources = _json.dumps(delete[0]["Resource"])
    assert "SourceDocumentsBucket" in resources
    # On the output bucket only detections.json may be deleted, never the
    # redacted documents themselves.
    assert "OutputDocumentsBucket" in resources
    assert "/*/detections.json" in resources
    assert '"/*"' not in resources.split("OutputDocumentsBucket", 1)[1].split("]", 1)[0]


# ---------------------------------------------------------------------------
# API access control
# ---------------------------------------------------------------------------
def _resources(template_json, type_name):
    return [r for r in template_json["Resources"].values() if r["Type"] == type_name]


def test_every_api_route_requires_cognito_jwt(template_json):
    routes = _resources(template_json, "AWS::ApiGatewayV2::Route")
    assert len(routes) == EXPECTED_ROUTE_COUNT
    authorizers = _resources(template_json, "AWS::ApiGatewayV2::Authorizer")
    assert len(authorizers) == 1
    assert authorizers[0]["Properties"]["AuthorizerType"] == "JWT"
    for route in routes:
        props = route["Properties"]
        assert not props["RouteKey"].startswith("OPTIONS")
        assert props["AuthorizationType"] == "JWT", props["RouteKey"]
        assert "AuthorizerId" in props


def test_api_stage_is_throttled(template_json):
    from infra.redaction_stack import API_THROTTLE_BURST_LIMIT, API_THROTTLE_RATE_LIMIT

    stages = _resources(template_json, "AWS::ApiGatewayV2::Stage")
    settings = stages[0]["Properties"]["DefaultRouteSettings"]
    assert settings["ThrottlingRateLimit"] == API_THROTTLE_RATE_LIMIT
    assert settings["ThrottlingBurstLimit"] == API_THROTTLE_BURST_LIMIT


def test_user_pool_is_invite_only_with_admin_group(template_json):
    from infra.redaction_stack import ADMIN_GROUP_NAME

    sys.path.insert(0, str(BACKEND_ROOT / "lambdas"))
    import request_auth

    pools = _resources(template_json, "AWS::Cognito::UserPool")
    assert len(pools) == 1
    assert pools[0]["Properties"]["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True
    groups = _resources(template_json, "AWS::Cognito::UserPoolGroup")
    assert [g["Properties"]["GroupName"] for g in groups] == [ADMIN_GROUP_NAME]
    assert request_auth.ADMIN_GROUP == ADMIN_GROUP_NAME
    clients = _resources(template_json, "AWS::Cognito::UserPoolClient")
    assert clients[0]["Properties"].get("GenerateSecret") in (False, None)


def test_first_admin_is_created_on_deploy(template_json):
    resources = template_json["Resources"].values()
    bootstrap = [r for r in resources if r["Type"] == "AWS::CloudFormation::CustomResource"
                 and "Email" in r.get("Properties", {})]
    assert len(bootstrap) == 1
    assert bootstrap[0]["Properties"]["Email"] == "first-admin@example.com"


def test_invitation_email_has_placeholders_and_app_url(template_json):
    pool = _resources(template_json, "AWS::Cognito::UserPool")[0]["Properties"]
    invite = pool["AdminCreateUserConfig"]["InviteMessageTemplate"]
    assert "{username}" in invite["EmailMessage"] and "{####}" in invite["EmailMessage"]
    assert "https://app.example.com" in invite["EmailMessage"]


def test_user_management_permissions_are_scoped_to_the_pool(template_json):
    import json as _json

    cognito_statements = []
    for policy in _resources(template_json, "AWS::IAM::Policy"):
        for st in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
            if any(a.startswith("cognito-idp:") for a in actions):
                cognito_statements.append(st)
    assert len(cognito_statements) == 2  # users API and first-admin bootstrap
    for st in cognito_statements:
        assert st["Resource"] != "*"
        assert "RedactionUserPool" in _json.dumps(st["Resource"])


def test_missing_admin_email_is_rejected():
    from infra.redaction_stack import validate_admin_email

    for bad in (None, "", "  ", "not-an-email", "a@b"):
        with pytest.raises(ValueError):
            validate_admin_email(bad)
    assert validate_admin_email(" Admin@Example.COM ") == "admin@example.com"
