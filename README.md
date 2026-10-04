# MontyCloud image service

A Python API for original-image uploads, public browsing and downloads, and owner-authorized deletion. It uses API Gateway, Lambda, private S3 storage, and DynamoDB. An EventBridge rule recovers unfinished work every five minutes. All ready images are public.

## Run locally

Requires Python 3.13 and Docker Compose.

```sh
make setup
make up
make compatibility
make bootstrap
make integration
```

Bootstrap writes the API URL to `.local/api.json`. The integration command exercises the deployed API. Run `make lint` and `make test` for fast checks. Run `make recovery`, `make limits`, or `make load` for longer checks. Recovery waits for the five-minute schedule and can take up to seven minutes. Run `make down` to stop LocalStack.

Run `make demo` after bootstrap and open the printed URL to use the local browser interface. The page lets you upload, browse, download, and delete images with a test user ID.

## Use the API

1. Start an upload with `POST /images` and an `Idempotency-Key`.
2. PUT the original file to the returned S3 URL using the returned headers.
3. Call `POST /images/{id}/complete` and poll the owner's status endpoint.
4. List ready images with `GET /images`, optionally filtering by owner or ready date.

Anyone can view or download a ready image. Only its owner can delete it.

The local API uses `X-User-Id` to simulate identity. See the [manual walkthrough](docs/usage.md) for commands and the [OpenAPI specification](docs/openapi.yaml) for request and response details.

## Limits and verification

The service accepts nonanimated JPEG, PNG, and WebP files up to 20 MiB and 25 million pixels. It preserves the original bytes. Deletion hides an image from new public reads immediately and finishes storage cleanup asynchronously.

See [verification results](docs/verification.md) for measured checks and limitations, and the [design plan](docs/plan.md) for concurrency and recovery decisions. LocalStack results do not establish production AWS access control or capacity.
