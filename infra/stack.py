"""AWS deployment: API Gateway (REST, response streaming) -> Lambda (container, Web Adapter)
-> S3 artifacts, plus the React UI on S3 + CloudFront.

    cdk deploy -c account=123456789012 -c region=us-east-1

Before deploying, put the Claude key in Secrets Manager:
    aws secretsmanager create-secret --name excel-copilot/anthropic-api-key --secret-string sk-ant-...
"""
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (aws_apigateway as apigw, aws_cloudfront as cf, aws_cloudfront_origins as origins,
                     aws_ecr_assets as ecr_assets, aws_iam as iam, aws_lambda as lambda_, aws_logs as logs,
                     aws_s3 as s3, aws_s3_deployment as s3deploy, aws_secretsmanager as sm)
from constructs import Construct

ROOT = Path(__file__).resolve().parent.parent


class ExcelCopilotStack(cdk.Stack):
    def __init__(self, scope: Construct, cid: str, **kw) -> None:
        super().__init__(scope, cid, **kw)
        ctx = self.node.try_get_context

        # ---------------------------------------------------------------- storage
        artifacts = s3.Bucket(
            self, "Artifacts",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=False,
            lifecycle_rules=[s3.LifecycleRule(prefix="runs/", expiration=cdk.Duration.days(int(ctx("artifactRetentionDays") or 7))),
                             s3.LifecycleRule(prefix="workbooks/", expiration=cdk.Duration.days(30)),
                             s3.LifecycleRule(abort_incomplete_multipart_upload_after=cdk.Duration.days(1))],
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        # ---------------------------------------------------------------- UI hosting
        site = s3.Bucket(self, "Site", block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
                         encryption=s3.BucketEncryption.S3_MANAGED, enforce_ssl=True,
                         removal_policy=cdk.RemovalPolicy.DESTROY, auto_delete_objects=True)
        dist = cf.Distribution(
            self, "Cdn",
            default_root_object="index.html",
            default_behavior=cf.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(site),
                viewer_protocol_policy=cf.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                response_headers_policy=cf.ResponseHeadersPolicy.SECURITY_HEADERS),
            error_responses=[cf.ErrorResponse(http_status=403, response_http_status=200, response_page_path="/index.html")],
        )
        ui_origin = f"https://{dist.distribution_domain_name}"

        # ---------------------------------------------------------------- API function
        secret = sm.Secret.from_secret_name_v2(self, "ClaudeKey", ctx("anthropicSecretName"))
        fn = lambda_.DockerImageFunction(
            self, "Api",
            code=lambda_.DockerImageCode.from_image_asset(
                str(ROOT / "backend"), target="lambda", platform=ecr_assets.Platform.LINUX_AMD64),
            architecture=lambda_.Architecture.X86_64,
            memory_size=3008,                 # pandas + a sandbox subprocess; more memory = more CPU
            timeout=cdk.Duration.minutes(5),
            ephemeral_storage_size=cdk.Size.mebibytes(1024),
            reserved_concurrent_executions=ctx("reservedConcurrency"),
            environment={
                "ARTIFACT_BUCKET": artifacts.bucket_name,
                "ANTHROPIC_SECRET_ARN": secret.secret_arn,
                "EXCEL_COPILOT_LLM": "claude",
                "EXCEL_COPILOT_MODEL": ctx("model") or "claude-sonnet-5",
                "CORS_ORIGINS": ui_origin,
                "LOG_LEVEL": "INFO",
            },
            log_group=logs.LogGroup(self, "ApiLogs", retention=logs.RetentionDays.ONE_MONTH,
                                    removal_policy=cdk.RemovalPolicy.DESTROY),
            tracing=lambda_.Tracing.ACTIVE,
        )
        artifacts.grant_read_write(fn)
        secret.grant_read(fn)

        # ---------------------------------------------------------------- API Gateway (streaming)
        api = apigw.RestApi(
            self, "Rest",
            rest_api_name="excel-copilot",
            binary_media_types=["multipart/form-data"],
            deploy_options=apigw.StageOptions(stage_name="v1api", throttling_rate_limit=20, throttling_burst_limit=40,
                                              metrics_enabled=True, tracing_enabled=True),
            default_cors_preflight_options=apigw.CorsOptions(allow_origins=[ui_origin], allow_methods=["GET", "POST"],
                                                             allow_headers=["Content-Type", "X-API-Key"]),
            endpoint_types=[apigw.EndpointType.REGIONAL],   # edge-optimised streams have a 30 s idle timeout
        )
        integration = apigw.LambdaIntegration(
            fn, proxy=True,
            response_transfer_mode=apigw.ResponseTransferMode.STREAM,   # InvokeWithResponseStreaming
            timeout=cdk.Duration.minutes(5),
        )
        api.root.add_proxy(default_integration=integration, any_method=True)

        # ---------------------------------------------------------------- UI build + config
        s3deploy.BucketDeployment(
            self, "DeploySite",
            sources=[s3deploy.Source.asset(str(ROOT / "frontend" / "dist")),
                     s3deploy.Source.data("config.json", cdk.Fn.to_json_string({"apiUrl": api.url}))],
            destination_bucket=site, distribution=dist, distribution_paths=["/*"],
        )

        cdk.CfnOutput(self, "UiUrl", value=ui_origin)
        cdk.CfnOutput(self, "ApiUrl", value=api.url)
        cdk.CfnOutput(self, "ArtifactBucket", value=artifacts.bucket_name)
