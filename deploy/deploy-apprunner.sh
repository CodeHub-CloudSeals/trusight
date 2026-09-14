#!/usr/bin/env bash
# TrustSight demo deploy: Dockerfile -> ECR -> App Runner.
#
# Run this from the repo root on a machine that has Docker (your Mac, or
# CloudShell if `docker info` works there).
#
#   chmod +x deploy/deploy-apprunner.sh
#   ./deploy/deploy-apprunner.sh
#
# Why App Runner and not Lambda: the API keeps run state in module-level
# dicts (_runs / _ctx in src/trustsight/api/main.py). One long-lived
# container is fine; Lambda's ephemeral containers are not. That is also why
# the service below is pinned to a SINGLE instance — see the autoscaling
# config at the bottom. Fix the state store before raising that.
set -euo pipefail

REGION="${AWS_REGION:-eu-west-2}"        # override: AWS_REGION=ap-southeast-2 ./deploy/...
APP="trustsight-demo"

# ── Preflight ────────────────────────────────────────────────────────────────
# Every check below is something that otherwise fails halfway through, after
# an ECR repo or an IAM role already exists. Fail here instead, out loud.
fail() { echo "PREFLIGHT FAIL: $*" >&2; exit 2; }

command -v aws >/dev/null    || fail "aws CLI not found"
command -v docker >/dev/null || fail "docker not found — build on a machine with Docker, or ask for the CodeBuild variant"
docker info >/dev/null 2>&1  || fail "docker is installed but the daemon is not running"

ACCOUNT="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)" \
  || fail "no valid AWS credentials for this shell"

aws apprunner list-services --region "$REGION" >/dev/null 2>&1 \
  || fail "App Runner is not reachable in ${REGION} (service may not exist in this region, or the role lacks apprunner:ListServices)"

[ -f Dockerfile ]    || fail "run this from the repo root (no Dockerfile here)"
[ -f .dockerignore ] || fail ".dockerignore missing — without it the macOS .venv goes into the image and pymupdf breaks"

git diff --quiet 2>/dev/null || echo "NOTE: uncommitted changes — the image is built from the working tree, the tag from the last commit"

ECR="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/${APP}"
TAG="$(git rev-parse --short HEAD 2>/dev/null || date +%s)"

echo "==> preflight OK"
echo "==> account ${ACCOUNT} / region ${REGION} / tag ${TAG}"

# 1. Repository ---------------------------------------------------------------
aws ecr describe-repositories --repository-names "$APP" --region "$REGION" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "$APP" --region "$REGION" \
       --image-scanning-configuration scanOnPush=true >/dev/null

aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"

# 2. Build and push -----------------------------------------------------------
# --platform is required when building on an Apple Silicon Mac.
docker build --platform linux/amd64 -t "${ECR}:${TAG}" -t "${ECR}:latest" .
docker push "${ECR}:${TAG}"
docker push "${ECR}:latest"

# 3. Access role App Runner uses to pull from ECR ------------------------------
ROLE="AppRunnerECRAccess-${APP}"
if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE" --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{"Effect":"Allow",
      "Principal":{"Service":"build.apprunner.amazonaws.com"},
      "Action":"sts:AssumeRole"}]}' >/dev/null
  aws iam attach-role-policy --role-name "$ROLE" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess
  echo "==> waiting for the new IAM role to propagate"; sleep 15
fi
ROLE_ARN="arn:aws:iam::${ACCOUNT}:role/${ROLE}"

# 4. Single-instance autoscaling ----------------------------------------------
# MaxSize 1 is load-bearing: in-memory run state does not survive a second
# instance, and a demo click would intermittently 404.
ASC_NAME="${APP}-single"
ASC_ARN="$(aws apprunner list-auto-scaling-configurations --region "$REGION" \
  --auto-scaling-configuration-name "$ASC_NAME" \
  --query 'AutoScalingConfigurationSummaryList[0].AutoScalingConfigurationArn' \
  --output text 2>/dev/null || echo None)"
if [ "$ASC_ARN" = "None" ] || [ -z "$ASC_ARN" ]; then
  ASC_ARN="$(aws apprunner create-auto-scaling-configuration --region "$REGION" \
    --auto-scaling-configuration-name "$ASC_NAME" \
    --max-concurrency 50 --min-size 1 --max-size 1 \
    --query 'AutoScalingConfiguration.AutoScalingConfigurationArn' --output text)"
fi

# 5. Create or update the service ---------------------------------------------
SVC_ARN="$(aws apprunner list-services --region "$REGION" \
  --query "ServiceSummaryList[?ServiceName=='${APP}'].ServiceArn | [0]" --output text)"

SOURCE=$(cat <<JSON
{"ImageRepository":{
  "ImageIdentifier":"${ECR}:${TAG}",
  "ImageRepositoryType":"ECR",
  "ImageConfiguration":{
    "Port":"8000",
    "RuntimeEnvironmentVariables":{"PYTHONPATH":"/app/src"}
  }},
 "AutoDeploymentsEnabled":false,
 "AuthenticationConfiguration":{"AccessRoleArn":"${ROLE_ARN}"}}
JSON
)

if [ "$SVC_ARN" = "None" ] || [ -z "$SVC_ARN" ]; then
  echo "==> creating service"
  SVC_ARN="$(aws apprunner create-service --region "$REGION" \
    --service-name "$APP" \
    --source-configuration "$SOURCE" \
    --instance-configuration '{"Cpu":"1 vCPU","Memory":"2 GB"}' \
    --health-check-configuration '{"Protocol":"HTTP","Path":"/health","Interval":10,"Timeout":5,"HealthyThreshold":1,"UnhealthyThreshold":5}' \
    --auto-scaling-configuration-arn "$ASC_ARN" \
    --query 'Service.ServiceArn' --output text)"
else
  echo "==> updating service"
  aws apprunner update-service --region "$REGION" \
    --service-arn "$SVC_ARN" --source-configuration "$SOURCE" >/dev/null
fi

echo "==> waiting for the service to go RUNNING (2-5 min)"
for _ in $(seq 1 60); do
  STATUS="$(aws apprunner describe-service --region "$REGION" --service-arn "$SVC_ARN" \
    --query 'Service.Status' --output text)"
  [ "$STATUS" = "RUNNING" ] && break
  [ "$STATUS" = "CREATE_FAILED" ] && { echo "FAILED - check App Runner logs"; exit 1; }
  sleep 15
done

URL="$(aws apprunner describe-service --region "$REGION" --service-arn "$SVC_ARN" \
  --query 'Service.ServiceUrl' --output text)"
echo
echo "  status : ${STATUS}"
echo "  demo   : https://${URL}"
echo "  health : https://${URL}/health"
