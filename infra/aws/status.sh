#!/usr/bin/env bash
# Show the sermon.guide instance state, address, and rough cost posture.

. "$(dirname "$0")/common.sh"

require_aws
instance_id="$(find_instance)"
if [ -z "${instance_id}" ]; then
  echo "no instance provisioned (run provision.sh)"
  exit 0
fi

aws ec2 describe-instances --instance-ids "${instance_id}" \
  --query 'Reservations[0].Instances[0].{id:InstanceId,state:State.Name,type:InstanceType,az:Placement.AvailabilityZone,launched:LaunchTime}' \
  --output table

eip_info="$(find_eip)"
if [ -n "${eip_info}" ]; then
  ip="$(echo "${eip_info}" | awk '{print $2}')"
  echo "elastic ip : ${ip}"
  echo "url        : https://${ip}"
  echo "ssh        : ssh -i ${KEY_FILE} ${SSH_USER}@${ip}"
fi

state="$(instance_state "${instance_id}")"
case "${state}" in
  running) echo "billing    : compute ON (~\$0.15/hr for t3a.xlarge) + EBS + EIP" ;;
  stopped) echo "billing    : compute OFF — EBS (~\$8/mo) + EIP (~\$3.65/mo) only" ;;
  *)       echo "billing    : transitional (${state})" ;;
esac
