"""Converge the isolated LocalStack resources used by the image service."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

import boto3
from botocore.exceptions import ClientError
from build_lambda import DEFAULT_ARCH

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local"
REGION = "us-east-1"
ACCOUNT = "000000000000"
BUCKET = "montycloud-images-v2-local"
IMAGE_TABLE = "montycloud-images-v2-local"
REQUEST_TABLE = "montycloud-upload-requests-v2-local"
API_FUNCTION = "montycloud-images-v2-api-local"
WORKER_FUNCTION = "montycloud-images-v2-worker-local"
RECOVERY_FUNCTION = "montycloud-images-v2-recovery-local"
API_NAME = "montycloud-images-v2-local"
SCHEDULE = "montycloud-images-v2-recovery-local"
RECOVERY_EVENT = '{"operation":"recover"}'
STAGE = "local"
INTERNAL_ENDPOINT = "http://localstack:4566"
ROLE_NAMES = {
    "api": "montycloud-images-v2-api-local",
    "worker": "montycloud-images-v2-worker-local",
    "recovery": "montycloud-images-v2-recovery-local",
}
INDEXES = {
    "global_gallery": ("public_scope", "gallery_sort"),
    "owner_gallery": ("owner_scope", "gallery_sort"),
    "maintenance": ("maintenance_scope", "maintenance_sort"),
}


def local_endpoint(raw: str) -> str:
    url = urlsplit(raw)
    if (
        url.scheme != "http"
        or url.hostname not in {"localhost", "127.0.0.1"}
        or url.username is not None
        or url.password is not None
        or url.path not in {"", "/"}
        or url.query
        or url.fragment
        or url.port is None
    ):
        raise ValueError("Endpoint must be http://localhost:PORT or http://127.0.0.1:PORT")
    return f"http://{url.hostname}:{url.port}"


def session(endpoint: str) -> boto3.session.Session:
    local_endpoint(endpoint)
    return boto3.session.Session(
        aws_access_key_id="test",
        aws_secret_access_key="test",
        aws_session_token="test",
        region_name=REGION,
    )


def code(error: ClientError) -> str:
    return error.response.get("Error", {}).get("Code", "")


def ensure_bucket(s3) -> None:
    try:
        s3.head_bucket(Bucket=BUCKET)
    except ClientError as error:
        if code(error) not in {"404", "NoSuchBucket", "NotFound"}:
            raise
        s3.create_bucket(Bucket=BUCKET)
    versioning = s3.get_bucket_versioning(Bucket=BUCKET)
    if versioning.get("Status") is not None:
        raise RuntimeError(f"Bucket {BUCKET} has versioning history; use a fresh unversioned bucket")
    s3.put_public_access_block(
        Bucket=BUCKET,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    s3.put_bucket_encryption(
        Bucket=BUCKET,
        ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
        },
    )


def ensure_table(ddb, name: str, key: str, indexes: dict[str, tuple[str, str]]) -> str:
    definitions = {key, *(field for pair in indexes.values() for field in pair)}
    try:
        table = ddb.describe_table(TableName=name)["Table"]
    except ddb.exceptions.ResourceNotFoundException:
        args = {
            "TableName": name,
            "KeySchema": [{"AttributeName": key, "KeyType": "HASH"}],
            "AttributeDefinitions": [
                {"AttributeName": field, "AttributeType": "S"} for field in sorted(definitions)
            ],
            "BillingMode": "PAY_PER_REQUEST",
        }
        if indexes:
            args["GlobalSecondaryIndexes"] = [
                {
                    "IndexName": index_name,
                    "KeySchema": [
                        {"AttributeName": partition, "KeyType": "HASH"},
                        {"AttributeName": sort, "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
                for index_name, (partition, sort) in indexes.items()
            ]
        ddb.create_table(**args)
        ddb.get_waiter("table_exists").wait(
            TableName=name, WaiterConfig={"Delay": 2, "MaxAttempts": 45}
        )
        table = ddb.describe_table(TableName=name)["Table"]
    if table["TableStatus"] != "ACTIVE":
        ddb.get_waiter("table_exists").wait(
            TableName=name, WaiterConfig={"Delay": 2, "MaxAttempts": 45}
        )
        table = ddb.describe_table(TableName=name)["Table"]
    if table["KeySchema"] != [{"AttributeName": key, "KeyType": "HASH"}]:
        raise RuntimeError(f"Existing table {name} has an incompatible primary key")
    actual_definitions = {
        item["AttributeName"]: item["AttributeType"]
        for item in table["AttributeDefinitions"]
    }
    expected_definitions = {field: "S" for field in definitions}
    if actual_definitions != expected_definitions:
        raise RuntimeError(f"Existing table {name} has incompatible attribute definitions")
    actual = {
        index["IndexName"]: (
            tuple(part["AttributeName"] for part in index["KeySchema"]),
            index["Projection"]["ProjectionType"],
        )
        for index in table.get("GlobalSecondaryIndexes", [])
    }
    expected = {index_name: (pair, "ALL") for index_name, pair in indexes.items()}
    if actual != expected:
        raise RuntimeError(f"Existing table {name} has incompatible indexes: {actual}")
    return table["TableArn"]


def ensure_ttl(ddb) -> None:
    state = ddb.describe_time_to_live(TableName=REQUEST_TABLE)["TimeToLiveDescription"]
    status = state.get("TimeToLiveStatus", "DISABLED")
    if status == "DISABLING":
        raise RuntimeError(f"TTL is disabling on {REQUEST_TABLE}; retry after it finishes")
    if status in {"ENABLED", "ENABLING"}:
        if state.get("AttributeName") != "expires_at":
            raise RuntimeError(f"TTL on {REQUEST_TABLE} uses the wrong attribute")
        return
    ddb.update_time_to_live(
        TableName=REQUEST_TABLE,
        TimeToLiveSpecification={"Enabled": True, "AttributeName": "expires_at"},
    )


def ensure_images_ttl_disabled(ddb) -> None:
    state = ddb.describe_time_to_live(TableName=IMAGE_TABLE)["TimeToLiveDescription"]
    if state.get("TimeToLiveStatus", "DISABLED") != "DISABLED":
        raise RuntimeError(f"TTL must be disabled on {IMAGE_TABLE}")


def arn_for_function(name: str) -> str:
    return f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{name}"


def role_policy(kind: str, image_arn: str, request_arn: str, bucket_arn: str) -> dict:
    statements = []

    def allow(actions: list[str], resource: str | list[str], condition: dict | None = None) -> None:
        statement = {"Effect": "Allow", "Action": actions, "Resource": resource}
        if condition:
            statement["Condition"] = condition
        statements.append(statement)

    if kind == "api":
        allow(["s3:GetObject"], f"{bucket_arn}/originals/*")
        allow(
            ["s3:PutObject"],
            f"{bucket_arn}/originals/*",
            {"Null": {"s3:if-none-match": "false"}},
        )
        allow(["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:BatchGetItem"], image_arn)
        allow(["dynamodb:GetItem", "dynamodb:PutItem"], request_arn)
        allow(["dynamodb:Query"], [f"{image_arn}/index/global_gallery", f"{image_arn}/index/owner_gallery"])
        allow(["lambda:InvokeFunction"], arn_for_function(WORKER_FUNCTION))
    elif kind == "worker":
        allow(["s3:GetObject", "s3:PutObject"], f"{bucket_arn}/originals/*")
        allow(["dynamodb:GetItem", "dynamodb:UpdateItem"], image_arn)
        allow(["lambda:InvokeFunction"], arn_for_function(WORKER_FUNCTION))
    elif kind == "recovery":
        allow(["dynamodb:GetItem", "dynamodb:UpdateItem"], image_arn)
        allow(["dynamodb:Query"], f"{image_arn}/index/maintenance")
        allow(["lambda:InvokeFunction"], arn_for_function(WORKER_FUNCTION))
    else:
        raise ValueError(kind)
    name = {"api": API_FUNCTION, "worker": WORKER_FUNCTION, "recovery": RECOVERY_FUNCTION}[kind]
    allow(
        ["logs:CreateLogStream", "logs:PutLogEvents"],
        f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/lambda/{name}:*",
    )
    return {"Version": "2012-10-17", "Statement": statements}


def ensure_role(iam, kind: str, policy: dict) -> str:
    name = ROLE_NAMES[kind]
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }],
    }
    try:
        role = iam.get_role(RoleName=name)["Role"]
    except iam.exceptions.NoSuchEntityException:
        role = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust))["Role"]
    else:
        iam.update_assume_role_policy(RoleName=name, PolicyDocument=json.dumps(trust))
    iam.put_role_policy(
        RoleName=name, PolicyName="montycloud-images-v2-local", PolicyDocument=json.dumps(policy)
    )
    return role["Arn"]


def ensure_upload_policy(s3, api_role_arn: str) -> None:
    # The signer may create an object only if the conditional header is present.
    # The worker uses a separate role so it can replace content with a placeholder.
    policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "RequireConditionalClientUpload",
            "Effect": "Deny",
            "Principal": {"AWS": api_role_arn},
            "Action": "s3:PutObject",
            "Resource": f"arn:aws:s3:::{BUCKET}/originals/*",
            "Condition": {"Null": {"s3:if-none-match": "true"}},
        }],
    }
    s3.put_bucket_policy(Bucket=BUCKET, Policy=json.dumps(policy))


def ensure_log_group(logs, name: str) -> None:
    group = f"/aws/lambda/{name}"
    with contextlib.suppress(logs.exceptions.ResourceAlreadyExistsException):
        logs.create_log_group(logGroupName=group)
    logs.put_retention_policy(logGroupName=group, retentionInDays=7)


def wait_for_function(lambda_client, name: str) -> None:
    for _ in range(90):
        config = lambda_client.get_function_configuration(FunctionName=name)
        state = config.get("State", "Active")
        update = config.get("LastUpdateStatus", "Successful")
        if state == "Failed" or update == "Failed":
            reason = config.get("StateReason") or config.get("LastUpdateStatusReason")
            raise RuntimeError(f"Lambda {name} failed: {reason}")
        if state == "Active" and update == "Successful":
            return
        time.sleep(2)
    raise TimeoutError(f"Lambda {name} did not become active within 180 seconds")


def ensure_function(lambda_client, name: str, role: str, handler: str, timeout: int,
                    memory: int, arch: str, environment: dict[str, str], archive: bytes) -> str:
    try:
        existing = lambda_client.get_function(FunctionName=name)["Configuration"]
    except lambda_client.exceptions.ResourceNotFoundException:
        created = lambda_client.create_function(
            FunctionName=name,
            Runtime="python3.13",
            Role=role,
            Handler=handler,
            Code={"ZipFile": archive},
            Timeout=timeout,
            MemorySize=memory,
            Architectures=[arch],
            Environment={"Variables": environment},
            Publish=False,
        )
        wait_for_function(lambda_client, name)
        return created["FunctionArn"]
    wait_for_function(lambda_client, name)
    if existing.get("Architectures", ["x86_64"]) != [arch]:
        raise RuntimeError(f"Existing Lambda {name} architecture differs from {arch}")
    lambda_client.update_function_configuration(
        FunctionName=name,
        Runtime="python3.13",
        Role=role,
        Handler=handler,
        Timeout=timeout,
        MemorySize=memory,
        Environment={"Variables": environment},
    )
    wait_for_function(lambda_client, name)
    lambda_client.update_function_code(FunctionName=name, ZipFile=archive, Publish=False)
    wait_for_function(lambda_client, name)
    return lambda_client.get_function(FunctionName=name)["Configuration"]["FunctionArn"]


def ensure_api(gateway, lambda_client, api_arn: str, endpoint: str) -> str:
    apis = gateway.get_rest_apis(limit=500).get("items", [])
    api = next((item for item in apis if item["name"] == API_NAME), None)
    if api is None:
        api = gateway.create_rest_api(name=API_NAME, endpointConfiguration={"types": ["REGIONAL"]})
    api_id = api["id"]
    resources = gateway.get_resources(restApiId=api_id, limit=500).get("items", [])
    root = next(item for item in resources if item["path"] == "/")
    proxy = next((item for item in resources if item.get("pathPart") == "{proxy+}"), None)
    if proxy is None:
        proxy = gateway.create_resource(restApiId=api_id, parentId=root["id"], pathPart="{proxy+}")
    uri = f"arn:aws:apigateway:{REGION}:lambda:path/2015-03-31/functions/{api_arn}/invocations"
    for resource in (root, proxy):
        try:
            gateway.put_method(
                restApiId=api_id,
                resourceId=resource["id"],
                httpMethod="ANY",
                authorizationType="NONE",
            )
        except ClientError as error:
            if code(error) != "ConflictException":
                raise
        gateway.put_integration(
            restApiId=api_id,
            resourceId=resource["id"],
            httpMethod="ANY",
            type="AWS_PROXY",
            integrationHttpMethod="POST",
            uri=uri,
        )
    try:
        lambda_client.add_permission(
            FunctionName=API_FUNCTION,
            StatementId=f"api-{api_id}",
            Action="lambda:InvokeFunction",
            Principal="apigateway.amazonaws.com",
            SourceArn=f"arn:aws:execute-api:{REGION}:{ACCOUNT}:{api_id}/*/*",
        )
    except ClientError as error:
        if code(error) != "ResourceConflictException":
            raise
    gateway.create_deployment(restApiId=api_id, stageName=STAGE, description="Local image service")
    return f"{endpoint}/restapis/{api_id}/{STAGE}/_user_request_"


def ensure_schedule(events, lambda_client, recovery_arn: str) -> None:
    try:
        rule = events.describe_rule(Name=SCHEDULE)
    except ClientError as error:
        if code(error) != "ResourceNotFoundException":
            raise
        rule = None
    if rule is None or rule.get("ScheduleExpression") != "rate(5 minutes)" or rule.get("State") != "ENABLED":
        rule_arn = events.put_rule(
            Name=SCHEDULE, ScheduleExpression="rate(5 minutes)", State="ENABLED"
        )["RuleArn"]
    else:
        rule_arn = rule["Arn"]
    try:
        lambda_client.add_permission(
            FunctionName=RECOVERY_FUNCTION,
            StatementId=f"events-{SCHEDULE}",
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=rule_arn,
        )
    except ClientError as error:
        if code(error) != "ResourceConflictException":
            raise
    target = {"Id": "recovery", "Arn": recovery_arn, "Input": RECOVERY_EVENT}
    existing = events.list_targets_by_rule(Rule=SCHEDULE)["Targets"]
    if existing == [target]:
        return
    if any(item["Id"] != "recovery" for item in existing):
        raise RuntimeError(f"Existing EventBridge rule {SCHEDULE} has unexpected targets")
    result = events.put_targets(Rule=SCHEDULE, Targets=[target])
    if result.get("FailedEntryCount"):
        raise RuntimeError(f"Failed to attach EventBridge recovery target: {result['FailedEntries']}")


def cursor_secret() -> str:
    path = LOCAL / "cursor_secret"
    if path.exists():
        if path.is_symlink():
            raise RuntimeError(f"Refusing cursor secret symlink: {path}")
        value = path.read_text().strip()
        if len(value) < 32:
            raise RuntimeError(f"Cursor secret is too short: {path}")
        return value
    value = secrets.token_urlsafe(48)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(value + "\n")
    return value


def bootstrap(endpoint: str, arch: str) -> dict[str, str]:
    endpoint = local_endpoint(endpoint)
    archive = LOCAL / "function.zip"
    if not archive.is_file():
        raise FileNotFoundError(f"Missing {archive}; run make build first")
    if arch not in {"arm64", "x86_64"}:
        raise ValueError(f"Unsupported architecture: {arch}")
    LOCAL.mkdir(exist_ok=True)
    secret = cursor_secret()
    aws = session(endpoint)
    s3 = aws.client("s3", endpoint_url=endpoint)
    ddb = aws.client("dynamodb", endpoint_url=endpoint)
    iam = aws.client("iam", endpoint_url=endpoint)
    lambda_client = aws.client("lambda", endpoint_url=endpoint)
    gateway = aws.client("apigateway", endpoint_url=endpoint)
    events = aws.client("events", endpoint_url=endpoint)
    logs = aws.client("logs", endpoint_url=endpoint)

    ensure_bucket(s3)
    image_arn = ensure_table(ddb, IMAGE_TABLE, "image_id", INDEXES)
    request_arn = ensure_table(ddb, REQUEST_TABLE, "request_key", {})
    ensure_images_ttl_disabled(ddb)
    ensure_ttl(ddb)
    bucket_arn = f"arn:aws:s3:::{BUCKET}"
    roles = {
        kind: ensure_role(iam, kind, role_policy(kind, image_arn, request_arn, bucket_arn))
        for kind in ROLE_NAMES
    }
    ensure_upload_policy(s3, roles["api"])
    for name in (API_FUNCTION, WORKER_FUNCTION, RECOVERY_FUNCTION):
        ensure_log_group(logs, name)
    common = {"APP_ENV": "local", "AWS_ENDPOINT_URL": INTERNAL_ENDPOINT}
    archive_bytes = archive.read_bytes()
    ensure_function(
        lambda_client, WORKER_FUNCTION, roles["worker"],
        "image_service.lifecycle.worker_handler", 120, 1024, arch,
        {**common, "IMAGE_BUCKET": BUCKET, "IMAGE_TABLE": IMAGE_TABLE,
         "WORKER_FUNCTION": WORKER_FUNCTION}, archive_bytes,
    )
    lambda_client.put_function_event_invoke_config(
        FunctionName=WORKER_FUNCTION,
        MaximumRetryAttempts=0,
        MaximumEventAgeInSeconds=300,
    )
    recovery_arn = ensure_function(
        lambda_client, RECOVERY_FUNCTION, roles["recovery"],
        "image_service.lifecycle.recovery_handler", 60, 512, arch,
        {**common, "IMAGE_TABLE": IMAGE_TABLE, "WORKER_FUNCTION": WORKER_FUNCTION}, archive_bytes,
    )
    api_arn = ensure_function(
        lambda_client, API_FUNCTION, roles["api"],
        "image_service.http.handler", 30, 512, arch,
        {
            **common,
            "IMAGE_BUCKET": BUCKET,
            "IMAGE_TABLE": IMAGE_TABLE,
            "REQUEST_TABLE": REQUEST_TABLE,
            "WORKER_FUNCTION": WORKER_FUNCTION,
            "S3_PUBLIC_ENDPOINT": endpoint,
            "CURSOR_SECRET": secret,
        }, archive_bytes,
    )
    api_url = ensure_api(gateway, lambda_client, api_arn, endpoint)
    ensure_schedule(events, lambda_client, recovery_arn)
    result = {
        "api_url": api_url,
        "endpoint_url": endpoint,
        "bucket_name": BUCKET,
        "image_table": IMAGE_TABLE,
        "request_table": REQUEST_TABLE,
        "api_function": API_FUNCTION,
        "worker_function": WORKER_FUNCTION,
        "recovery_function": RECOVERY_FUNCTION,
        "schedule_name": SCHEDULE,
        "schedule_type": "eventbridge_rule",
        "region": REGION,
        "architecture": arch,
    }
    output = LOCAL / "api.json"
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--endpoint-url", default=os.environ.get("LOCALSTACK_ENDPOINT", "http://localhost:4567")
    )
    parser.add_argument("--arch", choices=("arm64", "x86_64"), default=DEFAULT_ARCH)
    options = parser.parse_args()
    bootstrap(options.endpoint_url, options.arch)
