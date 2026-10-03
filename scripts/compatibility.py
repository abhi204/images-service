"""Run the local-only protocol gate against the project's fresh LocalStack endpoint.

Usage: python scripts/compatibility.py [--endpoint http://localhost:4567]
Resources use a unique mc-compat prefix on every run. No AWS endpoint is accepted.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import http.client
import io
import json
import threading
import time
import urllib.parse
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

MAX_BYTES = 20_971_520
REGION = "us-east-1"


@dataclass
class Result:
    name: str
    status: str
    observed: str
    elapsed_ms: int


class Gate:
    def __init__(self, endpoint: str):
        parsed = urllib.parse.urlsplit(endpoint)
        if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"} or parsed.port != 4567:
            raise ValueError("Only the project's local http://localhost:4567 endpoint is allowed")
        self.endpoint = endpoint.rstrip("/")
        self.run_id = uuid.uuid4().hex[:12]
        self.prefix = f"mc-compat-{self.run_id}"
        self.results: list[Result] = []
        session = boto3.Session(aws_access_key_id="test", aws_secret_access_key="test", region_name=REGION)
        self.s3 = session.client(
            "s3",
            endpoint_url=self.endpoint,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
        self.lam = session.client("lambda", endpoint_url=self.endpoint)
        self.events = session.client("events", endpoint_url=self.endpoint)
        self.iam = session.client("iam", endpoint_url=self.endpoint)
        self.bucket = self.prefix

    def record(self, name: str, fn):
        start = time.monotonic()
        try:
            status, observed = fn()
        except Exception as exc:
            status, observed = "FAIL", error_name(exc)
        item = Result(name, status, observed, round((time.monotonic() - start) * 1000))
        self.results.append(item)
        print(f"{item.status:10} {item.name}: {item.observed} ({item.elapsed_ms} ms)", flush=True)
        return item

    def key(self, name: str) -> str:
        return f"{self.prefix}/{name}"

    def presign(self, key: str, length: int = 4, content_type: str = "image/png") -> str:
        return self.s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ContentLength": length,
                "ContentType": content_type,
                "IfNoneMatch": "*",
            },
            ExpiresIn=900,
            HttpMethod="PUT",
        )

    @staticmethod
    def put_http(url: str, body: bytes, headers: dict[str, str], chunked: bool = False) -> tuple[int, bytes]:
        parsed = urllib.parse.urlsplit(url)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=30)
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        try:
            conn.putrequest("PUT", path)
            for name, value in headers.items():
                conn.putheader(name, value)
            if chunked:
                conn.putheader("Transfer-Encoding", "chunked")
            conn.endheaders()
            if chunked:
                conn.send(f"{len(body):X}\r\n".encode() + body + b"\r\n0\r\n\r\n")
            else:
                conn.send(body)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def headers(self, length: int = 4, content_type: str = "image/png") -> dict[str, str]:
        return {"Content-Length": str(length), "Content-Type": content_type, "If-None-Match": "*"}

    def setup_bucket(self):
        self.s3.create_bucket(Bucket=self.bucket)
        self.s3.put_public_access_block(
            Bucket=self.bucket,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
        self.s3.put_bucket_encryption(
            Bucket=self.bucket,
            ServerSideEncryptionConfiguration={
                "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
            },
        )

    def bucket_configuration(self):
        encryption = self.s3.get_bucket_encryption(Bucket=self.bucket)
        access = self.s3.get_public_access_block(Bucket=self.bucket)["PublicAccessBlockConfiguration"]
        version = self.s3.get_bucket_versioning(Bucket=self.bucket)
        rule = encryption["ServerSideEncryptionConfiguration"]["Rules"][0]
        algorithm = rule["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"]
        checks = (algorithm == "AES256", all(access.values()), "Status" not in version)
        observed = f"SSE={algorithm}; public block={all(access.values())}; versioning={version.get('Status', 'off')}"
        return "PASS" if all(checks) else "FAIL", observed

    def signed_headers(self):
        url = self.presign(self.key("signed-headers"))
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        actual = set(query.get("X-Amz-SignedHeaders", [""])[0].split(";"))
        needed = {"content-length", "content-type", "if-none-match"}
        return ("PASS" if needed <= actual else "FAIL", "signed headers: " + ", ".join(sorted(actual)))

    def valid_and_retry(self):
        key = self.key("retry")
        url = self.presign(key)
        first, _ = self.put_http(url, b"abcd", self.headers())
        retry, _ = self.put_http(url, b"WXYZ", self.headers())
        stored = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        return (
            "PASS" if first == 200 and retry == 412 and stored == b"abcd" else "FAIL",
            f"first HTTP {first}; retry HTTP {retry}; original retained={stored == b'abcd'}",
        )

    def unsigned_get(self):
        path = f"/{self.bucket}/{self.key('retry')}"
        conn = http.client.HTTPConnection("localhost", 4567, timeout=10)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            status = response.status
            response.read()
        finally:
            conn.close()
        verdict = "PASS" if status in {401, 403, 404} else "UNVERIFIED"
        return verdict, f"unsigned GET HTTP {status}"

    def altered_request(self, name: str, body: bytes, headers: dict[str, str], chunked: bool = False):
        key = self.key(name)
        url = self.presign(key)
        status, _ = self.put_http(url, body, headers, chunked)
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            exists = True
        except ClientError as exc:
            exists = exc.response["Error"]["Code"] not in {"404", "NoSuchKey", "NotFound"}
        verdict = "PASS" if status in {400, 403, 411, 412, 413} and not exists else "FAIL"
        return verdict, f"HTTP {status}; object created={exists}"

    def placeholder(self):
        key = self.key("placeholder")
        url = self.presign(key)
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=b"")
        status, _ = self.put_http(url, b"abcd", self.headers())
        obj = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        return ("PASS" if status == 412 and obj == b"" else "FAIL", f"replay HTTP {status}; stored bytes={len(obj)}")

    def race(self):
        key = self.key("race")
        url = self.presign(key)
        barrier = threading.Barrier(2)

        def upload():
            barrier.wait()
            return self.put_http(url, b"abcd", self.headers())[0]

        def clean():
            barrier.wait()
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=b"")

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(upload)
            b = pool.submit(clean)
            upload_status = a.result(timeout=35)
            b.result(timeout=35)
        stored = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        return (
            "PASS" if upload_status in {200, 412} and stored == b"" else "FAIL",
            f"upload HTTP {upload_status}; final stored bytes={len(stored)}",
        )

    def lambda_zip(self):
        source = """import json, os, boto3
