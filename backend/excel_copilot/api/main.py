"""ASGI entry point: `uvicorn excel_copilot.api.main:app` (also used on Lambda via the Web Adapter)."""
import logging
import os

from .app import create_app

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(message)s")


def _load_secret() -> None:
    """On Lambda the Claude key lives in Secrets Manager; fetch it once at cold start."""
    arn = os.environ.get("ANTHROPIC_SECRET_ARN")
    if arn and not os.environ.get("ANTHROPIC_API_KEY"):
        import boto3
        os.environ["ANTHROPIC_API_KEY"] = boto3.client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"].strip()


_load_secret()
app = create_app()
