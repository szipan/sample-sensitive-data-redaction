"""Root CDK Stack for the PII Document Redaction POC backend.

DynamoDB tables:
- `RedactionJobs`: PK `jobId`, DynamoDB Streams enabled with `NEW_IMAGE` view
  type (consumed by `JobDispatcher`).
- `RedactionRules`: PK `ruleName`.
- `RuleOperations`: PK `operationId`.

All three are AWS-managed, serverless DynamoDB tables billed on-demand
(`PAY_PER_REQUEST`): managed serverless services, no self-managed servers.

SQS FIFO queue:
- `RuleOperationsQueue`: serializes Rule Operations (create/enable/disable/
  delete requests against Redaction Rules). Producers
  (`RuleOperationSubmitHandler`) send every message with the same
  fixed `MessageGroupId` and an explicit `MessageDeduplicationId=operationId`,
  so the queue relies on that caller-supplied dedup id rather than
  content-based deduplication. The event source mapping drives the
  single-at-a-time consumer Lambda (`BatchSize=1`). A dead-letter queue is
  not required for this POC.

API Gateway HTTP API:
- Amazon API Gateway v2 `HttpApi`. HTTP APIs are HTTPS-only at the edge by
  default (there is no way to attach a plaintext HTTP listener, and no
  custom domain is configured here), so exposure is HTTPS-only and
  non-HTTPS requests are rejected by the edge with no application code
  involved.
- Every route (jobs, rules, rule operations, detection review) points at its
  real handler. The shared placeholder Lambda (`lambdas/placeholder.py`) is
  still defined but unused by any route; removing it is out of scope here.
"""
import re
from typing import Optional

from aws_cdk import CfnOutput, CfnResource, CustomResource, Duration, Fn, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_authorizers as apigwv2_authorizers
from aws_cdk import aws_apigatewayv2_integrations as apigwv2_integrations
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as lambda_event_sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sqs as sqs
from aws_cdk import custom_resources as cr
from constructs import Construct

from shared.builtin_rules import BUILT_IN_RULES

# Pinned Bedrock model identifier for the LLM CJK PII recall path
# (`agent/tools/llm_pii_detect.py`). Defined as module-level constants so
# the stack's literal and the tool's `_is_pinned_model_id` predicate can be
# checked against the same value (by the CDK template test).
#
# `LLM_PII_MODEL_ID` is the cross-Region (US geo) inference profile actually
# passed to `converse`; it ends in `-v1:0`, so it satisfies the tool's
# pinned/versioned-id rule (a floating alias would be rejected).
# `LLM_PII_FOUNDATION_MODEL` is the underlying foundation model that profile
# routes to — needed for the second IAM grant below.
# How long uploaded, redacted and detection artifacts (S3 objects and job
# records) are kept. After this the S3 lifecycle rule and the DynamoDB TTL
# remove them, so unredacted source text cannot linger indefinitely.
DOCUMENT_RETENTION_DAYS = 1

# Retention of every log group the stack controls (Lambda and AgentCore).
# Logs are PII-free by design and only used to troubleshoot recent jobs, so
# they are kept no longer than the documents themselves.
LOG_RETENTION = logs.RetentionDays.ONE_DAY

# Cognito group of administrators (manage users and rules). Everyone else
# is a regular user who uploads documents and works with their own jobs.
# Must match `lambdas/request_auth.py` ADMIN_GROUP.
ADMIN_GROUP_NAME = "admins"

# Stage-wide API throttle (requests per second, burst). Applies to all
# callers together, which is enough for a POC with a handful of users.
API_THROTTLE_RATE_LIMIT = 10
API_THROTTLE_BURST_LIMIT = 20

LLM_PII_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
LLM_PII_FOUNDATION_MODEL = "anthropic.claude-haiku-4-5-20251001-v1:0"

# Pinned Bedrock model identifier for the multimodal embedded-image OCR +
# localization path (`agent/tools/ocr.py`). Same shape as the LLM PII
# constants above so the stack's literal and the tool's `_is_pinned_model_id`
# predicate can be checked against the same value (the CDK template test).
#
# This is a Sonnet-class model: embedded-image OCR is served by Sonnet because
# it returns compliance-grade per-element bounding boxes (used for in-image
# redaction localization) where Haiku's boxes are unusable. It is deliberately
# distinct from the LLM PII source (Haiku), so the two can be retargeted
# independently by configuration alone.
#
# `IMAGE_OCR_MODEL_ID` is the cross-Region (US geo) inference profile passed
# to `converse`; it ends in `-v1:0`, so it satisfies the tool's
# pinned/versioned-id rule (a floating alias would be rejected).
# `IMAGE_OCR_FOUNDATION_MODEL` is the underlying foundation model that profile
# routes to — needed for the second IAM grant below.
IMAGE_OCR_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
IMAGE_OCR_FOUNDATION_MODEL = "anthropic.claude-sonnet-4-5-20250929-v1:0"

# AgentCore Runtime is now provisioned as a real resource (see
# `self.agent_runtime` below) using CDK's `aws_bedrockagentcore.Runtime`
# construct with direct code deployment.


