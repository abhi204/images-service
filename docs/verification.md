# Verification results

These checks use the revised implementation and the isolated LocalStack 4.14.0 environment on localhost port 4567. They do not reuse results from the earlier draft. No real AWS account was deployed to.

## Completed checks

| Check | Measured result |
| --- | --- |
| `make lint` | Ruff passed |
| `make test` | 55 tests passed |
| `make compatibility` | All required protocol checks passed; EventBridge minute rule invoked Lambda after 60.4 seconds |
| `make integration` | 24 checks passed against the deployed API |
| `make recovery` | Fresh deployment recovered missed validation, expiration cleanup, and missed deletion dispatch in 209.73 seconds, with zero manual invocations |
| `make limits` | Exactly 20 MiB accepted; exactly 25 million pixels accepted; more than 25 million pixels rejected |
| OpenAPI document | YAML parsed and all 55 local references resolved |

The unit tests cover owner isolation, idempotency and conflicting retries, concurrent initiation, upload expiration, stale-worker fencing, validation failures, bounded processing attempts, cleanup backoff, interrupted storage reads, cursor expiry and filter binding, sparse-index pagination, authoritative gallery rechecks, and safe error responses. The deployment policy tests cover batched reads and the scheduled-rule permission.

The HTTP integration checks upload JPEG, PNG, and WebP through signed S3 requests, validate them through deployed Lambdas, browse by owner and date, paginate, download identical original bytes, reject malformed content, enforce owner-only operations, and complete deletion without allowing old upload URLs to recreate content.

The byte and pixel boundary checks completed in approximately 1.1 to 1.4 seconds each in this local environment. These times are observations, not production latency targets or proof of peak memory for every supported file.

## Scheduled application recovery

The first application-level five-minute recovery check did not converge within seven minutes. The three simulated interrupted operations remained unchanged. The rule fired, but LocalStack skipped delivery because the target input was the empty object `{}`. The target now supplies a nonempty recovery message. Existing interrupted records subsequently reached their expected terminal states on an actual timer tick. The fresh-environment check then passed on the actual five-minute rule. It recovered missed validation to `ready`, an abandoned upload to `expired`, and missed deletion dispatch to `deleted`. Original or empty stored bytes matched each outcome. The run completed in 209.73 seconds with zero manual worker or recovery invocations.

The first cold-restart bootstrap exceeded its 180-second Lambda startup wait. The worker later became active, and two successive bootstrap runs completed with the same API URL. No runtime error in the available INFO logs established the cause of that delay. The fresh deployment passed all 24 API integration checks.

## Limits of the evidence

- LocalStack returned HTTP 200 for an unsigned direct S3 GET despite private-bucket and public-access-block settings. AWS authorization enforcement remains unverified. The emulator is bound to localhost and uses test credentials.
- LocalStack's EventBridge Scheduler API stored schedules without executing them. The implementation uses an EventBridge scheduled rule instead. The automatic recovery interval and application contract remain five minutes.
- Unit tests use Moto and controlled failures. Integration tests use emulated AWS services. Neither establishes real AWS IAM enforcement, production throughput, or availability during service outages.
- Authentication is supplied by the surrounding platform. Local `X-User-Id` is a development identity only. Production accepts the trusted authorizer context.
- GitHub Actions runs lint and unit tests. Check the repository Actions page for the result of a particular commit; local integration and timed recovery remain separate commands.

See [compatibility details](compatibility.md), [the API contract](openapi.yaml), and [the approved plan](plan.md). Each local verification script records its latest measured output under ignored `.local/` files. Rerun the commands to obtain fresh evidence after changes.
