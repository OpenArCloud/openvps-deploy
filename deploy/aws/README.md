# OpenVPS on AWS

Deploy [OpenVPS](https://github.com/OpenArCloud/openvps) (MIT, © 2025 Nokia, contributed
to Open AR Cloud) onto a single on-demand GPU EC2 instance, fronted by a small always-on
host that wakes it when a request arrives.

Upstream's `docker-compose.yaml` is used **unmodified**. Everything AWS-specific lives in
`docker-compose.aws.yaml`, applied as a Compose override, so this deployment can track
upstream without maintaining a fork.

> **Status:** Phases 1–3 done and verified on a real deployment — 31/31 acceptance checks
> on a from-zero `create-stack`, plus a full stop/wake cycle. Phase 4 (the waker) is not
> built, so nothing is reachable from the internet; use SSM port-forwarding. See
> [NOTES.md](NOTES.md) for measurements and the friction log.
>
> **Read the capacity section before building Phase 4.** GPU capacity in `us-east-1` moved
> constantly during testing, and a wake after idle shutdown failed on its first attempt
> with `InsufficientInstanceCapacity` before succeeding on retry 51 seconds later. The
> waker must retry, and `g5`/`g4dn` are worth evaluating — measured peak VRAM was 3.8 GB of
> 22.4 GB available.

## Design

```
              cloudpose.io  (Route 53)
                     │
   t4g.nano "waker"  │  always on, ~$3.07/mo
     Caddy holds the domain and terminates TLS
       ├─ GPU instance stopped → holding page + StartInstances
       └─ GPU instance running → reverse proxy
                     │
   g6.2xlarge "openvps"  started on demand
     gp3 root 200 GB   Docker images + HLOC weights, survives stop
     gp3 data 200 GB   → /home/ubuntu/data/maps  (MY_SHARED_MAPS_DIR)
     docker compose up: backend, mapaligner, maplocalizer, frontend
     idle-shutdown timer
```

One instance is deliberate. MapLocalizer serves one active map at a time, so there is no
concurrency to scale out to, and keeping the `${MY_SHARED_MAPS_DIR}` bind mount intact is
what makes upstream trackable.

Explicitly out of scope: ECS/EKS/Batch/SageMaker/Fargate, splitting services across hosts,
EFS or S3-backed map storage, autoscaling, and multi-AZ or multi-region.

## Prerequisites

1. **Service quota.** `Running On-Demand G and VT instances` ≥ 8 vCPU in the target region:
   ```sh
   aws service-quotas get-service-quota --service-code ec2 --quota-code L-DB2E81BA
   ```
2. **FusionAuth.** A tenant and application, with the client ID and secret placed in
   Secrets Manager. Not created by this stack — see [NOTES.md](NOTES.md) for why FusionAuth
   is a separate concern from the four upstream services.
3. **Domain.** A Route 53 hosted zone, or `sslip.io` against the Elastic IP for real
   Let's Encrypt certificates with no DNS setup.

## Layout

| Path | Purpose |
|---|---|
| `template.yaml` | CloudFormation stack |
| `docker-compose.aws.yaml` | Compose override; the only place upstream behaviour is changed |
| `nginx/mapbuilder.conf` | Replaces the frontend's baked-in config to lift its 5 GB upload cap |
| `caddy/Caddyfile` | Waker: TLS, holding page, wake-on-request, reverse proxy (Phase 4, not built) |
| `scripts/idle-shutdown.sh` | Stops the GPU instance once it has been idle |
| `NOTES.md` | Measurements and every friction found |

## Deploying

```sh
aws cloudformation create-stack --stack-name openvps-aws \
  --template-body file://deploy/aws/template.yaml \
  --capabilities CAPABILITY_IAM \
  --parameters \
    ParameterKey=VpcId,ParameterValue=vpc-xxxx \
    ParameterKey=SubnetId,ParameterValue=subnet-xxxx \
    ParameterKey=SecretsManagerArn,ParameterValue=arn:aws:secretsmanager:... \
    ParameterKey=DomainName,ParameterValue=vps.example.org
```

First boot takes about **25 minutes**: clone, HLOC submodules, four image builds, cache
prune, container start. Measured 23m30s for the build alone on a `g6.2xlarge`. The stack
completes when the instance signals its wait condition.

Use `--on-failure DO_NOTHING` while iterating on user-data. The default rollback terminates
the instance and takes `/var/log/openvps-bootstrap.log` with it, leaving nothing to debug.

