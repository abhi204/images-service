"""Verify scheduled recovery after simulated crashes between state writes and dispatch."""

from __future__ import annotations

import io
import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import boto3
import requests
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def main():
    config = json.loads((ROOT / ".local/api.json").read_text())
    for name in ("api_url", "endpoint_url"):
        url = urlparse(config[name])
        if url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1"}:
            raise ValueError("Recovery verification only accepts localhost HTTP endpoints")
    session = boto3.Session(
        aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1"
    )
    ddb = session.resource("dynamodb", endpoint_url=config["endpoint_url"])
    table = ddb.Table(config["image_table"])
    s3 = session.client("s3", endpoint_url=config["endpoint_url"])
    events = session.client("events", endpoint_url=config["endpoint_url"])
    schedule = events.describe_rule(Name=config["schedule_name"])
    assert schedule["State"] == "ENABLED"
    assert schedule["ScheduleExpression"] == "rate(5 minutes)"
    targets = events.list_targets_by_rule(Rule=config["schedule_name"])["Targets"]
    assert any(target["Arn"].endswith(":" + config["recovery_function"]) for target in targets)
    owner = "recovery-" + uuid.uuid4().hex[:12]
    output = io.BytesIO()
    Image.new("RGB", (12, 9), "blue").save(output, format="PNG")
    raw = output.getvalue()
    fixtures = {}
    now = int(time.time())
    due = (
        datetime.fromtimestamp(now - 1, UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    for wanted in ("ready", "expired", "deleted"):
        response = requests.post(
            config["api_url"] + "/images",
            headers={
                "X-User-Id": owner,
                "Idempotency-Key": uuid.uuid4().hex,
            },
            json={"filename": "recovery.png", "content_type": "image/png", "size_bytes": len(raw)},
            timeout=35,
        )
        assert response.status_code == 201, response.status_code
        upload = response.json()
        stored = requests.put(
            upload["upload_url"], headers=upload["upload_headers"], data=raw, timeout=30
        )
        assert stored.status_code == 200, stored.status_code
        image_id = upload["image_id"]
        values = {":due": now - 1, ":sort": due + "#" + image_id, ":uploading": "uploading"}
        update = "SET next_check_at=:due, maintenance_sort=:sort"
        names = {"#status": "status"}
        if wanted == "ready":
            update += ", #status=:processing, processing_deadline=:deadline"
            values.update({":processing": "processing", ":deadline": now + 1800})
        elif wanted == "expired":
            update += ", completion_deadline=:due"
        else:
            update += ", #status=:deleting, cleanup_pending=:yes"
            values.update({":deleting": "deleting", ":yes": True})
        table.update_item(
            Key={"image_id": image_id},
            UpdateExpression=update,
            ConditionExpression="#status=:uploading",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        fixtures[image_id] = wanted
    print(
        "Created three local crash fixtures. Waiting for the actual five-minute schedule; no manual invocation.",
        flush=True,
    )
    start = time.monotonic()
    last_report = start
    last_states = {}
    while time.monotonic() - start < 420:
        done = True
        for image_id, wanted in fixtures.items():
            record = table.get_item(Key={"image_id": image_id}, ConsistentRead=True)["Item"]
            last_states[image_id] = record["status"]
            if record["status"] != wanted or record.get("cleanup_pending"):
                done = False
                continue
            body = s3.get_object(Bucket=config["bucket_name"], Key="originals/" + image_id)["Body"]
            try:
                contents = body.read()
            finally:
                body.close()
            assert contents == (raw if wanted == "ready" else b"")
        if done:
            result = {
                "schedule": schedule["ScheduleExpression"],
                "elapsed_seconds": round(time.monotonic() - start, 2),
                "outcomes": last_states,
                "manual_worker_or_recovery_invocations": 0,
                "verified": [
                    "missed validation dispatch",
                    "expired upload cleanup",
                    "missed deletion dispatch",
                ],
            }
            (ROOT / ".local/recovery-result.json").write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps(result, indent=2), flush=True)
            return
        if time.monotonic() - last_report >= 30:
            print("Waiting for scheduled recovery: " + ", ".join(last_states.values()), flush=True)
            last_report = time.monotonic()
        time.sleep(2)
    raise TimeoutError(
        "Scheduled recovery did not converge within seven minutes: " + str(last_states)
    )


if __name__ == "__main__":
    main()
