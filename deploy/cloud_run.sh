#!/usr/bin/env bash
# Set the variables below, create secrets beforehand, then run this script.
set -euo pipefail
: "${PROJECT_ID:?required}"
: "${REGION:?required}"
: "${IMAGE:?Artifact Registry image URL required}"
: "${JOB_SERVICE_ACCOUNT:?required}"
: "${SCHEDULER_SERVICE_ACCOUNT:?required}"
: "${NOTION_DATABASE_ID:?required}"
: "${LLM_MODEL:?provider/model required}"
: "${LLM_KEY_ENV:?e.g. GEMINI_API_KEY}"
: "${LLM_SECRET:?Secret Manager secret name required}"
JOB_NAME="${JOB_NAME:-webclip-summarizer}"
SCHEDULE="${SCHEDULE:-0 * * * *}"
OPENAI_COMPATIBLE_BASE_URL="${OPENAI_COMPATIBLE_BASE_URL:-}"
ENV_VARS_DELIM="@"
if [[ -n "$OPENAI_COMPATIBLE_BASE_URL" ]]; then
  if [[ "$OPENAI_COMPATIBLE_BASE_URL" == *"$ENV_VARS_DELIM"* ]]; then
    echo "OPENAI_COMPATIBLE_BASE_URL must not contain '${ENV_VARS_DELIM}' (used as --set-env-vars delimiter)" >&2
    exit 1
  fi
  SET_ENV_VARS="^${ENV_VARS_DELIM}^NOTION_DATABASE_ID=${NOTION_DATABASE_ID}${ENV_VARS_DELIM}LLM_MODEL=${LLM_MODEL}${ENV_VARS_DELIM}TIMEZONE=Asia/Tokyo${ENV_VARS_DELIM}MARKDOWN_OUTPUT_DIR=${ENV_VARS_DELIM}NOTION_DATA_SOURCE_ID=${NOTION_DATA_SOURCE_ID:-}${ENV_VARS_DELIM}OPENAI_COMPATIBLE_BASE_URL=${OPENAI_COMPATIBLE_BASE_URL}"
else
  SET_ENV_VARS="NOTION_DATABASE_ID=$NOTION_DATABASE_ID,LLM_MODEL=$LLM_MODEL,TIMEZONE=Asia/Tokyo,MARKDOWN_OUTPUT_DIR=,NOTION_DATA_SOURCE_ID=${NOTION_DATA_SOURCE_ID:-}"
fi
gcloud builds submit --project "$PROJECT_ID" --tag "$IMAGE" .
gcloud run jobs deploy "$JOB_NAME" --project "$PROJECT_ID" --region "$REGION" \
  --image "$IMAGE" --service-account "$JOB_SERVICE_ACCOUNT" \
  --tasks 1 --parallelism 1 --max-retries 0 --task-timeout 3600s \
  --set-env-vars "$SET_ENV_VARS" \
  --set-secrets "NOTION_TOKEN=notion-token:latest,$LLM_KEY_ENV=$LLM_SECRET:latest"
gcloud run jobs add-iam-policy-binding "$JOB_NAME" --project "$PROJECT_ID" --region "$REGION" \
  --member "serviceAccount:$SCHEDULER_SERVICE_ACCOUNT" --role roles/run.invoker
gcloud scheduler jobs create http "$JOB_NAME" --project "$PROJECT_ID" --location "$REGION" \
  --schedule "$SCHEDULE" --time-zone Asia/Tokyo --http-method POST \
  --uri "https://run.googleapis.com/v2/projects/$PROJECT_ID/locations/$REGION/jobs/$JOB_NAME:run" \
  --oauth-service-account-email "$SCHEDULER_SERVICE_ACCOUNT"
