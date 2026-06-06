#!/usr/bin/env bash
# PERMANENTLY tear down the sermon.guide AWS footprint: instance (and its
# EBS volume — Postgres, Milvus, uploads, everything), Elastic IP, security
# group. The key pair is kept unless --delete-key is passed.
#
# There are no backups unless you made a snapshot first:
#   aws ec2 create-snapshot --volume-id <vol> --description sermon-pre-destroy

. "$(dirname "$0")/common.sh"

require_aws
instance_id="$(find_instance)"
eip_info="$(find_eip)"
sg_id="$(find_security_group)"

echo "will destroy:"
echo "  instance : ${instance_id:-none} (+ its EBS volume — ALL DATA)"
echo "  eip      : ${eip_info:-none}"
echo "  sg       : ${sg_id:-none}"
printf 'type "destroy sermon-guide" to confirm: '
read -r confirm
[ "${confirm}" = "destroy sermon-guide" ] || die "aborted"

if [ -n "${instance_id}" ]; then
  aws ec2 terminate-instances --instance-ids "${instance_id}" >/dev/null
  echo "terminating ${instance_id} …"
  aws ec2 wait instance-terminated --instance-ids "${instance_id}"
fi

if [ -n "${eip_info}" ]; then
  alloc_id="$(echo "${eip_info}" | awk '{print $1}')"
  aws ec2 release-address --allocation-id "${alloc_id}"
  echo "released EIP"
fi

if [ -n "${sg_id}" ]; then
  aws ec2 delete-security-group --group-id "${sg_id}"
  echo "deleted security group"
fi

if [ "${1:-}" = "--delete-key" ]; then
  aws ec2 delete-key-pair --key-name "${KEY_NAME}"
  rm -f "${KEY_FILE}"
  echo "deleted key pair + ${KEY_FILE}"
fi

echo "destroyed ✓ (billing for this stack is now \$0)"
