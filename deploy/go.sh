#!/usr/bin/env bash
# One command, from a clean Mac terminal, to the verified hosted demo.
#
#   ./deploy/go.sh
#
# It checks what the deploy needs, deploys, and then verifies the live URL
# against the go/no-go in docs/DEMO_RUNBOOK.md. Every failure stops with the
# exact next action rather than a stack trace, because this gets run under
# time pressure the day before a client session.
#
# It will not touch your AWS credentials. If they are missing it says so and
# stops: entering an access key is yours to do, in your own terminal.
set -uo pipefail

REGION="${AWS_REGION:-ap-southeast-2}"
APP="trustsight-demo"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mOK\033[0m    %s\n' "$*"; }
bad()  { printf '  \033[31mSTOP\033[0m  %s\n' "$*"; }
# Non-fatal findings get their own label. Printing STOP beside something the
# script then continues past teaches the reader to ignore the word STOP.
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$*"; }
info() { printf '        %s\n' "$*"; }

stop() { bad "$1"; shift; for l in "$@"; do info "$l"; done; exit 1; }

bold "TrustSight deploy — region ${REGION}"
echo

# ── 1. what the build needs ──────────────────────────────────────────────────
bold "1 / 5  Preflight"

command -v aws >/dev/null \
  || stop "the aws CLI is not installed" \
          "install it, then run this again:" \
          "  brew install awscli"

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  stop "this terminal has no valid AWS credentials" \
       "run this, and answer the four prompts:" \
       "  aws configure" \
       "" \
       "  AWS Access Key ID      your key, starting AKIA" \
       "  AWS Secret Access Key  your secret" \
       "  Default region name    ${REGION}" \
       "  Default output format  json" \
       "" \
       "no key yet: IAM -> Users -> naveen.m -> Security credentials" \
       "            -> Create access key" \
       "" \
       "then run ./deploy/go.sh again"
fi
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
ok "AWS account ${ACCOUNT}"

command -v docker >/dev/null \
  || stop "docker is not installed" \
          "install Docker Desktop, then run this again"

if ! docker info >/dev/null 2>&1; then
  stop "Docker is installed but the daemon is not running" \
       "start it:" \
       "  open -a Docker" \
       "" \
       "wait for the whale icon to settle in the menu bar, then run" \
       "./deploy/go.sh again"
fi
ok "docker daemon running"

aws apprunner list-services --region "$REGION" >/dev/null 2>&1 \
  || stop "App Runner is not reachable in ${REGION}" \
          "either the region is wrong or this role lacks apprunner:ListServices"
ok "App Runner reachable in ${REGION}"

# ── 2. what the image needs ──────────────────────────────────────────────────
# The corpus and the recorded run are git-ignored, so they are present only on
# a machine someone put them on. Building without them produces an image that
# starts cleanly and has no client drawings in it — the failure that looks
# fine in a script and is discovered on stage.
bold "2 / 5  Demo data in the build context"

PDFS="$(find data/corpus -name '*.pdf' 2>/dev/null | wc -l | tr -d ' ')"
if [ "$PDFS" -lt 2 ]; then
  stop "data/corpus holds ${PDFS} PDFs — the image would ship with no drawings" \
       "the hosted demo would show the seeded walkthrough only:" \
       "no Project 5, no reference reconciliation, no accuracy screen." \
       "" \
       "put the project folders directly under data/corpus/, each holding" \
       "its Input*.pdf and its Output*.pdf reference bar list."
fi
ok "${PDFS} drawing PDFs in data/corpus"

RECORDED="$(find data/fallback -name manifest.json 2>/dev/null | wc -l | tr -d ' ')"
if [ "$RECORDED" -lt 1 ]; then
  warn "no recorded run in data/fallback"
  info "the demo has no fallback if a live run fails mid-session."
  info "not fatal — continuing. To add one later:"
  info "  uvicorn trustsight.api.main:app --port 8000 &"
  info "  python scripts/freeze_run.py --project \"Project 5 - Atlanic Cages\""
  echo
else
  ok "${RECORDED} recorded run available as a fallback"
fi

[ -f Dockerfile ]    || stop "no Dockerfile here — run this from the repo root"
[ -f .dockerignore ] || stop ".dockerignore missing" \
                             "without it the macOS .venv enters the image and pymupdf breaks"
ok "Dockerfile and .dockerignore present"

if ! git diff --quiet 2>/dev/null; then
  warn "uncommitted changes"
  info "the image is built from the working tree; the tag comes from the last"
  info "commit, so the tag will not describe what is in the image."
  echo
fi
echo

# ── 3. deploy ────────────────────────────────────────────────────────────────
bold "3 / 5  Build and deploy"
info "this takes 5-10 minutes, most of it the first docker build"
echo
AWS_REGION="$REGION" ./deploy/deploy-apprunner.sh || stop "the deploy failed — see the output above"
echo

# ── 4. find the URL ──────────────────────────────────────────────────────────
bold "4 / 5  Live service"
URL="$(aws apprunner list-services --region "$REGION" \
        --query "ServiceSummaryList[?ServiceName=='${APP}'].ServiceUrl | [0]" \
        --output text 2>/dev/null)"
[ -n "$URL" ] && [ "$URL" != "None" ] \
  || stop "deployed, but the service URL could not be read back"
BASE="https://${URL}"
ok "$BASE"
echo

# ── 5. verify, against the runbook ───────────────────────────────────────────
bold "5 / 5  Go / no-go against the live URL"
FAIL=0

HEALTH="$(curl -s --max-time 20 "${BASE}/health" || true)"
case "$HEALTH" in
  *'"mode":"local"'*) ok "mode is local" ;;
  *'"mode":"cloud"'*) bad "mode is CLOUD — unset STATE_MACHINE_ARN and redeploy"; FAIL=1 ;;
  *) bad "/health did not answer: ${HEALTH:-no response}"; FAIL=1 ;;
esac

READY="$(curl -s --max-time 30 "${BASE}/ready" || true)"
check() {
  case "$READY" in
    *"$1"*) ok "$2" ;;
    *) bad "$2 — NOT satisfied"; FAIL=1 ;;
  esac
}
check '"ready": true'          "ready"
check '"blocking": []'         "nothing blocking"
check '"corpus_present": true' "corpus is in the image"
case "$READY" in
  *'"recorded_runs": []'*) bad "no recorded fallback in the image"; FAIL=1 ;;
  *'recorded-'*)           ok  "recorded fallback present" ;;
  *)                       bad "could not read recorded_runs"; FAIL=1 ;;
esac

echo
if [ -f deploy/smoke.py ]; then
  python3 deploy/smoke.py "$BASE" >/tmp/ts-smoke.log 2>&1 \
    && ok "smoke: all checks passed" \
    || { bad "smoke failed — see /tmp/ts-smoke.log"; FAIL=1; }
fi
if [ -f deploy/verify_ui.py ]; then
  python3 deploy/verify_ui.py "$BASE" >/tmp/ts-ui.log 2>&1 \
    && ok "interface: contrast, layout and console clean" \
    || { bad "interface check failed — see /tmp/ts-ui.log"; FAIL=1; }
fi

echo
if [ "$FAIL" -eq 0 ]; then
  bold "READY — demo at ${BASE}"
  info "still to do by hand, from docs/DEMO_RUNBOOK.md section 1:"
  info "  open the Value & accuracy screen and confirm no unearned saving"
  info "  load the recorded run once and confirm the amber banner"
  info "  rehearse the fifteen minutes twice"
else
  bold "NOT READY — fix the items marked STOP above, then run this again"
  exit 1
fi
