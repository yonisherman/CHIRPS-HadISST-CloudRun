# CHIRPS and HadISST Cloud Run downloaders

Two production jobs replace CHIRPS's ERDDAP `cacheFromUrl` downloads and Roy's periodic HadISST SST/ice downloads. They write into the existing production prefixes, validate NetCDF files before publishing, and request ERDDAP reloads after changed data. The ERDDAP rclone mount remains read-only.

Based on [erd-coastwatch/cloud-run-template](https://github.com/erd-coastwatch/cloud-run-template), commit `7f676b9c67700f3aa6e63b66e75df215b2475ca4`. Uses the template Dockerfile, `deploy_job.sh`, `entrypoint.sh`, `config/`, `scripts/`, `src/`, and Docker Buildx / dedicated Artifact Registry deployment flow. Template LICENSE retained.

Validation on October 13: nine automated tests passed; shell syntax and Python compilation passed; the narrow XML candidate retained dataset identities and removed exactly four cache tags. A real Met Office ice archive downloaded, decompressed, and passed validation with 1,879 time records from January 1870 through July 2026. Production GCS writes, authenticated flags and Docker execution still require verification in your environment.

## Jobs and schedules

| Job | Datasets | Default check |
|---|---|---|
| `chirps-downloader` | annual, daily, pentad and monthly CHIRPS v2 | Tuesday 08:00 America/Los_Angeles |
| `hadisst-downloader` | `erdHadISST` and `erdHadISSTIce` | Tuesday 09:00 America/Los_Angeles |

Weekly is a chosen checking interval, not an asserted upstream publication schedule. The daily and pentad labels describe temporal resolution; their source files are yearly aggregates. On October 13, 2026, current daily/pentad and the monthly aggregate had September 11 modification dates; annual had May 15. HadISST full files had September 3 HTTP Last-Modified dates, and Roy confirmed irregular releases. Weekly checks should detect changes within a week without redownloading unchanged aggregates.

Source URLs and exact filename patterns come from Roy's October 1 XML and match the October 7 cloud XML. Only root-directory daily/pentad annual files and the exact combined monthly/annual files are selected. `by_month/` and `byYear/` subdirectories are excluded intentionally.

HadISST decomposition remains Roy's manual process. LISIRD is retained as historical data. Neither is processed by these jobs. NAVGEM 1° remains waiting on the delivery path/upstream sources.

## Output paths

| Dataset | Production prefix |
|---|---|
| `chirps20GlobalDailyP05` | `gs://erddap-swfsc-erd/points/chirps20/globalDailyP05/` |
| `chirps20GlobalPentadP05` | `gs://erddap-swfsc-erd/points/chirps20/globalPentadP05/` |
| `chirps20GlobalMonthlyP05` | `gs://erddap-swfsc-erd/points/chirps20/globalMonthlyP05/` |
| `chirps20GlobalAnnualP05` | `gs://erddap-swfsc-erd/points/chirps20/globalAnnualP05/` |
| `erdHadISST` | `gs://erddap-swfsc-erd/Hadley/HadISST_sst.nc` |
| `erdHadISSTIce` | `gs://erddap-swfsc-erd/Hadley/HadISST_ice.nc` |

HadISST downloads the full `.nc.gz` URLs and writes decompressed `.nc` files. If a same-name `.nc.gz` object already exists in the destination, the job stops that product so it cannot introduce a duplicate matching ERDDAP's filename regex. Inspect that existing object before removing or moving anything.

## Change detection and first execution

Each selected source is checked with HTTP HEAD. Successful validated comparisons/publications record source URL, ETag, Last-Modified and Content-Length in the destination object's custom metadata. Future executions skip unchanged sources. If a server supplies neither ETag nor Last-Modified, the file is downloaded and compared every time.

**First execution downloads every eligible source file once**, including historical CHIRPS files, to compare its actual MD5 with the existing GCS object and establish the source fingerprint. This may transfer roughly 100 GB and take hours. It does not upload identical content. It processes one file at a time, removes temporary files between downloads, and keeps completed fingerprints if the job later fails, so a rerun resumes without downloading already-compared objects. No unverified size-only adoption is performed.

CHIRPS uses 4 CPUs / 16 GiB, because the combined monthly file is approximately 7.2 GiB and Cloud Run's writable filesystem counts toward memory. A 12 GiB temporary-file guard leaves headroom; increase memory and the guard together if source files grow. The first-run timeout is 24 hours. HadISST uses 2 CPUs / 4 GiB and a two-hour timeout. Existing bucket files are never deleted.

Downloads must be nonempty, complete relative to Content-Length when supplied, and open successfully as NetCDF. Time, latitude, longitude and the expected data variable must exist. Coordinate axes must be finite and monotonic. A known source time-coverage regression is rejected once a baseline has been established. Content hashes are calculated while streaming from disk. GCS uploads/metadata changes use generation preconditions. A per-suite GCS lock prevents overlapping writers; an abandoned lock expires after 25 hours, longer than the longest deployed task timeout.

Reload intent and per-suite locks are stored under `operations/workflow-state/source-downloads/` before each changed upload. If flagging fails, the execution fails and the next run retries the flag even if no additional files changed. ERDDAP flags are sent after each product's attempted processing; if one product partially succeeds, its published changes can still be reloaded while the overall job reports failure. A successful flag acknowledges a reload request; it does not prove served coverage.

## Local source plan and tests

From the repository directory on your Mac:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r config/requirements.txt
python scripts/workflow.py --suite chirps --plan-only
python scripts/workflow.py --suite hadisst --plan-only
python -m unittest discover -s tests -v
```

`--plan-only` lists exact source matches and HEAD metadata. It performs no GCS writes, needs no Google credentials, and sends no ERDDAP flags. It can take a few minutes for the full CHIRPS archive.

For an isolated GCS test bucket use `--bucket YOUR_TEST_BUCKET --no-flags`; do not use `--no-flags` for the routine production execution. `--force` downloads again even when fingerprints match, still validating and comparing before publishing.

## Deployment on your Mac

The deployment script is **plan-only by default**. With `--deploy`, it builds locally with Docker Buildx, pushes to dedicated Artifact Registry repo `source-downloads-cloudrun`, deploys the selected job, grants its invoker and flag-secret access, and creates/updates Scheduler. It does not execute the job. New schedules are paused. Existing schedules retain their state when updated.

Defaults:

- Project: `ggn-nmfs-swfscerddap-dev-1`
- Region: `us-east4`
- Runtime/invoker service account: `swfscerddap-run-sa1@ggn-nmfs-swfscerddap-dev-1.iam.gserviceaccount.com`
- Production bucket: `erddap-swfsc-erd`
- Image: `source-downloads:v0.1.0`
- Temporary ERDDAP endpoint: `https://155.206.244.135/erddap`, TLS verification disabled to match the existing IP-endpoint setup. Override `ERDDAP_BASE_URL` and `ERDDAP_VERIFY_TLS=true` once a trusted hostname is ready.

The runtime account needs object read/write permissions on the output bucket (including `operations/workflow-state/`). The existing project service account is reused; the script does not silently grant new bucket/project-wide permissions. It needs `roles/run.invoker` for Scheduler invocation and accessor access to the corresponding **regional** flag secret. The deploying user needs the existing Artifact Registry, Cloud Run, Scheduler, service-account-use and regional Secret Manager permissions.

Before deploying a version that changes `STATE_PREFIX`, pause each suite's scheduler and wait for any running execution to finish. Copy its existing state to the new path, then deploy and run a verification execution. Keep the old prefix until the new run succeeds:

```bash
for suite in chirps hadisst; do
  gcloud scheduler jobs pause "${suite}-weekly-schedule" \
    --project=ggn-nmfs-swfscerddap-dev-1 --location=us-east4
  gcloud storage rsync --recursive \
    "gs://erddap-swfsc-erd/_workflow_state/source-downloads/${suite}/" \
    "gs://erddap-swfsc-erd/operations/workflow-state/source-downloads/${suite}/"
done
```

The deployment script leaves an existing paused schedule paused. Resume each schedule after verifying a successful run.

Create the two regional flag secrets using your current valid per-dataset ERDDAP flag keys. The helper prompts without echoing keys and passes JSON directly to gcloud; it stores no local key file. It does not generate or change ERDDAP keys.

```bash
python scripts/create_flag_secret.py chirps
python scripts/create_flag_secret.py hadisst

./deploy_job.sh chirps
./deploy_job.sh hadisst

./deploy_job.sh chirps --deploy
SKIP_BUILD=true ./deploy_job.sh hadisst --deploy
```

`SKIP_BUILD=true` reuses the image just pushed by the first deployment. Both jobs use the same image, with different `JOB_MODE` values, secrets and resources. Regional secrets are `chirps-erddap-flag-keys` and `hadisst-erddap-flag-keys`. The runtime reads them directly through the `us-east4` Secret Manager API using its service-account identity. Cloud Run volume mounts do not support regional secrets. Local runs may instead use `ERDDAP_FLAG_JSON=/absolute/path/to/private.json`. Secret JSON is `{ "datasetID": "flag-key", ... }`. The runtime also accepts a top-level `flagKeys` mapping and nested `{ "flagKey": "..." }` values.

## First production executions

```bash
gcloud run jobs execute chirps-downloader \
  --project=ggn-nmfs-swfscerddap-dev-1 --region=us-east4

gcloud run jobs execute hadisst-downloader \
  --project=ggn-nmfs-swfscerddap-dev-1 --region=us-east4
```

Monitor Cloud Logging for `Published`, `Content unchanged`, `Unchanged source`, and `ERDDAP flag acknowledged`. Do not interpret a started execution as success. Verify the execution reaches success and sample each dataset's `.das` and time axis. If old on-prem → production sync includes these prefixes, exclude them from that producer before enabling the new schedules to avoid writers competing over the same objects.

The template-based package is built and tested locally; it has not been deployed to your GCP project or authenticated against production GCS/ERDDAP from the authoring environment.

## CHIRPS XML cutover on erd-erddap

Only after the CHIRPS execution succeeds, remove `cacheFromUrl` from the four file-backed bases, preserving all paths, metadata, variables and wrappers. Run against a **fresh live XML copy**, not the supplied October 7 snapshot.

Copy `scripts/remove_chirps_cache.py` to your VM working directory, then on `erd-erddap`:

```bash
cd ~/dataset_import
sudo cp /usr/local/tomcat_erddap/content/erddap/datasets.xml chirps-live-before.xml
sudo python3 remove_chirps_cache.py chirps-live-before.xml chirps-candidate.xml
sudo diff -u chirps-live-before.xml chirps-candidate.xml
```

The helper produces a candidate only. It rejects a missing/duplicate target or unexpected dataset changes. Review the diff: exactly four cache tags should be removed. Install using the existing backup/reload workflow:

```bash
sudo cp -p /usr/local/tomcat_erddap/content/erddap/datasets.xml \
  "/usr/local/tomcat_erddap/content/erddap/datasets.xml.bak.$(date -u +%Y%m%dT%H%M%SZ)"
sudo cp chirps-candidate.xml /usr/local/tomcat_erddap/content/erddap/datasets.xml
for id in chirps20GlobalDailyP05 chirps20GlobalPentadP05 chirps20GlobalMonthlyP05 chirps20GlobalAnnualP05; do
  sudo -u tomcat touch "/mnt/disks/erddap-data/erddap/hardFlag/$id"
done
```

Verify all four cloud bases after reload. Their `_Lon0360` wrappers own no output files and retain the canonical hostname through cutover. Flag wrappers only if their served axes stay stale after base reload.

## Enable weekly schedules after verification

On your Mac:

```bash
gcloud scheduler jobs resume chirps-weekly-schedule \
  --project=ggn-nmfs-swfscerddap-dev-1 --location=us-east4
gcloud scheduler jobs resume hadisst-weekly-schedule \
  --project=ggn-nmfs-swfscerddap-dev-1 --location=us-east4
```

After the first unattended runs, check job success, object freshness and served ERDDAP coverage. Mark these pipelines complete in the tracker only after deployment and service verification.
