#!/usr/bin/env bash
# Shared helpers for the sermon.guide AWS scripts. Source, don't execute.
#
# Conventions:
#   - Everything is found by tag, not by stored state: the instance, SG, EIP
#     and key pair all carry Name/Project=sermon-guide tags, so the scripts
#     are re-runnable from any checkout.
#   - Region/profile come from the ambient AWS CLI config (aws configure /
#     AWS_PROFILE / AWS_REGION); override per-invocation with env vars.

set -euo pipefail

TAG_NAME="${SERMON_AWS_NAME:-sermon-guide}"
KEY_NAME="${SERMON_AWS_KEY_NAME:-${TAG_NAME}}"
KEY_FILE="${SERMON_AWS_KEY_FILE:-${HOME}/.ssh/${TAG_NAME}.pem}"
SSH_USER="ubuntu"

aws() {
  command aws "$@"
}

region() {
  aws configure get region 2>/dev/null || echo "${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

require_aws() {
  command -v aws >/dev/null 2>&1 || die "aws CLI not found (expected on PATH, e.g. ~/.local/bin/aws)"
  aws sts get-caller-identity >/dev/null 2>&1 \
    || die "AWS credentials not configured — run: aws configure"
  [ -n "$(region)" ] || die "no default region — set one via aws configure or AWS_REGION"
}

# Newest non-terminated instance tagged Name=$TAG_NAME; empty if none.
find_instance() {
  aws ec2 describe-instances \
    --filters "Name=tag:Name,Values=${TAG_NAME}" \
              "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'sort_by(Reservations[].Instances[], &LaunchTime)[-1].InstanceId' \
    --output text 2>/dev/null | grep -v '^None$' || true
}

instance_state() {
  aws ec2 describe-instances --instance-ids "$1" \
    --query 'Reservations[0].Instances[0].State.Name' --output text
}

# Elastic IP tagged Name=$TAG_NAME; prints "ALLOC_ID IP" or nothing.
find_eip() {
  aws ec2 describe-addresses \
    --filters "Name=tag:Name,Values=${TAG_NAME}" \
    --query 'Addresses[0].[AllocationId,PublicIp]' --output text 2>/dev/null \
    | grep -v '^None' || true
}

find_security_group() {
  aws ec2 describe-security-groups \
    --filters "Name=group-name,Values=${TAG_NAME}-sg" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null \
    | grep -v '^None$' || true
}

ssh_cmd() {
  # accept-new = trust-on-first-use: conventional for a box we just created
  # ourselves, but the first connect is unauthenticated — paranoid operators
  # can pre-pin the host key from the EC2 console's system log.
  ssh -i "${KEY_FILE}" \
    -o StrictHostKeyChecking=accept-new \
    -o ConnectTimeout=10 \
    "${SSH_USER}@$1" "${@:2}"
}

wait_for_ssh() {
  local ip="$1" tries=0
  echo "waiting for SSH on ${ip} …"
  until ssh_cmd "${ip}" true 2>/dev/null; do
    tries=$((tries + 1))
    [ "${tries}" -lt 40 ] || die "SSH to ${ip} not reachable after ~3min"
    sleep 5
  done
}
