#!/usr/bin/env python3
"""CDK application entry point for the PII Document Redaction POC backend.

This app currently synthesizes an (intentionally empty) skeleton stack.
Actual infrastructure (DynamoDB tables, S3 buckets, SQS queue, API Gateway,
Lambda functions, AgentCore wiring, etc.) is added in later tasks.
"""
import aws_cdk as cdk

from infra.redaction_stack import RedactionStack

app = cdk.App()

RedactionStack(
    app,
    "PiiDocumentRedactionPocStack",
    description="PII Document Redaction POC backend (infra, lambdas, agent).",
)

app.synth()