There is no SSH and no key pair. To get a shell or reach a service:

```sh
aws ssm start-session --target <instance-id>

# MapBuilder on http://localhost:8080
aws ssm start-session --target <instance-id> \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["80"],"localPortNumber":["8080"]}'
```

## Verified behaviour

From a clean `create-stack` on a `g6.2xlarge` in `us-east-1d`:

| | |
|---|---|
| Image build | 23 m 21 s |
| Stack create → serving | 25 m 40 s |
| Root disk used after bootstrap | 110 GiB of 194 |
| Acceptance checks | 31 / 31 |
| Idle timer stopped the instance | yes, unattended |
| Wake → all four services healthy | ~30 s after boot, no rebuild |
| Maps survived stop/start | yes, checksum identical |

The device name for the maps volume changed from `/dev/nvme2n1` to `/dev/nvme1n1` across
that stop/start. It remounted correctly because `/etc/fstab` keys on UUID and the bootstrap
identifies the disk by its NVMe serial. Never key on a device path here.

## Idle shutdown

`openvps-idle.timer` runs every 5 minutes and stops the instance once it has been idle for
`IdleShutdownMinutes` (default 30; `0` disables). Idle means **all** of:

- no CUDA compute process,
- no established off-box connection to :80, :3001 or :8000,
- no non-loopback HTTP request in any service log within the window,
- no file written under the maps directory within the window.

The last condition is not in the original design and matters: COLMAP's bundle adjustment is
CPU-bound and can run for many minutes holding no CUDA context and serving no HTTP. Without
it the instance would shut down in the middle of a reconstruction.

The instance is *stopped*, not terminated — the root volume keeps the built images and the
HLOC model cache, so a wake skips the 25-minute build entirely.

```sh
# change the threshold on a running host
sudo sed -i 's/^IDLE_MINUTES=.*/IDLE_MINUTES=60/' /opt/openvps/idle.conf
journalctl -t openvps-idle -n 20      # what it decided, and why
```

## Secrets

No secret ever appears in the template, in user-data, or in this repository.
`AUTH_SECRET`, `AUTH_FUSIONAUTH_ID`, and `AUTH_FUSIONAUTH_SECRET` live in Secrets Manager
and are fetched at boot by the instance role. Access is by SSM Session Manager; there is no
key pair and no SSH ingress.

## Cost

On-demand, `us-east-1`, from Phase 1 measurements.

| Component | Rate | Notes |
|---|---|---|
| `g6.2xlarge` GPU host | $0.9776/hr | only while awake |
| `g6.xlarge` alternative | $0.8048/hr | 4 vCPU; see the sizing note in NOTES.md |
| `t4g.nano` waker | $0.0042/hr | ~$3.07/mo, always on |
| gp3 root, 200 GB | ~$16/mo | standing; survives instance stop |
| gp3 data, 200 GB | ~$16/mo | standing; `DeletionPolicy: Retain` |
| Elastic IP | ~$3.60/mo | standing while allocated |

Standing cost is therefore about **$39/mo** with the GPU host stopped, plus roughly
**$0.98 per awake hour**. At 2 h/day the GPU adds about $59/mo.

The root volume is 200 GB because measurement demanded it, not by preference: the Deep
Learning AMI occupies 50 GB before anything is built, steady state after building all four
images is 112 GB, and an in-place rebuild transiently needs a further ~71 GB of build
cache. A 40 GB root cannot hold the AMI.

## Choosing an AZ — read this before creating the stack

`SubnetId` is effectively a permanent choice, and GPU capacity is scarce enough that it
needs thought rather than a default.

The maps volume is `DeletionPolicy: Retain` so it outlives stacks, and an EBS volume is
bound to one AZ for life. The instance must therefore always launch in the subnet's AZ.
There is deliberately no multi-AZ fallback: a fallback that stranded the maps would be
worse than a failed launch.

**Capacity moves, and it is specific to both AZ and instance type.** Observed within a
single session in `us-east-1`:

| Attempt | Type | Result |
|---|---|---|
| 1 | `g6.xlarge` | `InsufficientInstanceCapacity` in `1a` and `1b`; launched in `1c` |
| 2 | `g6.2xlarge` | `InsufficientInstanceCapacity` in `1c`; AWS suggested `1a`, `1b`, `1d`, `1f` |

So probe before committing — with a **real launch**, not a dry run.

`run-instances --dry-run` does **not** check capacity. It validates permissions only, and
returns `DryRunOperation` ("Request would have succeeded") in an AZ that has no capacity at
all. Verified against an AZ that had just refused a real launch. The only honest probe is
to actually launch and immediately terminate:

```sh
AMI=$(aws ssm get-parameter \
  --name /aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id \
  --query Parameter.Value --output text)

for SUB in <subnet-a> <subnet-b> <subnet-c> <subnet-d> <subnet-f>; do
  ID=$(aws ec2 run-instances --image-id "$AMI" --instance-type g6.2xlarge --subnet-id "$SUB" \
        --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":75,"VolumeType":"gp3","DeleteOnTermination":true}}]' \
        --tag-specifications 'ResourceType=instance,Tags=[{Key=Project,Value=openvps-aws},{Key=Name,Value=capacity-probe}]' \
        --query 'Instances[0].InstanceId' --output text 2>&1)
  case "$ID" in
    i-*) echo "$SUB AVAILABLE"; aws ec2 terminate-instances --instance-ids "$ID" >/dev/null ;;
    *)   echo "$SUB no capacity" ;;
  esac
done
```

A probe costs a few seconds of instance time. Always terminate — the loop above does.

**Pick an AZ that has capacity for both your default and your fallback instance type.**
`InstanceType` is a parameter so the host can be resized, but a resize is a
stop-modify-start *in the pinned AZ* — if that AZ has `g6.2xlarge` and not `g6.xlarge`, the
resize simply fails. A real probe across `us-east-1` produced this, all within a few
minutes:

| AZ | `g6.2xlarge` | `g6.xlarge` |
|---|---|---|
| `1a` | no | **yes** |
| `1b` | no | **yes** |
| `1c` | **yes** | no |
| `1d` | **yes** | **yes** |
| `1f` | no | no |

Only `1d` supported both, and so `1d` is where this stack was verified. Note `1c` had
refused `g6.2xlarge` ten minutes earlier — re-probe, do not trust a stale table.

The residual risk is that a *wake* can fail: when the waker calls `StartInstances` on the
stopped host, the pinned AZ may be momentarily full and there is nowhere else to go. The
alternatives — an On-Demand Capacity Reservation, which bills the full instance rate
whether running or not — cost more than the problem. Accept the risk and keep the
migration runbook below to hand.

## Migrating to another AZ

Not a disaster-only procedure. Given how capacity moves, expect to need it eventually.

```sh
STACK=openvps-aws
OLD_VOL=$(aws cloudformation describe-stacks --stack-name $STACK \
            --query 'Stacks[0].Outputs[?OutputKey==`MapsVolumeId`].OutputValue' --output text)

# 1. Stop the GPU host so the filesystem is quiescent, then snapshot.
aws ec2 stop-instances --instance-ids <instance-id>
aws ec2 wait instance-stopped --instance-ids <instance-id>
SNAP=$(aws ec2 create-snapshot --volume-id "$OLD_VOL" \
        --description "openvps maps pre-AZ-migration" \
        --tag-specifications 'ResourceType=snapshot,Tags=[{Key=Project,Value=openvps-aws}]' \
        --query SnapshotId --output text)
aws ec2 wait snapshot-completed --snapshot-ids "$SNAP"

# 2. Delete the stack. The maps volume is retained, not destroyed.
aws cloudformation delete-stack --stack-name $STACK
aws cloudformation wait stack-delete-complete --stack-name $STACK

# 3. Recreate in the new AZ, then restore the snapshot over the fresh maps volume:
#    create a volume from $SNAP in the new AZ, stop the instance, detach the empty
#    volume, attach the restored one as /dev/sdf, start. User-data mounts by UUID and
#    will not reformat a volume that already has a filesystem.
```

Keep the old volume until the new one is verified, then delete it — an idle 200 GB gp3
volume is about $16/mo.

## Teardown

```sh
aws cloudformation delete-stack --stack-name openvps-aws
```

The maps volume carries `DeletionPolicy: Retain`, so this deliberately leaves it behind —
verified: it survives as `available`. Everything else goes. Two consequences:

- A recreated stack does **not** adopt the old volume. It creates a new empty one; re-attach
  the old one by hand (see migration above) or import it.
- The retained volume keeps billing at roughly $16/mo until explicitly deleted. Find
  leftovers with:

```sh
aws ec2 describe-volumes --filters Name=tag:Project,Values=openvps-aws Name=status,Values=available \
  --query 'Volumes[].{Id:VolumeId,Size:Size,AZ:AvailabilityZone,Created:CreateTime}' --output table
```
