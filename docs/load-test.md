# Bounded local load test

Run on October 3, 2026 at 06:55 UTC against the isolated LocalStack 4.14.0 deployment. The Docker environment exposed 10 CPUs and 8,218,034,176 bytes (7.65 GiB) of memory on a shared aarch64 machine. The worker Lambda was configured for a 120-second timeout and 1,024 MiB of memory. This is one local emulator observation, not an AWS capacity limit.

The [measured aggregate report](load-test-result.json) retains the per-operation counts and latency distributions. It omits upload IDs, owner IDs, idempotency keys, and presigned URLs. The local runner writes its full report under `.local/` for inspection after each run.

Run `make setup`, `make up`, and `make bootstrap` as described in the [README](../README.md), then run `make load`. The script writes the full measured report to ignored `.local/load-result.json` and exits nonzero if a stage or cleanup fails. It uses localhost endpoints and test credentials only.

## Workload

The script ran three stages with 1, 3, and 5 concurrent upload clients. Each stage had three waves, for 27 images total. A wave completed validation before the next began, so at most five images awaited validation at a time. Two clients browsed the anonymous global gallery and the test owner's gallery at a maximum of one request per second each while uploads were submitted. Browsing stopped after the final wave's submission in each stage; the script measured how long remaining validation took to finish.

The generated original PNGs were 256 × 256 pixels (197,174 bytes) and 1,024 × 1,024 pixels (3,150,970 bytes). Requests used a fresh owner and idempotency key for each image. Each initiation was replayed to check that the server returned the same image and deadlines. The script then uploaded with the signed URL, requested completion, polled until ready, checked owner-gallery membership without duplicates, and compared downloaded bytes with the originals. Cleanup deleted only this run's images in groups of at most five and verified an empty object remained for each one.

## Measured results

| Concurrent upload clients | Images ready | Stage time | Validation window | Ready completions per second | Completion to ready p95 | Initiation request p95 | Pending on first poll after traffic stopped | Final drain |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 3/3 | 5.873 s | 5.749 s | 0.522 | 3.161 s | 67 ms | 1 | 1.054 s |
| 3 | 9/9 | 7.738 s | 7.276 s | 1.237 | 3.203 s | 3,416 ms | 2 | 1.137 s |
| 5 | 15/15 | 8.038 s | 7.399 s | 2.027 | 2.394 s | 2,071 ms | 2 | 2.377 s |

The ready-completion rate divides ready images by the validation window. That window includes three sequential waves and waiting for each wave to become ready; stage time also includes the final download and gallery checks. The rate is **not** a sustained arrival-rate or maximum-capacity measurement. The first-poll count is a snapshot after traffic stopped, not an exact count at the stop instant. The p95 figures use nearest rank; with only 3–15 samples per stage, p95 is the largest observed value or close to it. Initiation had individual outliers of 2–3.4 seconds at the higher levels. We did not isolate their cause, and the fixed ascending run order can include warmup effects.

All stages had zero unexpected HTTP or transport failures. The test recorded 27 attempts, 27 distinct image IDs, 27 ready images, matching downloaded bytes, complete owner-gallery membership, and no pending validation at stage end. Cleanup returned `deleted` for all 27 records and confirmed all 27 S3 keys held zero bytes. The full run, including cleanup, took 29.319 seconds.

## What this establishes

This workload found no loss, duplication, failed validation, or unfinished cleanup at up to five concurrent upload clients in this local run. It also exposed slow individual requests, which the aggregate pass result alone would hide. It does not establish how many users the service can support before failure. The client counts are small, the stages were short, the image mix was limited, and LocalStack performance depends on this machine and other processes using it. Real AWS throughput, quotas, IAM behavior, and sustained or geographically distributed traffic need separate measurement. The earlier [verification report](verification.md) records the API, protocol, and timed recovery checks.