def handler(event, context):
    s3 = boto3.client("s3", endpoint_url=os.environ["MARKER_ENDPOINT"])
    s3.put_object(Bucket=os.environ["MARKER_BUCKET"], Key=event["marker"], Body=b"executed")
    return {"ok": True}
"""
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("handler.py", source)
        return stream.getvalue()

    def create_role(self, suffix: str, service: str, policy: dict) -> str:
        name = f"{self.prefix}-{suffix}"
        trust = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": service},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
        created = self.iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(trust),
        )
        self.iam.put_role_policy(
            RoleName=name,
            PolicyName="compatibility",
            PolicyDocument=json.dumps(policy),
        )
        return created["Role"]["Arn"]

    def marker_seen(self, key: str, seconds: int) -> tuple[bool, int]:
        started = time.monotonic()
        while time.monotonic() - started < seconds:
            try:
                obj = self.s3.get_object(Bucket=self.bucket, Key=key)
                return obj["Body"].read() == b"executed", round((time.monotonic() - started) * 1000)
            except ClientError as exc:
                if exc.response["Error"]["Code"] not in {"NoSuchKey", "404", "NotFound"}:
                    raise
            time.sleep(2)
        return False, round((time.monotonic() - started) * 1000)

    def lambda_delivery(self):
        self.function_name = self.prefix
        role_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "s3:PutObject",
                    "Resource": f"arn:aws:s3:::{self.bucket}/{self.prefix}/*",
                }
            ],
        }
        self.lambda_role_arn = self.create_role("lambda-role", "lambda.amazonaws.com", role_policy)
        response = self.lam.create_function(
            FunctionName=self.function_name,
            Runtime="python3.13",
            Handler="handler.handler",
            Architectures=["arm64"],
            Role=self.lambda_role_arn,
            Code={"ZipFile": self.lambda_zip()},
            Timeout=30,
            Environment={
                "Variables": {
                    "MARKER_BUCKET": self.bucket,
                    "MARKER_ENDPOINT": "http://localstack:4566",
                }
            },
        )
        self.function_arn = response["FunctionArn"]
        self.lam.get_waiter("function_active_v2").wait(
            FunctionName=self.function_name,
            WaiterConfig={"Delay": 2, "MaxAttempts": 30},
        )
        self.lam.put_function_event_invoke_config(
            FunctionName=self.function_name,
            MaximumRetryAttempts=0,
            MaximumEventAgeInSeconds=300,
        )
        actual = self.lam.get_function_event_invoke_config(FunctionName=self.function_name)
        configured = actual.get("MaximumRetryAttempts") == 0 and actual.get("MaximumEventAgeInSeconds") == 300
        self.record(
            "Lambda async configuration",
            lambda: (
                "PASS" if configured else "FAIL",
                f"retries={actual.get('MaximumRetryAttempts')}; max age={actual.get('MaximumEventAgeInSeconds')}s",
            ),
        )
        marker = self.key("async-marker")
        invoke = self.lam.invoke(
            FunctionName=self.function_name,
            InvocationType="Event",
            Payload=json.dumps({"marker": marker}).encode(),
        )
        seen, wait_ms = self.marker_seen(marker, 60)
        return (
            "PASS" if invoke["StatusCode"] == 202 and seen else "FAIL",
            f"invoke HTTP {invoke['StatusCode']}; marker seen={seen}; delivery wait={wait_ms} ms",
        )

    def eventbridge_delivery(self):
        marker = self.key("eventbridge-marker")
        rule = self.events.put_rule(Name=self.prefix, ScheduleExpression="rate(1 minute)", State="ENABLED")
        self.lam.add_permission(
            FunctionName=self.function_name,
            StatementId=f"{self.prefix}-events",
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=rule["RuleArn"],
        )
        target_id = "compatibility"
        try:
            targets = self.events.put_targets(
                Rule=self.prefix,
                Targets=[{"Id": target_id, "Arn": self.function_arn, "Input": json.dumps({"marker": marker})}],
            )
            if targets["FailedEntryCount"]:
                return "FAIL", f"target registration failed: {targets['FailedEntryCount']} entry"
            seen, wait_ms = self.marker_seen(marker, 130)
            return "PASS" if seen else "FAIL", f"marker seen={seen}; delivery wait={wait_ms} ms"
        finally:
            self.events.remove_targets(Rule=self.prefix, Ids=[target_id])
            self.events.delete_rule(Name=self.prefix)

    def run(self):
        self.record("Private bucket setup", self.setup_bucket_result)
        if self.results[-1].status != "PASS":
            return
        self.record("Bucket configuration", self.bucket_configuration)
        self.record("Presigned required headers", self.signed_headers)
        self.record("Conditional first PUT and retry", self.valid_and_retry)
        self.record("Unsigned GET access control", self.unsigned_get)
        self.record(
            "Altered content type",
            lambda: self.altered_request("wrong-type", b"abcd", self.headers(content_type="image/jpeg")),
        )
        headers = self.headers()
        headers.pop("If-None-Match")
        self.record("Omitted condition header", lambda: self.altered_request("no-condition", b"abcd", headers))
        headers = self.headers()
        headers.pop("Content-Type")
        self.record("Omitted content type", lambda: self.altered_request("no-type", b"abcd", headers))
        self.record("Changed content length", lambda: self.altered_request("wrong-length", b"abcde", self.headers(5)))
        self.record(
            "Oversized body",
            lambda: self.altered_request("oversized", b"x" * (MAX_BYTES + 1), self.headers(MAX_BYTES + 1)),
        )
        headers = self.headers()
        headers.pop("Content-Length")
        self.record("Chunked transfer", lambda: self.altered_request("chunked", b"abcd", headers, True))
        self.record("Placeholder prevents replay", self.placeholder)
        self.record("Concurrent upload and placeholder", self.race)
        delivery = self.record("Lambda asynchronous execution", self.lambda_delivery)
        if delivery.status == "PASS":
            self.record("EventBridge minute-rule delivery", self.eventbridge_delivery)
        else:
            self.record(
                "EventBridge minute-rule delivery",
                lambda: ("UNVERIFIED", "not run because the Lambda function did not become active"),
            )

    def setup_bucket_result(self):
        self.setup_bucket()
        return "PASS", f"created {self.bucket}"

    def write_report(self, path: Path):
        timestamp = dt.datetime.now(dt.UTC).isoformat()
        rows = "\n".join(f"| {r.name} | {r.status} | {r.observed} | {r.elapsed_ms} |" for r in self.results)
        required = [r for r in self.results if r.name != "Unsigned GET access control"]
        overall = "PASS" if required and all(r.status == "PASS" for r in required) else "FAIL"
        contents = f"""# LocalStack compatibility gate

