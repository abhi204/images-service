"""Check the deployed permissions and recovery trigger contract."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import bootstrap_local as bootstrap  # noqa: E402


def test_role_policies_match_handler_access_patterns():
    image = "arn:aws:dynamodb:us-east-1:000000000000:table/images"
    requests = "arn:aws:dynamodb:us-east-1:000000000000:table/requests"
    bucket = "arn:aws:s3:::images"
    policies = {kind: bootstrap.role_policy(kind, image, requests, bucket) for kind in bootstrap.ROLE_NAMES}

    def resources_for(kind, action):
        return {
            resource
            for statement in policies[kind]["Statement"]
            if action in statement["Action"]
            for resource in (
                statement["Resource"] if isinstance(statement["Resource"], list)
                else [statement["Resource"]]
            )
        }

    assert resources_for("api", "dynamodb:BatchGetItem") == {image}
    assert resources_for("api", "dynamodb:Query") == {
        f"{image}/index/global_gallery", f"{image}/index/owner_gallery"
    }
    assert resources_for("recovery", "dynamodb:Query") == {f"{image}/index/maintenance"}
    assert resources_for("worker", "lambda:InvokeFunction") == {
        bootstrap.arn_for_function(bootstrap.WORKER_FUNCTION)
    }
    assert resources_for("api", "s3:PutObject") == {f"{bucket}/originals/*"}
    assert resources_for("worker", "s3:PutObject") == {f"{bucket}/originals/*"}
    api_put = next(
        statement for statement in policies["api"]["Statement"]
        if "s3:PutObject" in statement["Action"]
    )
    assert api_put["Condition"] == {"Null": {"s3:if-none-match": "false"}}
    for policy in policies.values():
        actions = {action for statement in policy["Statement"] for action in statement["Action"]}
        assert "s3:DeleteObject" not in actions
        assert "dynamodb:Scan" not in actions


def test_bootstrap_configures_worker_for_cleanup_dispatch(monkeypatch, tmp_path):
    (tmp_path / "function.zip").write_bytes(b"archive")
    monkeypatch.setattr(bootstrap, "LOCAL", tmp_path)
    monkeypatch.setattr(bootstrap, "cursor_secret", lambda: "test-secret")

    class Client:
        def put_function_event_invoke_config(self, **_kwargs):
            pass

    class Session:
        def client(self, *_args, **_kwargs):
            return Client()

    monkeypatch.setattr(bootstrap, "session", lambda _endpoint: Session())
    for name in (
        "ensure_bucket", "ensure_images_ttl_disabled", "ensure_ttl", "ensure_upload_policy",
        "ensure_log_group", "ensure_schedule",
    ):
        monkeypatch.setattr(bootstrap, name, lambda *_args: None)
    monkeypatch.setattr(bootstrap, "ensure_table", lambda *_args: "arn:table")
    monkeypatch.setattr(bootstrap, "ensure_role", lambda *_args: "arn:role")
    monkeypatch.setattr(bootstrap, "ensure_api", lambda *_args: "http://localhost:4567/api")
    environments = {}

    def capture_function(_client, name, _role, _handler, _timeout, _memory, _arch, env, _archive):
        environments[name] = env
        return f"arn:function:{name}"

    monkeypatch.setattr(bootstrap, "ensure_function", capture_function)
    bootstrap.bootstrap("http://localhost:4567", "arm64")

    assert environments[bootstrap.WORKER_FUNCTION]["WORKER_FUNCTION"] == bootstrap.WORKER_FUNCTION


def test_recovery_rule_has_lambda_target_and_permission():
    class Events:
        def __init__(self):
            self.rule = None
            self.targets = None
            self.put_rule_calls = 0
            self.put_targets_calls = 0

        def describe_rule(self, **_kwargs):
            if self.rule is None:
                raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "DescribeRule")
            return {"Arn": "arn:aws:events:us-east-1:000000000000:rule/recovery",
                    "ScheduleExpression": self.rule["ScheduleExpression"], "State": self.rule["State"]}

        def put_rule(self, **kwargs):
            self.rule = kwargs
            self.put_rule_calls += 1
            return {"RuleArn": "arn:aws:events:us-east-1:000000000000:rule/recovery"}

        def list_targets_by_rule(self, **_kwargs):
            return {"Targets": self.targets["Targets"] if self.targets else []}

        def put_targets(self, **kwargs):
            self.targets = kwargs
            self.put_targets_calls += 1
            return {"FailedEntryCount": 0}

    class Lambda:
        def __init__(self):
            self.permission = None

        def add_permission(self, **kwargs):
            self.permission = kwargs

    events, lambda_client = Events(), Lambda()
    recovery_arn = bootstrap.arn_for_function(bootstrap.RECOVERY_FUNCTION)
    bootstrap.ensure_schedule(events, lambda_client, recovery_arn)
    assert events.rule == {
        "Name": bootstrap.SCHEDULE,
        "ScheduleExpression": "rate(5 minutes)",
        "State": "ENABLED",
    }
    assert events.targets == {
        "Rule": bootstrap.SCHEDULE,
        "Targets": [{"Id": "recovery", "Arn": recovery_arn, "Input": '{"operation":"recover"}'}],
    }
    assert json.loads(events.targets["Targets"][0]["Input"])
    assert lambda_client.permission["Principal"] == "events.amazonaws.com"
    assert lambda_client.permission["SourceArn"] == "arn:aws:events:us-east-1:000000000000:rule/recovery"
    bootstrap.ensure_schedule(events, lambda_client, recovery_arn)
    assert events.put_rule_calls == 1
    assert events.put_targets_calls == 1
