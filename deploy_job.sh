#!/bin/bash
# Adapted from erd-coastwatch/cloud-run-template (Docker Buildx -> AR -> Job -> Scheduler).
set -euo pipefail
cd "$(dirname "$0")"
SUITE="${1:?Usage: ./deploy_job.sh chirps|hadisst [--deploy]}"
MODE="${2:---plan}"
[[ "$MODE" == '--plan' || "$MODE" == '--deploy' ]] || { echo 'Use --plan or --deploy'; exit 2; }
PROJECT_ID="${PROJECT_ID:-ggn-nmfs-swfscerddap-dev-1}"
REGION="${REGION:-us-east4}"
REPO_NAME="${REPO_NAME:-source-downloads-cloudrun}"
IMAGE_NAME="${IMAGE_NAME:-source-downloads}"
TAG="${TAG:-v0.1.0}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-swfscerddap-run-sa1@${PROJECT_ID}.iam.gserviceaccount.com}"
PLATFORM="${PLATFORM:-linux/amd64}"
IMAGE_PATH="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/${IMAGE_NAME}:${TAG}"
OUTPUT_BUCKET="${OUTPUT_BUCKET:-erddap-swfsc-erd}"
STATE_PREFIX="${STATE_PREFIX:-operations/workflow-state/source-downloads}"
ERDDAP_BASE_URL="${ERDDAP_BASE_URL:-https://155.206.244.135/erddap}"
# Temporary IP endpoint compatibility, matching the existing project jobs.
ERDDAP_VERIFY_TLS="${ERDDAP_VERIFY_TLS:-false}"
case "$SUITE" in
 chirps) CPU=4; MEMORY=16Gi; JOB_TIMEOUT=86400s; SCHEDULE='0 8 * * 2'; MAX_LOCAL_BYTES=12884901888 ;;
 hadisst) CPU=2; MEMORY=4Gi; JOB_TIMEOUT=7200s; SCHEDULE='0 9 * * 2'; MAX_LOCAL_BYTES=2147483648 ;;
 *) echo 'Unknown suite'; exit 2 ;;
esac
JOB_NAME="${SUITE}-downloader"
SCHEDULER_NAME="${SUITE}-weekly-schedule"
FLAG_SECRET="${FLAG_SECRET:-${SUITE}-erddap-flag-keys}"
printf 'Suite: %s\nProject: %s\nImage: %s\nJob: %s (%s CPU, %s, timeout %s)\nSchedule: %s America/Los_Angeles\nRegional flag secret: %s\n' \
 "$SUITE" "$PROJECT_ID" "$IMAGE_PATH" "$JOB_NAME" "$CPU" "$MEMORY" "$JOB_TIMEOUT" "$SCHEDULE" "$FLAG_SECRET"
[[ "$MODE" == '--deploy' ]] || { echo 'Plan only. Add --deploy to build, push and deploy.'; exit 0; }
need() { command -v "$1" >/dev/null || { echo "Missing: $1"; exit 1; }; }
need gcloud; need docker
# Check the secret before starting an image build.
gcloud secrets describe "$FLAG_SECRET" --project "$PROJECT_ID" --location "$REGION" >/dev/null
gcloud secrets add-iam-policy-binding "$FLAG_SECRET" --project "$PROJECT_ID" --location "$REGION" \
 --member "serviceAccount:${SERVICE_ACCOUNT}" --role roles/secretmanager.secretAccessor >/dev/null
if ! gcloud artifacts repositories describe "$REPO_NAME" --project "$PROJECT_ID" --location "$REGION" >/dev/null 2>&1; then
 gcloud artifacts repositories create "$REPO_NAME" --project "$PROJECT_ID" --location "$REGION" --repository-format docker
fi
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
if [[ "${SKIP_BUILD:-false}" != true ]]; then
 docker buildx build --platform "$PLATFORM" -t "${IMAGE_NAME}:${TAG}" --load .
 docker tag "${IMAGE_NAME}:${TAG}" "$IMAGE_PATH"
 docker push "$IMAGE_PATH"
fi
gcloud run jobs deploy "$JOB_NAME" --project "$PROJECT_ID" --region "$REGION" \
 --image "$IMAGE_PATH" --service-account "$SERVICE_ACCOUNT" --tasks 1 --parallelism 1 \
 --max-retries 0 --task-timeout "$JOB_TIMEOUT" --memory "$MEMORY" --cpu "$CPU" \
 --set-env-vars "JOB_MODE=${SUITE},OUTPUT_BUCKET=${OUTPUT_BUCKET},STATE_PREFIX=${STATE_PREFIX},ERDDAP_BASE_URL=${ERDDAP_BASE_URL},ERDDAP_VERIFY_TLS=${ERDDAP_VERIFY_TLS},ERDDAP_FLAG_SECRET=projects/${PROJECT_ID}/locations/${REGION}/secrets/${FLAG_SECRET}/versions/latest,MAX_LOCAL_BYTES=${MAX_LOCAL_BYTES}"
gcloud run jobs add-iam-policy-binding "$JOB_NAME" --project "$PROJECT_ID" --region "$REGION" \
 --member "serviceAccount:${SERVICE_ACCOUNT}" --role roles/run.invoker >/dev/null
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
URI="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_NUMBER}/jobs/${JOB_NAME}:run"
if gcloud scheduler jobs describe "$SCHEDULER_NAME" --project "$PROJECT_ID" --location "$REGION" >/dev/null 2>&1; then
 gcloud scheduler jobs update http "$SCHEDULER_NAME" --project "$PROJECT_ID" --location "$REGION" \
  --schedule "$SCHEDULE" --time-zone America/Los_Angeles --uri "$URI" --http-method POST \
  --oauth-service-account-email "$SERVICE_ACCOUNT" --oauth-token-scope https://www.googleapis.com/auth/cloud-platform \
  --update-headers Content-Type=application/json --message-body '{}'
else
 gcloud scheduler jobs create http "$SCHEDULER_NAME" --project "$PROJECT_ID" --location "$REGION" \
  --schedule "$SCHEDULE" --time-zone America/Los_Angeles --uri "$URI" --http-method POST \
  --oauth-service-account-email "$SERVICE_ACCOUNT" --oauth-token-scope https://www.googleapis.com/auth/cloud-platform \
  --headers Content-Type=application/json --message-body '{}'
 gcloud scheduler jobs pause "$SCHEDULER_NAME" --project "$PROJECT_ID" --location "$REGION"
fi
echo 'Deployment complete. New schedules are paused until the first execution is verified.'
printf 'gcloud run jobs execute %s --project=%s --region=%s\n' "$JOB_NAME" "$PROJECT_ID" "$REGION"