def _invitation_body(app_url: Optional[str]) -> str:
    """Invitation email; Cognito fills in {username} and {####}."""
    where_zh = f"请访问 {app_url} ," if app_url else ""
    where_en = f" at {app_url}" if app_url else ""
    return (
        "您已受邀使用 PII 文档脱敏系统。<br>"
        f"{where_zh}用邮箱 {{username}} 和临时密码 {{####}} 登录,首次登录需设置新密码。"
        "临时密码 3 天内有效。<br><br>"
        "You have been invited to the PII Document Redaction system. "
        f"Sign in{where_en} with {{username}} and the temporary password {{####}}; "
        "you will be asked to set a new password. The temporary password is valid for 3 days."
    )


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_admin_email(value: Optional[str]) -> str:
    """The first administrator's email, normalized; raises ValueError."""
    email = (value or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise ValueError(
            "The first administrator's email is required: deploy with "
            "`cdk deploy -c adminEmail=you@example.com` (or set ADMIN_EMAIL)."
        )
    return email


class RedactionStack(Stack):
    """Top-level stack that will host all backend infrastructure."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        admin_email: str,
        app_url: Optional[str] = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        admin_email = validate_admin_email(admin_email)

        # `RedactionJobs` table: tracks each document redaction job from
        # creation (`processing`) through to a terminal state (`completed`
        # or `failed`). Streams (`NEW_IMAGE`) are enabled so `JobDispatcher`
        # can react to newly created jobs and invoke the
        # AgentCore Redaction Agent.
        self.jobs_table = dynamodb.Table(
            self,
            "RedactionJobsTable",
            table_name="RedactionJobs",
            partition_key=dynamodb.Attribute(
                name="jobId", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            stream=dynamodb.StreamViewType.NEW_IMAGE,
            # Job records expire together with their S3 objects.
            time_to_live_attribute="expiresAt",
            removal_policy=RemovalPolicy.DESTROY,
        )

        # `RedactionRules` table: the global collection of Redaction Rules
        # (Built-in and custom), keyed by unique, case-sensitive `ruleName`.
        self.rules_table = dynamodb.Table(
            self,
            "RedactionRulesTable",
            table_name="RedactionRules",
            partition_key=dynamodb.Attribute(
                name="ruleName", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # `SourceDocumentsBucket`: holds uploaded Supported Documents
        # (`.docx` / `.pdf`) written by `UploadHandler` and later
        # read by the AgentCore Redaction Agent's `extract_document_content`
        # tool.
        #
        # `OutputDocumentsBucket`: holds Redacted Documents produced by the
        # Agent's `store_redacted_document` tool (key format
        # `{jobId}/{fileName}`) and later read by `DownloadHandler`.
        #
        # Both buckets use POC-appropriate, non-production settings:
        # - `block_public_access=BLOCK_ALL`: no public access, ever.
        # - `encryption=S3_MANAGED` (SSE-S3): default, no extra KMS key to
        #   manage for a POC.
        # - `removal_policy=DESTROY` + `auto_delete_objects=True`: lets
        #   `cdk destroy` fully tear down the stack (including any objects)
        #   without manual bucket emptying, which is convenient for a POC
        #   but would need to be reconsidered for production (where you'd
        #   normally want `RETAIN` to avoid accidental data loss).
        self.source_documents_bucket = s3.Bucket(
            self,
            "SourceDocumentsBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireDocuments",
                    enabled=True,
                    expiration=Duration.days(DOCUMENT_RETENTION_DAYS),
                    abort_incomplete_multipart_upload_after=Duration.days(1),
                )
            ],
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        self.output_documents_bucket = s3.Bucket(
            self,
            "OutputDocumentsBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireDocuments",
                    enabled=True,
                    expiration=Duration.days(DOCUMENT_RETENTION_DAYS),
                    abort_incomplete_multipart_upload_after=Duration.days(1),
                )
            ],
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # `RuleOperationsQueue`: SQS FIFO queue used to serialize Rule
        # Operations. A single fixed MessageGroupId (e.g. "rule-ops") is used
        # by producers, so the FIFO ordering + single message group + the
        # BatchSize=1 event source mapping together guarantee that
        # only one Rule Operation is processed at a time, in submission
        # order. Producers supply an explicit MessageDeduplicationId
        # (operationId) per message, so content-based deduplication is not
        # needed here.
        self.rule_operations_queue = sqs.Queue(
            self,
            "RuleOperationsQueue",
            queue_name="RuleOperationsQueue.fifo",
            fifo=True,
            content_based_deduplication=False,
            visibility_timeout=Duration.seconds(30),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # `RuleOperations` table: tracks each requested Rule Operation
        # (create/enable/disable/delete) from submission (`queued`) through
        # to a Terminal Result (`applied` or `rejected`).
        self.rule_operations_table = dynamodb.Table(
            self,
            "RuleOperationsTable",
            table_name="RuleOperations",
            partition_key=dynamodb.Attribute(
                name="operationId", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # `RulesInitHandler`: deploy-time, idempotent Built-in
        # Rule initialization. Wraps the pure
        # `initialize_rule_collection` function around the real
        # `RedactionRules` table via `RulesRepository`. This Lambda is NOT
        # wired to any API Gateway route -- it is invoked exactly once per
        # `cdk deploy` (stack create/update) via the `RulesInitCustomResource`
        # below, not on every request.
        self.rules_init_handler = lambda_.Function(
            self,
            "RulesInitHandler",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="rules_init_handler.handler",
            code=lambda_.Code.from_asset("lambdas"),
            timeout=Duration.seconds(29),
            environment={
                "RULES_TABLE_NAME": self.rules_table.table_name,
            },
        )
        self.rules_table.grant_read_write_data(self.rules_init_handler)

        # CDK Custom Resource: triggers `RulesInitHandler` once on stack
        # create/update (and is a no-op on stack delete, per the handler's
        # own `RequestType` check), ensuring the Built-in Rules exist in
        # `RedactionRules` after every deployment without re-running on
        # every request. `Provider` wraps the handler with the
        # CloudFormation custom-resource request/response protocol.
        self.rules_init_provider = cr.Provider(
            self,
            "RulesInitProvider",
            on_event_handler=self.rules_init_handler,
        )
        self.rules_init_custom_resource = CustomResource(
            self,
            "RulesInitCustomResource",
            service_token=self.rules_init_provider.service_token,
            # Changing a Built-in Rule pattern changes this property, which
            # makes CloudFormation send an `Update`, so the init Lambda runs
            # and upgrades the stored rule. Without a changing property the
            # custom resource only ever ran on the first deploy.
            properties={
                "builtInRulePatterns": [rule["pattern"] for rule in BUILT_IN_RULES],
            },
        )
        # Ensure the custom resource (and thus the init Lambda invocation)
        # only runs once the `RedactionRules` table actually exists.
        self.rules_init_custom_resource.node.add_dependency(self.rules_table)

        # Single shared placeholder Lambda backing every route not yet
        # implemented by a real handler. Always returns a 501 "not
        # implemented" JSON response.
        self.placeholder_handler = lambda_.Function(
            self,
            "PlaceholderHandler",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="placeholder.handler",
            code=lambda_.Code.from_asset("lambdas"),
        )
        placeholder_integration = apigwv2_integrations.HttpLambdaIntegration(
            "PlaceholderIntegration", self.placeholder_handler
        )

        # `RulesListHandler`: real implementation of `GET /rules`.
        # Reads every `RedactionRule` via `RulesRepository.get_all()` and
        # returns `{ "rules": [ { "ruleName", "pattern", "enabled",
        # "isBuiltIn" }, ... ] }`. Only needs
        # read access to the rules table.
        self.rules_list_handler = lambda_.Function(
            self,
            "RulesListHandler",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="rules_list_handler.handler",
            code=lambda_.Code.from_asset("lambdas"),
            timeout=Duration.seconds(29),
            environment={
                "RULES_TABLE_NAME": self.rules_table.table_name,
            },
        )
        self.rules_table.grant_read_data(self.rules_list_handler)
        rules_list_integration = apigwv2_integrations.HttpLambdaIntegration(
            "RulesListIntegration", self.rules_list_handler
        )

        # `RuleOperationSubmitHandler`: real implementation of
        # `POST /rules`, `PATCH /rules/{ruleName}`, and
        # `DELETE /rules/{ruleName}`. Synchronously validates name/
        # pattern length (returning 400 INVALID_LENGTH without creating any
        # record or message on violation), then writes a `queued`
        # `RuleOperations` record and enqueues a single SQS FIFO message
        # (fixed `MessageGroupId`, `MessageDeduplicationId=operationId`) for
        # `RuleOperationProcessor` to pick up later, returning 202
        # + `operationId` immediately. Needs write access to the
        # `RuleOperations` table (to create the queued record) and
        # `sqs:SendMessage` on `RuleOperationsQueue`; it never touches
        # `RedactionRules` directly.
        self.rule_operation_submit_handler = lambda_.Function(
            self,
            "RuleOperationSubmitHandler",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="rule_operation_submit_handler.handler",
            code=lambda_.Code.from_asset("lambdas"),
            timeout=Duration.seconds(29),
            environment={
                "RULE_OPERATIONS_TABLE_NAME": self.rule_operations_table.table_name,
                "RULE_OPERATIONS_QUEUE_URL": self.rule_operations_queue.queue_url,
            },
        )
        self.rule_operations_table.grant_write_data(self.rule_operation_submit_handler)
        self.rule_operations_queue.grant_send_messages(self.rule_operation_submit_handler)
        rule_operation_submit_integration = apigwv2_integrations.HttpLambdaIntegration(
            "RuleOperationSubmitIntegration", self.rule_operation_submit_handler
        )

        # `RuleOperationStatusHandler`: real implementation of
        # `GET /rule-operations/{operationId}`. Reads the
        # `RuleOperations` record for the `operationId` path parameter and
        # returns its current status (`queued` | `processing` | `applied` |
        # `rejected`) and `errorReason`, or a 404 if no such operation
        # exists. Only needs read access to the `RuleOperations` table.
        self.rule_operation_status_handler = lambda_.Function(
            self,
            "RuleOperationStatusHandler",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="rule_operation_status_handler.handler",
            code=lambda_.Code.from_asset("lambdas"),
            timeout=Duration.seconds(29),
            environment={
                "RULE_OPERATIONS_TABLE_NAME": self.rule_operations_table.table_name,
            },
        )
        self.rule_operations_table.grant_read_data(self.rule_operation_status_handler)
        rule_operation_status_integration = apigwv2_integrations.HttpLambdaIntegration(
            "RuleOperationStatusIntegration", self.rule_operation_status_handler
        )

        # `RuleOperationProcessor`: SQS FIFO consumer of
        # `RuleOperationsQueue`. For each dequeued Rule
        # Operation, moves its `RuleOperations` record to `processing`,
        # validates the requested change against the current
        # `RedactionRules` collection (`shared/rule_validation.py`),
        # applies the mutation on success, and writes the Terminal
        # Result (`applied`/`rejected` + `errorReason`) back to
        # `RuleOperations`. Needs read+write on both `RedactionRules` (to
        # read the current collection and apply create/enable/disable/
        # delete mutations) and `RuleOperations` (to update status).
        #
        # Event source mapping: `batch_size=1` is set explicitly
        # below. Combined with the FIFO queue's single, fixed
        # `MessageGroupId` (all producers use "rule-ops"), this guarantees
        # only one Rule Operation is processed at a time, in submission
        # order -- the next message only becomes visible once the current
        # invocation of `RuleOperationProcessor` completes (Requirement
        # 2.4's single-at-a-time-processing semantics).
        self.rule_operation_processor = lambda_.Function(
            self,
            "RuleOperationProcessor",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="rule_operation_processor.handler",
            code=lambda_.Code.from_asset("lambdas"),
            timeout=Duration.seconds(29),
            environment={
                "RULES_TABLE_NAME": self.rules_table.table_name,
                "RULE_OPERATIONS_TABLE_NAME": self.rule_operations_table.table_name,
            },
        )
        self.rules_table.grant_read_write_data(self.rule_operation_processor)
        self.rule_operations_table.grant_read_write_data(self.rule_operation_processor)
        self.rule_operation_processor.add_event_source(
            lambda_event_sources.SqsEventSource(
                self.rule_operations_queue,
                batch_size=1,
            )
        )

        # `UploadHandler`: real implementation of `POST /jobs`.
        # Validates the uploaded file's extension, writes it to
        # `SourceDocumentsBucket`, and creates the `RedactionJobs` record
        # (`processing` on success, `failed` on a storage write failure).
        # Needs write access to the source bucket (to store uploaded
        # documents) and to the jobs table (to create the Job record);
        # no read access to either is required for this handler's logic.
        self.upload_handler = lambda_.Function(
            self,
            "UploadHandler",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="upload_handler.handler",
            code=lambda_.Code.from_asset("lambdas"),
            timeout=Duration.seconds(29),
            environment={
                "SOURCE_BUCKET_NAME": self.source_documents_bucket.bucket_name,
                "JOBS_TABLE_NAME": self.jobs_table.table_name,
                "JOB_RETENTION_DAYS": str(DOCUMENT_RETENTION_DAYS),
            },
        )
        self.source_documents_bucket.grant_write(self.upload_handler)
        self.jobs_table.grant_write_data(self.upload_handler)
        upload_integration = apigwv2_integrations.HttpLambdaIntegration(
            "UploadIntegration", self.upload_handler
        )

        # `JobStatusHandler`: real implementation of
        # `GET /jobs/{jobId}`. Reads the `RedactionJobs` record for the
        # `jobId` path parameter and returns its current status (and
        # `errorMessage`, if any), or a 404 if no such Job exists. Only
        # needs read access to the jobs table.
        self.job_status_handler = lambda_.Function(
            self,
            "JobStatusHandler",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="job_status_handler.handler",
            code=lambda_.Code.from_asset("lambdas"),
            timeout=Duration.seconds(29),
            environment={
                "JOBS_TABLE_NAME": self.jobs_table.table_name,
            },
        )
        self.jobs_table.grant_read_data(self.job_status_handler)
        job_status_integration = apigwv2_integrations.HttpLambdaIntegration(
            "JobStatusIntegration", self.job_status_handler
        )

        # `DownloadHandler`: real implementation of
        # `GET /jobs/{jobId}/download`. Re-reads the `RedactionJobs` record
        # for the `jobId` path parameter immediately before returning the
        # file; only when that fresh read's status is `completed` does it
        # fetch the Redacted Document from `OutputDocumentsBucket` (keyed by
        # the record's `redactedKey`) and return it as a binary response.
        # Otherwise returns 404 (no such Job) or 409 (not `completed` at
        # this exact read), never touching the output bucket in either case.
        # Only
        # needs read access to the jobs table and the output bucket.
        self.download_handler = lambda_.Function(
            self,
            "DownloadHandler",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="download_handler.handler",
            code=lambda_.Code.from_asset("lambdas"),
            timeout=Duration.seconds(29),
            environment={
                "JOBS_TABLE_NAME": self.jobs_table.table_name,
                "OUTPUT_BUCKET_NAME": self.output_documents_bucket.bucket_name,
            },
        )
        self.jobs_table.grant_read_data(self.download_handler)
        self.output_documents_bucket.grant_read(self.download_handler)
        download_integration = apigwv2_integrations.HttpLambdaIntegration(
            "DownloadIntegration", self.download_handler
        )

        # `DetectionsHandler`: real implementation of
        # `GET /jobs/{jobId}/detections`. Reads the `RedactionJobs` record
        # to verify status is `"detected"`, then fetches
        # `{jobId}/detections.json` from the output documents bucket and
        # returns it. Returns 404 if the job doesn't exist, or 409 if the
        # job is not in the `"detected"` status.
        self.detections_handler = lambda_.Function(
            self,
            "DetectionsHandler",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="detections_handler.handler",
            code=lambda_.Code.from_asset("lambdas"),
            timeout=Duration.seconds(29),
            environment={
                "JOBS_TABLE_NAME": self.jobs_table.table_name,
                "OUTPUT_BUCKET_NAME": self.output_documents_bucket.bucket_name,
            },
        )
        self.jobs_table.grant_read_data(self.detections_handler)
        self.output_documents_bucket.grant_read(self.detections_handler)
        detections_integration = apigwv2_integrations.HttpLambdaIntegration(
            "DetectionsIntegration", self.detections_handler
        )

        # `RedactHandler`: real implementation of
        # `POST /jobs/{jobId}/redact`. Validates the request body
        # (`selectedIds` array of integers), then writes the
        # `confirmedSelections` attribute to the `RedactionJobs` record via
        # a conditional update (status must be `"detected"` and
        # `confirmedSelections` must not already exist). Returns 202 on
        # success, 400 for invalid body, 404 if job not found, or 409 if
        # status is wrong or already confirmed.
        self.redact_handler = lambda_.Function(
            self,
            "RedactHandler",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="redact_handler.handler",
            code=lambda_.Code.from_asset("lambdas"),
            timeout=Duration.seconds(29),
            environment={
                "JOBS_TABLE_NAME": self.jobs_table.table_name,
            },
        )
        self.jobs_table.grant_read_write_data(self.redact_handler)
        redact_integration = apigwv2_integrations.HttpLambdaIntegration(
            "RedactIntegration", self.redact_handler
        )

        # `JobDispatcher`: triggered by `RedactionJobs` table
        # DynamoDB Streams (`NEW_IMAGE`, configured on the table itself in
        # --- AgentCore Redaction Agent Runtime (Direct Code Deployment) ---
        # Deploys the agent code (agent/ + shared/ modules) and all Linux
        # ARM64 dependencies as a ZIP to S3, then creates an AgentCore
        # Runtime resource that runs the agent as a managed service.
        self.agent_runtime_artifact = agentcore.AgentRuntimeArtifact.from_code_asset(
            path="agent_bundle/package_clean",
            runtime=agentcore.AgentCoreRuntime.PYTHON_3_12,
            entrypoint=["main.py"],
            exclude=["**/__pycache__", "**/*.pyc"],
        )

        # --- Bedrock Guardrail: ML-based PII detection ---
        # Supplementary PII detection layer used alongside the regex-based
        # `match_rules` pipeline. Every supported Sensitive
        # Information Filter entity type is enabled with the `BLOCK` action
        # so that `ApplyGuardrail` responses include the matched text for
        # each detection. The L1 `CfnGuardrail` construct
        # is used because no L2 construct exists for Bedrock Guardrails yet.
        #
        # Note: the entity type list below is the set accepted by the
        # `AWS::Bedrock::Guardrail` PiiEntityConfig `Type` property. Aliases
        # like "SSN" / "PASSPORT_NUMBER" are not valid — the canonical names
        # are `US_SOCIAL_SECURITY_NUMBER` / `US_PASSPORT_NUMBER`.
        pii_entity_types = [
            # General
            "ADDRESS",
            "AGE",
            "NAME",
            "EMAIL",
            "PHONE",
            "USERNAME",
            "PASSWORD",
            "DRIVER_ID",
            "LICENSE_PLATE",
            "VEHICLE_IDENTIFICATION_NUMBER",
            # Finance
            "CREDIT_DEBIT_CARD_CVV",
            "CREDIT_DEBIT_CARD_EXPIRY",
            "CREDIT_DEBIT_CARD_NUMBER",
            "PIN",
            "INTERNATIONAL_BANK_ACCOUNT_NUMBER",
            "SWIFT_CODE",
            # IT
            "IP_ADDRESS",
            "MAC_ADDRESS",
            "URL",
            "AWS_ACCESS_KEY",
            "AWS_SECRET_KEY",
            # USA specific
            "US_BANK_ACCOUNT_NUMBER",
            "US_BANK_ROUTING_NUMBER",
            "US_INDIVIDUAL_TAX_IDENTIFICATION_NUMBER",
            "US_PASSPORT_NUMBER",
            "US_SOCIAL_SECURITY_NUMBER",
            # Canada specific
            "CA_HEALTH_NUMBER",
            "CA_SOCIAL_INSURANCE_NUMBER",
            # UK specific
            "UK_NATIONAL_HEALTH_SERVICE_NUMBER",
            "UK_NATIONAL_INSURANCE_NUMBER",
            "UK_UNIQUE_TAXPAYER_REFERENCE_NUMBER",
        ]
        self.pii_guardrail = bedrock.CfnGuardrail(
            self,
            "PiiGuardrail",
            name="pii-redaction-guardrail",
            description="PII detection for the document redaction pipeline",
            sensitive_information_policy_config=(
                bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
                    pii_entities_config=[
                        bedrock.CfnGuardrail.PiiEntityConfigProperty(
                            type=pii_type, action="BLOCK"
                        )
                        for pii_type in pii_entity_types
                    ]
                )
            ),
            blocked_input_messaging="PII detected",
            blocked_outputs_messaging="PII detected",
        )

        # A published Guardrail version gives `ApplyGuardrail` a stable
        # identifier to reference. `attr_version` is passed
        # to the Agent runtime as `GUARDRAIL_VERSION` below.
        self.pii_guardrail_version = bedrock.CfnGuardrailVersion(
            self,
            "PiiGuardrailVersion",
            guardrail_identifier=self.pii_guardrail.attr_guardrail_id,
        )

        # Execution role for the AgentCore Runtime — needs access to S3
        # buckets, DynamoDB tables, and Textract for the redaction pipeline.
        self.agent_execution_role = iam.Role(
            self,
            "AgentExecutionRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            inline_policies={
                "AgentRedactionPolicy": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=[
                                "s3:GetObject",
                                "s3:PutObject",
                            ],
                            resources=[
                                self.source_documents_bucket.arn_for_objects("*"),
                                self.output_documents_bucket.arn_for_objects("*"),
                            ],
                        ),
                        # Source documents are removed once a job completes,
                        # and detection results once the job leaves review.
                        iam.PolicyStatement(
                            actions=["s3:DeleteObject"],
                            resources=[
                                self.source_documents_bucket.arn_for_objects("*"),
                                self.output_documents_bucket.arn_for_objects(
                                    "*/detections.json"
                                ),
                            ],
                        ),
                        iam.PolicyStatement(
                            actions=[
                                "dynamodb:GetItem",
                                "dynamodb:UpdateItem",
                                "dynamodb:Scan",
                            ],
                            resources=[
                                self.jobs_table.table_arn,
                                self.rules_table.table_arn,
                            ],
                        ),
                        iam.PolicyStatement(
                            actions=["textract:DetectDocumentText"],
                            resources=["*"],
                        ),
                        # `bedrock:ApplyGuardrail` scoped to the provisioned
                        # Guardrail only.
                        iam.PolicyStatement(
                            actions=["bedrock:ApplyGuardrail"],
                            resources=[self.pii_guardrail.attr_guardrail_arn],
                        ),
                        # `bedrock:InvokeModel` for the LLM CJK PII recall
                        # path (`agent/tools/llm_pii_detect.py`), scoped to
                        # the pinned model only — no wildcard-model resource.
                        # A Converse call through a
                        # cross-Region inference profile authorizes against
                        # BOTH the profile AND the underlying foundation
                        # model, so both ARNs are required; granting only one
                        # yields `AccessDeniedException` at invocation time.
                        # The foundation-model ARN is wildcarded on Region
                        # (the US geo profile routes across US Regions) but
                        # NOT on model.
                        iam.PolicyStatement(
                            actions=["bedrock:InvokeModel"],
                            resources=[
                                f"arn:aws:bedrock:{self.region}:{self.account}"
                                f":inference-profile/{LLM_PII_MODEL_ID}",
                                f"arn:aws:bedrock:*::foundation-model/"
                                f"{LLM_PII_FOUNDATION_MODEL}",
                            ],
                        ),
                        # `bedrock:InvokeModel` for the multimodal
                        # embedded-image OCR path (`agent/tools/ocr.py`),
                        # scoped to the pinned image-OCR model only — no
                        # wildcard-model resource. As with
                        # the LLM PII grant above, a Converse call through a
                        # cross-Region inference profile authorizes against
                        # BOTH the profile AND the underlying foundation
                        # model, so both ARNs are required; the
                        # foundation-model ARN is wildcarded on Region but
                        # NOT on model. Kept as a separate statement for
                        # clarity even though `IMAGE_OCR_MODEL_ID` currently
                        # equals `LLM_PII_MODEL_ID` — either statement alone
                        # is scoped, and keeping them distinct lets the two
                        # models be retargeted independently without merging
                        # or splitting grants.
                        iam.PolicyStatement(
                            actions=["bedrock:InvokeModel"],
                            resources=[
                                f"arn:aws:bedrock:{self.region}:{self.account}"
                                f":inference-profile/{IMAGE_OCR_MODEL_ID}",
                                f"arn:aws:bedrock:*::foundation-model/"
                                f"{IMAGE_OCR_FOUNDATION_MODEL}",
                            ],
                        ),
                    ]
                )
            },
        )

        self.agent_runtime = agentcore.Runtime(
            self,
            "RedactionAgentRuntime",
            runtime_name="pii_redaction_agent",
            agent_runtime_artifact=self.agent_runtime_artifact,
            execution_role=self.agent_execution_role,
            environment_variables={
                "JOBS_TABLE_NAME": self.jobs_table.table_name,
                "RULES_TABLE_NAME": self.rules_table.table_name,
                "SOURCE_BUCKET_NAME": self.source_documents_bucket.bucket_name,
                "OUTPUT_BUCKET_NAME": self.output_documents_bucket.bucket_name,
                # Guardrail identifier + published version consumed by
                # `agent/tools/guardrails_pii_detect.py`.
                "GUARDRAIL_ID": self.pii_guardrail.attr_guardrail_id,
                "GUARDRAIL_VERSION": self.pii_guardrail_version.attr_version,
                # Raster resolution (dots per inch) used by
                # `agent/tools/ocr_pdf.py` when rasterizing each PDF page
                # before sending it to Textract `DetectDocumentText`. It
                # affects recognition quality and payload size only, never
                # rectangle placement (Textract bounding boxes are
                # normalized to the rasterized page). The value below is
                # just the default made explicit — the tool clamps whatever
                # it reads to [72, 400] and falls back to 200 when the
                # variable is absent, blank, non-numeric, or out of range,
                # so nothing here is load-bearing.
                "PDF_OCR_DPI": "200",
                # Severity threshold `agent/logging_config.py` applies to the
                # `agent` / `shared` loggers. The default made explicit, in
                # the same style as `PDF_OCR_DPI` above: raising verbosity
                # for a diagnostic run means editing a visible value here
                # rather than discovering an absent variable (Requirement
                # 4.6). The Configurator falls back to `INFO` when this is
                # absent, blank, or unrecognised, so nothing here is
                # load-bearing. Note it never widens third-party loggers:
                # the root level stays at an `INFO` floor even at `DEBUG`,
                # which is what keeps `botocore`'s request bodies (the whole
                # document text) out of the log stream.
                "LOG_LEVEL": "INFO",
                # Pinned, versioned Bedrock model id consumed by
                # `agent/tools/llm_pii_detect.py` for the LLM CJK PII recall
                # path. Clearing this variable is the
                # documented off switch: the tool goes
                # dormant and returns zero spans without issuing any request,
                # while Redaction Jobs still complete on the regex,
                # Guardrails, and OCR sources.
                "LLM_PII_MODEL_ID": LLM_PII_MODEL_ID,
                # Pinned, versioned Bedrock model id consumed by
                # `agent/tools/ocr.py` for the multimodal embedded-image OCR
                # path. Read from a distinct variable
                # from `LLM_PII_MODEL_ID` so the two models are configured
                # independently. Clearing this variable is
                # the documented off switch: the tool goes
                # dormant and returns the image regions unchanged without
                # issuing any request, while Redaction Jobs still complete on
                # the regex, Guardrails, LLM-text, and full-page OCR sources.
                "IMAGE_OCR_MODEL_ID": IMAGE_OCR_MODEL_ID,
            },
            description="PII Document Redaction Agent - extracts, matches, and redacts PII from documents",
        )

        # Retention_Period on the runtime's application log group.
        # The AgentCore service — not this stack —
        # creates `/aws/bedrock-agentcore/runtimes/{AgentRuntimeId}-DEFAULT`
        # on first invocation, so `application_log_group` is an imported
        # reference rather than an owned resource. `logs.LogRetention` is
        # therefore the mechanism: its custom resource calls
        # `CreateLogGroup` then `PutRetentionPolicy`, tolerating
        # `ResourceAlreadyExistsException`, so it works whether the group
        # exists yet or not.
        #
        # Retention is LOG_RETENTION (1 day), the same as the documents:
        # the logs are only for troubleshooting a recent job, and there is
        # no reason to keep records about a job longer than the job itself.
        # `DESTROY` so `cdk destroy` leaves nothing behind.
        #
        # One caveat: the group name embeds `AgentRuntimeId`, so replacing
        # the runtime produces a new group and this resource follows the new
        # name, leaving the old group at whatever retention it had.
        # Acceptable for a POC; a job for a future cleanup task.
        logs.LogRetention(
            self,
            "AgentRuntimeLogRetention",
            log_group_name=self.agent_runtime.application_log_group.log_group_name,
            retention=LOG_RETENTION,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # `JobDispatcher`: triggered by DynamoDB Streams on the
        # `RedactionJobs` table (newly created items with `status ==
        # "processing"`).
        # Filters Streams records to newly-inserted Jobs with
        # `status == "processing"` and calls AgentCore's
        # `InvokeAgentRuntime` for each, passing `jobId`; it does not wait
        # for the Agent to finish.
        self.job_dispatcher = lambda_.Function(
            self,
            "JobDispatcher",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="job_dispatcher.handler",
            code=lambda_.Code.from_asset("lambdas"),
            timeout=Duration.seconds(300),
            environment={
                "AGENT_RUNTIME_ARN": self.agent_runtime.agent_runtime_arn,
            },
        )
        self.job_dispatcher.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[
                    self.agent_runtime.agent_runtime_arn,
                    f"{self.agent_runtime.agent_runtime_arn}/*",
                ],
            )
        )
        self.job_dispatcher.add_event_source(
            lambda_event_sources.DynamoEventSource(
                self.jobs_table,
                starting_position=lambda_.StartingPosition.LATEST,
                batch_size=10,
                retry_attempts=2,
            )
        )

        # HTTP API (API Gateway v2). HTTPS-only at the edge by construction:
        # no custom domain / HTTP listener is configured, so there is no
        # non-HTTPS entry point to disable.
        # Users sign in with Cognito; every API route requires a valid token.
        # Self sign-up is off: accounts are created by an administrator
        # (`aws cognito-idp admin-create-user`), so only invited people can
        # upload documents or read results.
        self.user_pool = cognito.UserPool(
            self,
            "RedactionUserPool",
            user_pool_name="PiiDocumentRedactionUsers",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=False,
                temp_password_validity=Duration.days(3),
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            user_invitation=cognito.UserInvitationConfig(
                email_subject="PII 文档脱敏系统 - 账号邀请 / Account invitation",
                email_body=_invitation_body(app_url),
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.user_pool_client = self.user_pool.add_client(
            "WebClient",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_password=True),
            prevent_user_existence_errors=True,
            access_token_validity=Duration.hours(1),
            id_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.hours(8),
        )
        admins_group = cognito.CfnUserPoolGroup(
            self,
            "AdminsGroup",
            user_pool_id=self.user_pool.user_pool_id,
            group_name=ADMIN_GROUP_NAME,
            description="Administrators: manage users and redaction rules.",
        )
        # First administrator: created (and invited by email) on deploy, so a
        # fresh installation can be administered without any manual step.
        # The handler never deletes or demotes anyone.
        self.bootstrap_admin_handler = lambda_.Function(
            self,
            "BootstrapAdminHandler",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="bootstrap_admin_handler.handler",
            code=lambda_.Code.from_asset("lambdas"),
            timeout=Duration.seconds(29),
        )
        self.bootstrap_admin_handler.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "cognito-idp:AdminGetUser",
                    "cognito-idp:AdminCreateUser",
                    "cognito-idp:AdminAddUserToGroup",
                ],
                resources=[self.user_pool.user_pool_arn],
            )
        )
        bootstrap_provider = cr.Provider(
            self, "BootstrapAdminProvider", on_event_handler=self.bootstrap_admin_handler
        )
        bootstrap_admin = CustomResource(
            self,
            "BootstrapAdmin",
            service_token=bootstrap_provider.service_token,
            properties={"UserPoolId": self.user_pool.user_pool_id, "Email": admin_email},
        )
        bootstrap_admin.node.add_dependency(admins_group)

        # User management API, for administrators only.
        self.users_handler = lambda_.Function(
            self,
            "UsersHandler",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="users_handler.handler",
            code=lambda_.Code.from_asset("lambdas"),
            timeout=Duration.seconds(29),
            environment={"USER_POOL_ID": self.user_pool.user_pool_id},
        )
        self.users_handler.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "cognito-idp:ListUsers",
                    "cognito-idp:ListUsersInGroup",
                    "cognito-idp:AdminGetUser",
                    "cognito-idp:AdminCreateUser",
                    "cognito-idp:AdminDeleteUser",
                    "cognito-idp:AdminAddUserToGroup",
                    "cognito-idp:AdminRemoveUserFromGroup",
                    "cognito-idp:AdminUserGlobalSignOut",
                ],
                resources=[self.user_pool.user_pool_arn],
            )
        )
        users_integration = apigwv2_integrations.HttpLambdaIntegration(
            "UsersIntegration", self.users_handler
        )

        api_authorizer = apigwv2_authorizers.HttpUserPoolAuthorizer(
            "CognitoAuthorizer",
            self.user_pool,
            user_pool_clients=[self.user_pool_client],
        )

        self.http_api = apigwv2.HttpApi(
            self,
            "RedactionHttpApi",
            api_name="PiiDocumentRedactionPocApi",
            create_default_stage=True,
            # Applies to every route; CORS preflight (OPTIONS) is answered by
            # API Gateway itself and needs no token.
            default_authorizer=api_authorizer,
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=["http://localhost:5173", "http://44.236.84.187:5173"],
                allow_methods=[
                    apigwv2.CorsHttpMethod.GET,
                    apigwv2.CorsHttpMethod.POST,
                    apigwv2.CorsHttpMethod.PATCH,
                    apigwv2.CorsHttpMethod.DELETE,
                    apigwv2.CorsHttpMethod.OPTIONS,
                ],
                allow_headers=["Content-Type", "Authorization"],
            ),
        )
        default_stage = self.http_api.default_stage.node.default_child
        default_stage.default_route_settings = apigwv2.CfnStage.RouteSettingsProperty(
            throttling_rate_limit=API_THROTTLE_RATE_LIMIT,
            throttling_burst_limit=API_THROTTLE_BURST_LIMIT,
        )

        CfnOutput(self, "ApiUrl", value=self.http_api.api_endpoint)
        CfnOutput(self, "UserPoolId", value=self.user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=self.user_pool_client.user_pool_client_id)

        # Route table (design.md "API Design"). `POST /jobs` points at the
        # real `UploadHandler`, `GET /jobs/{jobId}` at the real
        # `JobStatusHandler`, `GET /jobs/{jobId}/download` at
        # the real `DownloadHandler`, `GET /rules` at the real
        # `RulesListHandler`, `POST /rules` /
        # `PATCH /rules/{ruleName}` / `DELETE /rules/{ruleName}` at the real
        # `RuleOperationSubmitHandler`,
        # `GET /rule-operations/{operationId}` at the real
        # `RuleOperationStatusHandler`,
        # `GET /jobs/{jobId}/detections` at `DetectionsHandler`, and
        # `POST /jobs/{jobId}/redact` at `RedactHandler`.
        self.http_api.add_routes(
            path="/jobs",
            methods=[apigwv2.HttpMethod.POST],
            integration=upload_integration,
        )
        self.http_api.add_routes(
            path="/jobs/{jobId}",
            methods=[apigwv2.HttpMethod.GET],
            integration=job_status_integration,
        )
        self.http_api.add_routes(
            path="/jobs/{jobId}/download",
            methods=[apigwv2.HttpMethod.GET],
            integration=download_integration,
        )
        self.http_api.add_routes(
            path="/rules",
            methods=[apigwv2.HttpMethod.GET],
            integration=rules_list_integration,
        )
        self.http_api.add_routes(
            path="/rules",
            methods=[apigwv2.HttpMethod.POST],
            integration=rule_operation_submit_integration,
        )
        self.http_api.add_routes(
            path="/rules/{ruleName}",
            methods=[apigwv2.HttpMethod.PATCH],
            integration=rule_operation_submit_integration,
        )
        self.http_api.add_routes(
            path="/rules/{ruleName}",
            methods=[apigwv2.HttpMethod.DELETE],
            integration=rule_operation_submit_integration,
        )
        self.http_api.add_routes(
            path="/rule-operations/{operationId}",
            methods=[apigwv2.HttpMethod.GET],
            integration=rule_operation_status_integration,
        )
        self.http_api.add_routes(
            path="/jobs/{jobId}/detections",
            methods=[apigwv2.HttpMethod.GET],
            integration=detections_integration,
        )
        self.http_api.add_routes(
            path="/jobs/{jobId}/redact",
            methods=[apigwv2.HttpMethod.POST],
            integration=redact_integration,
        )
        self.http_api.add_routes(
            path="/users",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
            integration=users_integration,
        )
        self.http_api.add_routes(
            path="/users/{username}",
            methods=[apigwv2.HttpMethod.PATCH, apigwv2.HttpMethod.DELETE],
            integration=users_integration,
        )
        self.http_api.add_routes(
            path="/users/{username}/resend-invite",
            methods=[apigwv2.HttpMethod.POST],
            integration=users_integration,
        )

        # Last, so every function defined above is covered.
        self._limit_lambda_log_retention()

    def _limit_lambda_log_retention(self) -> None:
        """Give every Lambda function in the stack a LOG_RETENTION log group.

        Lambda creates `/aws/lambda/<function name>` itself on first
        invocation with no expiry, so without this its logs would be kept
        forever. This covers the app's handlers and the functions CDK adds
        for custom resources (bucket auto-delete, provider frameworks). The
        LogRetention function itself is skipped: it sets a 1-day retention on
        its own log group, and pointing a LogRetention at it would be a
        circular dependency.
        """
        functions = [
            node
            for node in self.node.find_all()
            if isinstance(node, CfnResource)
            and node.cfn_resource_type == "AWS::Lambda::Function"
            and not node.node.scope.node.id.startswith("LogRetention")
        ]
        for function in functions:
            logs.LogRetention(
                self,
                f"FunctionLogRetention{function.node.addr[-8:]}",
                log_group_name=Fn.join("", ["/aws/lambda/", function.ref]),
                retention=LOG_RETENTION,
                removal_policy=RemovalPolicy.DESTROY,
            )
