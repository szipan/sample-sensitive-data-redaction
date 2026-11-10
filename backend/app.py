#!/usr/bin/env python3
"""CDK application entry point for the PII Document Redaction POC backend.

This app currently synthesizes an (intentionally empty) skeleton stack.
Actual infrastructure (DynamoDB tables, S3 buckets, SQS queue, API Gateway,
Lambda functions, AgentCore wiring, etc.) is added in later tasks.
"""
import aws_cdk as cdk

from infra.redaction_stack import RedactionStack

app = cdk.App()

import os

RedactionStack(
    app,
    "PiiDocumentRedactionPocStack",
    # First administrator, invited by email on deploy:
    #   cdk deploy -c adminEmail=you@example.com [-c appUrl=https://...]
    admin_email=app.node.try_get_context("adminEmail") or os.environ.get("ADMIN_EMAIL"),
    app_url=app.node.try_get_context("appUrl") or os.environ.get("APP_URL"),
    description="PII Document Redaction POC backend (infra, lambdas, agent).",
)

app.synth()
