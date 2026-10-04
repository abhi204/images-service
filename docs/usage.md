# Use the local image API

Start the service with the commands in the [README](../README.md). Run the commands below from the repository root. Local requests use `X-User-Id` as a test identity. A production deployment must supply verified identity through an API Gateway authorizer and must not use `APP_ENV=local`.

## Upload an image

Create a sample PNG and initiate an upload as Alice:

```sh
.venv/bin/python - <<'PY'
import json
from pathlib import Path
from PIL import Image

Image.new('RGB', (32, 24), (30, 130, 180)).save('.local/sample.png')
Path('.local/upload-request.json').write_text(json.dumps({
    'filename': 'sample.png',
    'content_type': 'image/png',
    'size_bytes': Path('.local/sample.png').stat().st_size,
    'caption': 'Local demonstration',
}))
PY
API=$(.venv/bin/python -c 'import json; print(json.load(open(".local/api.json"))["api_url"])')
REQUEST_KEY=$(.venv/bin/python -c 'import uuid; print(uuid.uuid4())')
curl --fail-with-body -sS "$API/images" \
  -H 'X-User-Id: alice' -H "Idempotency-Key: $REQUEST_KEY" \
  -H 'Content-Type: application/json' \
  --data-binary @.local/upload-request.json > .local/upload.json
```

Send the file to the returned URL with the exact returned headers. The URL grants temporary upload permission; keep `.local/upload.json` private.

```sh
.venv/bin/python - <<'PY'
import json
from pathlib import Path
import requests

upload = json.loads(Path('.local/upload.json').read_text())
response = requests.put(upload['upload_url'], headers=upload['upload_headers'],
                        data=Path('.local/sample.png').read_bytes(), timeout=30)
assert response.status_code == 200, response.status_code
print('Uploaded bytes to S3')
PY
IMAGE_ID=$(.venv/bin/python -c 'import json; print(json.load(open(".local/upload.json"))["image_id"])')
curl --fail-with-body -sS -X POST "$API/images/$IMAGE_ID/complete" -H 'X-User-Id: alice'
curl --fail-with-body -sS "$API/images/$IMAGE_ID/status" -H 'X-User-Id: alice'
```

The upload URL lasts 15 minutes; request completion within 30 minutes of initiation. Completion returns `202` while validation runs. Repeat the status request until it reports `ready`, `rejected`, or `failed`. If the initiation response is lost, retry with the same key and identical request to recover the original image ID. Use a new key for a new upload.

## Browse, download, and delete

After the image is ready, public reads need no identity header:

```sh
curl --fail-with-body -sS "$API/images?owner_id=alice&limit=25"
curl --fail-with-body -sS "$API/images?owner_id=alice&date_from=2026-10-01&date_to=2026-11-01"
curl --fail-with-body -sS "$API/images/$IMAGE_ID"
curl --fail-with-body -sS -L "$API/images/$IMAGE_ID/content?disposition=attachment" -o .local/download.png
curl --fail-with-body -sS -X DELETE "$API/images/$IMAGE_ID" -H 'X-User-Id: alice'
curl --fail-with-body -sS "$API/images/$IMAGE_ID/status" -H 'X-User-Id: alice'
```

Replace the example dates with the interval you want. The start is inclusive and the end is exclusive; both refer to when validation completed. Results are newest first. To paginate, send `next_cursor` back as `cursor` with the same filters and page size. Continue until `next_cursor` is null, even if a page is empty.

Deletion returns `202` and blocks new public reads. Repeat the status request until it reports `deleted`; another deletion then returns `204`. Cleanup replaces the original bytes with an empty S3 object, preventing a previous conditional upload URL from recreating the image.

## Local startup and reset

Bootstrap waits up to 180 seconds for each Lambda to start. If a cold start exceeds that wait, inspect `docker compose logs localstack`, wait for the runtime image download, and rerun `make bootstrap`. A timeout does not mean setup succeeded.

LocalStack data may not persist across restarts. Rerun `make bootstrap` after restarting it. To discard this project's local data, run `docker compose down --volumes`, then `make up` and `make bootstrap`.
