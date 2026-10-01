"""CDK assertions on the synthesized template (no AWS account needed): `pytest infra/`."""
import json

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from stack import ExcelCopilotStack


def template() -> Template:
    app = cdk.App(context={"anthropicSecretName": "excel-copilot/anthropic-api-key", "model": "claude-sonnet-5",
                           "artifactRetentionDays": 7})
    return Template.from_stack(ExcelCopilotStack(app, "T", env=cdk.Environment(account="123456789012", region="us-east-1")))


T = template()


def test_api_streams_from_lambda():
    T.has_resource_properties("AWS::ApiGateway::Method", {
        "HttpMethod": "ANY",
        "Integration": Match.object_like({
            "Type": "AWS_PROXY",
            "ResponseTransferMode": "STREAM",
            "TimeoutInMillis": 300000,
            "Uri": Match.object_like({"Fn::Join": Match.array_with([
                Match.array_with([Match.string_like_regexp("response-streaming-invocations")])])}),
        }),
    })
    T.has_resource_properties("AWS::ApiGateway::RestApi", {"EndpointConfiguration": {"Types": ["REGIONAL"]}})


def test_artifact_bucket_is_private_encrypted_and_expiring():
    T.has_resource_properties("AWS::S3::Bucket", {
        "PublicAccessBlockConfiguration": {"BlockPublicAcls": True, "BlockPublicPolicy": True,
                                           "IgnorePublicAcls": True, "RestrictPublicBuckets": True},
        "BucketEncryption": Match.any_value(),
        "LifecycleConfiguration": {"Rules": Match.array_with([
            Match.object_like({"Prefix": "runs/", "ExpirationInDays": 7, "Status": "Enabled"})])},
    })


def test_lambda_gets_secret_by_reference_not_value():
    fns = T.find_resources("AWS::Lambda::Function", {"Properties": {"PackageType": "Image"}})
    assert len(fns) == 1
    env = next(iter(fns.values()))["Properties"]["Environment"]["Variables"]
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_SECRET_ARN" in env
    assert env["EXCEL_COPILOT_MODEL"] == "claude-sonnet-5"
    policies = json.dumps(T.find_resources("AWS::IAM::Policy"))
    assert "secretsmanager:GetSecretValue" in policies and "s3:PutObject" in policies
