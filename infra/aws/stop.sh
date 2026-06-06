#!/usr/bin/env bash
# Stop the sermon.guide instance. Compute billing stops; you keep paying only
# EBS storage (~$8/mo for 100GB gp3) + the Elastic IP (~$3.65/mo). All data
# (Postgres, Milvus, uploads, model cache) lives on the EBS volume and
# survives. start.sh brings everything back on the same IP.

. "$(dirname "$0")/common.sh"

require_aws
instance_id="$(find_instance)"
[ -n "${instance_id}" ] || die "no instance found"

state="$(instance_state "${instance_id}")"
if [ "${state}" = "stopped" ]; then
  echo "already stopped"
  exit 0
fi

echo "stopping ${instance_id} …"
aws ec2 stop-instances --instance-ids "${instance_id}" >/dev/null
aws ec2 wait instance-stopped --instance-ids "${instance_id}"
echo "stopped ✓  (resting cost ≈ \$12/mo: EBS + Elastic IP. ./start.sh to resume)"
