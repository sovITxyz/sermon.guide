#!/usr/bin/env bash
# Provision the sermon.guide EC2 box: security group (443/80 world, 22 admin
# IP only), key pair, Ubuntu 24.04 instance with Docker via user-data, and an
# Elastic IP (so stop/start keeps the same address — stopped instances bill
# only EBS ~$8/mo + EIP ~$3.65/mo, not compute).
#
# Idempotent-ish: re-running finds the tagged instance and exits with info.
#
#   ./provision.sh                          # t3a.xlarge, 100GB gp3, current region
#   INSTANCE_TYPE=t3a.large ./provision.sh  # override sizing
#   SSH_CIDR=1.2.3.4/32 ./provision.sh      # override admin-SSH source

. "$(dirname "$0")/common.sh"

INSTANCE_TYPE="${INSTANCE_TYPE:-t3a.xlarge}"
VOLUME_GB="${VOLUME_GB:-100}"

require_aws
REGION="$(region)"
echo "region: ${REGION}  type: ${INSTANCE_TYPE}  disk: ${VOLUME_GB}GB gp3"

existing="$(find_instance)"
if [ -n "${existing}" ]; then
  state="$(instance_state "${existing}")"
  eip_info="$(find_eip)"
  echo "already provisioned: ${existing} (${state})  eip: ${eip_info:-none}"
  echo "use deploy.sh / start.sh / stop.sh / destroy.sh"
  exit 0
fi

# --- default VPC ---
vpc_id="$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
  --query 'Vpcs[0].VpcId' --output text)"
if [ "${vpc_id}" = "None" ] || [ -z "${vpc_id}" ]; then
  echo "no default VPC — creating one"
  vpc_id="$(aws ec2 create-default-vpc --query 'Vpc.VpcId' --output text)"
fi
echo "vpc: ${vpc_id}"

# --- security group: 80/443 world, 22 admin only ---
# Admin IP is resolved BEFORE the SG exists, and rules are (re)applied even
# when the SG already exists — so a half-created SG from an aborted earlier
# run self-heals instead of silently shipping with no ingress (SSH lockout).
if [ -z "${SSH_CIDR:-}" ]; then
  my_ip="$(curl -fsS https://checkip.amazonaws.com || true)"
  [ -n "${my_ip}" ] || die "could not determine admin IP — set SSH_CIDR=x.x.x.x/32 explicitly"
  SSH_CIDR="${my_ip}/32"
fi

sg_id="$(find_security_group)"
if [ -z "${sg_id}" ]; then
  sg_id="$(aws ec2 create-security-group \
    --group-name "${TAG_NAME}-sg" \
    --description "sermon.guide single-box: 443/80 public, 22 admin" \
    --vpc-id "${vpc_id}" \
    --tag-specifications "ResourceType=security-group,Tags=[{Key=Name,Value=${TAG_NAME}},{Key=Project,Value=${TAG_NAME}}]" \
    --query 'GroupId' --output text)"
fi
echo "sg: ${sg_id}  ssh from: ${SSH_CIDR}"
for perm in \
  "IpProtocol=tcp,FromPort=80,ToPort=80,IpRanges=[{CidrIp=0.0.0.0/0}],Ipv6Ranges=[{CidrIpv6=::/0}]" \
  "IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges=[{CidrIp=0.0.0.0/0}],Ipv6Ranges=[{CidrIpv6=::/0}]" \
  "IpProtocol=udp,FromPort=443,ToPort=443,IpRanges=[{CidrIp=0.0.0.0/0}],Ipv6Ranges=[{CidrIpv6=::/0}]" \
  "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=${SSH_CIDR}}]"; do
  out="$(aws ec2 authorize-security-group-ingress --group-id "${sg_id}" \
    --ip-permissions "${perm}" 2>&1)" \
    || { echo "${out}" | grep -q InvalidPermission.Duplicate || die "SG ingress failed: ${out}"; }
done

# --- key pair ---
if ! aws ec2 describe-key-pairs --key-names "${KEY_NAME}" >/dev/null 2>&1; then
  echo "creating key pair ${KEY_NAME} → ${KEY_FILE}"
  mkdir -p "$(dirname "${KEY_FILE}")"
  aws ec2 create-key-pair --key-name "${KEY_NAME}" \
    --key-type ed25519 \
    --tag-specifications "ResourceType=key-pair,Tags=[{Key=Project,Value=${TAG_NAME}}]" \
    --query 'KeyMaterial' --output text > "${KEY_FILE}"
  chmod 600 "${KEY_FILE}"
elif [ ! -f "${KEY_FILE}" ]; then
  die "key pair ${KEY_NAME} exists in AWS but ${KEY_FILE} is missing locally — delete the AWS key pair or set SERMON_AWS_KEY_FILE"
fi

# --- AMI: latest Ubuntu 24.04 LTS amd64 ---
ami_id="$(aws ssm get-parameters \
  --names /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id \
  --query 'Parameters[0].Value' --output text)"
echo "ami: ${ami_id}"

# --- user-data: Docker engine + compose v2 (official repo; distro packages
# are too old for the compose features the prod file uses) ---
user_data="$(mktemp)"
trap 'rm -f "${user_data}"' EXIT
cat > "${user_data}" <<'CLOUDINIT'
#!/bin/bash
set -euxo pipefail
apt-get update
apt-get install -y ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
usermod -aG docker ubuntu
mkdir -p /opt/sermon
chown ubuntu:ubuntu /opt/sermon
CLOUDINIT

# --- launch ---
instance_id="$(aws ec2 run-instances \
  --image-id "${ami_id}" \
  --instance-type "${INSTANCE_TYPE}" \
  --key-name "${KEY_NAME}" \
  --security-group-ids "${sg_id}" \
  --block-device-mappings "DeviceName=/dev/sda1,Ebs={VolumeSize=${VOLUME_GB},VolumeType=gp3,DeleteOnTermination=true}" \
  --metadata-options "HttpEndpoint=enabled,HttpTokens=required" \
  --instance-initiated-shutdown-behavior stop \
  --user-data "file://${user_data}" \
  --tag-specifications \
    "ResourceType=instance,Tags=[{Key=Name,Value=${TAG_NAME}},{Key=Project,Value=${TAG_NAME}}]" \
    "ResourceType=volume,Tags=[{Key=Name,Value=${TAG_NAME}},{Key=Project,Value=${TAG_NAME}}]" \
  --query 'Instances[0].InstanceId' --output text)"
echo "instance: ${instance_id} — waiting for running state"
aws ec2 wait instance-running --instance-ids "${instance_id}"

# --- elastic IP (survives stop/start) ---
eip_info="$(find_eip)"
if [ -z "${eip_info}" ]; then
  alloc_id="$(aws ec2 allocate-address --domain vpc \
    --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Name,Value=${TAG_NAME}},{Key=Project,Value=${TAG_NAME}}]" \
    --query 'AllocationId' --output text)"
else
  alloc_id="$(echo "${eip_info}" | awk '{print $1}')"
fi
aws ec2 associate-address --instance-id "${instance_id}" --allocation-id "${alloc_id}" >/dev/null
eip="$(aws ec2 describe-addresses --allocation-ids "${alloc_id}" --query 'Addresses[0].PublicIp' --output text)"

echo
echo "provisioned ✓"
echo "  instance : ${instance_id} (${INSTANCE_TYPE}, ${REGION})"
echo "  ip       : ${eip}"
echo "  ssh      : ssh -i ${KEY_FILE} ${SSH_USER}@${eip}"
echo
echo "cloud-init is installing Docker (~2min). next: ./deploy.sh"
