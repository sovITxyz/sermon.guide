#!/usr/bin/env bash
# Start the (stopped) sermon.guide instance. The Elastic IP and all docker
# volumes persist across stop/start; every service has restart:
# unless-stopped, so the whole stack comes back on its own (~2-3min until
# Milvus is healthy).

. "$(dirname "$0")/common.sh"

require_aws
instance_id="$(find_instance)"
[ -n "${instance_id}" ] || die "no instance found — run provision.sh"

state="$(instance_state "${instance_id}")"
if [ "${state}" = "running" ]; then
  echo "already running"
else
  echo "starting ${instance_id} (was: ${state}) …"
  aws ec2 start-instances --instance-ids "${instance_id}" >/dev/null
  aws ec2 wait instance-running --instance-ids "${instance_id}"
fi

eip_info="$(find_eip)"
ip="$(echo "${eip_info}" | awk '{print $2}')"
echo "running ✓  https://${ip}  (give the stack ~2-3min; compute billing is on)"
