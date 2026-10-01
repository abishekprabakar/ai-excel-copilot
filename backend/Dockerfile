# syntax=docker/dockerfile:1.7
# One image, two targets:
#   local  -> plain uvicorn container (docker compose)
#   lambda -> same app behind the AWS Lambda Web Adapter in response-streaming mode
FROM public.ecr.aws/docker/library/python:3.12-slim AS local
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
WORKDIR /app
COPY pyproject.toml ./
COPY excel_copilot ./excel_copilot
RUN pip install . && useradd --uid 10001 --no-create-home app
USER 10001
EXPOSE 8080
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["uvicorn", "excel_copilot.api.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]

FROM local AS lambda
# Lambda Web Adapter: translates Lambda invocations to HTTP on :8080 and, with
# AWS_LWA_INVOKE_MODE=response_stream, streams the SSE body back incrementally.
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 /lambda-adapter /opt/extensions/lambda-adapter
ENV AWS_LWA_INVOKE_MODE=response_stream \
    AWS_LWA_PORT=8080 \
    AWS_LWA_READINESS_CHECK_PATH=/healthz \
    LOCAL_STORAGE_DIR=/tmp/excel-copilot