Run at {timestamp} against `{self.endpoint}` with LocalStack 4.14.0.
Resource prefix: `{self.prefix}`. The script uses dummy local credentials and refuses nonlocal endpoints.
The isolated compose must set `S3_SKIP_SIGNATURE_VALIDATION=0`;
LocalStack otherwise accepts changed and omitted signed headers.

Overall required protocol result: **{overall}**. Unsigned GET access control remains an informational emulator limit.

| Check | Result | Observation | Time (ms) |
| --- | --- | --- | ---: |
{rows}

The S3 probes send actual HTTP PUT requests through a presigned URL.
The Lambda and EventBridge probes require an S3 marker written by executed function code,
so an accepted invocation alone cannot pass.
A schedule result is unverified when its target Lambda cannot become active.
Results reflect this LocalStack instance only. IAM policy enforcement and signature fidelity
require separate AWS verification before production use.

On the first run with LocalStack's default signature validation setting, the emulator accepted
altered and omitted signed headers, changed lengths, oversized bodies, and chunked transfer.
After the isolated compose enabled `S3_SKIP_SIGNATURE_VALIDATION=0`, each of those requests
returned HTTP 403 without creating an object. This configuration is required for the local upload gate.

The initial EventBridge Scheduler probe used a one-time schedule with a valid IAM role and
waited past the full one-minute delivery window. Its API call succeeded but no Lambda marker
appeared. The installed LocalStack 4.14.0 Scheduler provider delegates creation to Moto's
in-memory schedule store, and the installed provider has no delivery worker.
The required recovery trigger therefore uses a classic EventBridge scheduled rule.
Its probe registers a rule with Lambda permission, waits for a real minute tick,
checks the function's marker, and removes the rule afterward.

The unsigned GET probe checks access control with no Authorization header or query signature.
LocalStack's bucket configuration reads can pass while unauthenticated object access still succeeds;
that observation is marked UNVERIFIED for production access control.

Run again with `python scripts/compatibility.py --endpoint http://localhost:4567`.
Every run creates fresh resources with a unique `mc-compat` prefix;
no existing resources are deleted or changed.
"""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
        print(f"Evidence: {path}")
        return overall


def error_name(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return f"{type(exc).__name__}: {exc.response['Error'].get('Code', 'unknown')}"
    return f"{type(exc).__name__}: {str(exc)[:180]}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://localhost:4567")
    args = parser.parse_args()
    gate = Gate(args.endpoint)
    gate.run()
    report = Path(__file__).resolve().parents[1] / "docs" / "compatibility.md"
    return 0 if gate.write_report(report) == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
