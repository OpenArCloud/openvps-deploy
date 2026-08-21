# openvps-deploy

Deploy [OpenVPS](https://github.com/OpenArCloud/openvps) on AWS: a single GPU instance that
starts on demand, stops itself when idle, and runs the upstream `docker-compose.yaml`
**unmodified**.

> **This repository contains no OpenVPS application code.** It is deployment infrastructure
> only — CloudFormation, a Compose override, and boot scripts. The instance clones upstream
> at a pinned commit at boot. Everything AWS-specific lives in an override file, so tracking
> a new OpenVPS release is a pin bump rather than a merge.

OpenVPS is MIT-licensed, © 2025 Nokia, contributed to
[Open AR Cloud](https://www.openarcloud.org/).

## Status

Phases 1–3 are built and verified on real deployments. The waker is not built yet, so
**nothing is reachable from the internet** — reach services over SSM port-forwarding.

| | State |
|---|---|
| Manual bring-up, measurements | done |
| CloudFormation stack | done — 31/31 acceptance from a clean `create-stack` |
| Idle shutdown + wake | done — unattended stop, wake with maps intact |
| FusionAuth identity provider | done — 30/30 acceptance |
| Waker (Caddy, TLS, wake-on-request) | **not started** |

**Verified honestly means "runs and is wired correctly", not "does its job".** All four
services start healthy, both GPU containers see CUDA, and a full hloc SfM reconstruction
runs on the box (10/10 images registered, 0.985 px mean reprojection error). But **no map
has been built through the application's own API**, no localization has been performed, no
multi-GB upload tested, and nobody has logged in — those need a StrayScanner dataset and
real FusionAuth credentials. See [NOTES.md](deploy/aws/NOTES.md).

## Design

```
              your domain  (Route 53)
                     │
   t4g.nano "waker"  │  always on, ~$3/mo          ← Phase 4, not built
     Caddy holds the domain and terminates TLS
       ├─ GPU instance stopped → holding page + StartInstances
       └─ GPU instance running → reverse proxy
                     │
   g4dn.2xlarge "openvps"   started on demand
     gp3 root 200 GB   Docker images + HLOC weights, survives stop
     gp3 data 200 GB   maps + FusionAuth database, RETAINED on stack deletion
     backend · mapaligner · maplocalizer · frontend · fusionauth · postgres
     idle-shutdown timer
```

One instance is deliberate. MapLocalizer serves one active map at a time, so there is no
concurrency to scale out to, and keeping the `${MY_SHARED_MAPS_DIR}` bind mount intact is
what makes upstream trackable.

Out of scope on purpose: ECS/EKS/Batch/SageMaker/Fargate, splitting services across hosts,
EFS or S3-backed map storage, autoscaling, multi-AZ and multi-region.

## Cost

About **$39/month standing** (two 200 GB gp3 volumes plus the waker) and roughly
**$0.75 per awake hour**. At 2 h/day that is around $85/month all in. The GPU host is
stopped by default and only billed while awake.

## Getting started

```sh
python3 deploy/aws/build.py          # regenerate template.yaml from its sources

aws cloudformation create-stack --stack-name openvps-aws \
  --template-body file://deploy/aws/template.yaml \
  --capabilities CAPABILITY_IAM \
  --parameters \
    ParameterKey=VpcId,ParameterValue=vpc-xxxx \
    ParameterKey=SubnetId,ParameterValue=subnet-xxxx \
    ParameterKey=SecretsManagerArn,ParameterValue=arn:aws:secretsmanager:... \
    ParameterKey=DomainName,ParameterValue=vps.example.org
```

First boot takes about 25 minutes: clone, HLOC submodules, four image builds, container
start. There is no SSH and no key pair — access is SSM Session Manager only.

**Read [the AZ section](deploy/aws/README.md#choosing-an-az--read-this-before-creating-the-stack)
before picking a subnet.** The retained maps volume pins the availability zone permanently,
and GPU capacity is scarce and moves between zones.

## What this deployment learned

The friction log is as much the deliverable as the template. A few things that cost real
time and are not obvious from upstream's documentation:

- **Pick the instance type on availability, not on GPU.** A capacity probe found
  `g4dn.2xlarge` in 5 of 5 `us-east-1` AZs against 1 of 5 for `g6.2xlarge` — at 23 % lower
  cost, identical CPU and RAM, and identical reconstruction quality. Peak VRAM measured
  3.8 GiB against 22.4 available, so the GPU was never the constraint.
- **A wake can fail.** After the idle timer stopped the instance, `StartInstances` returned
  `InsufficientInstanceCapacity` on the first attempt and succeeded on the second. The waker
  must retry.
- **`run-instances --dry-run` does not check capacity.** It returns success in a zone with
  none. Probe with a real launch.
- **Never key storage on a device path.** The maps volume moved from `/dev/nvme2n1` to
  `/dev/nvme1n1` across a single stop/start. And every `g6`/`g4dn` ships an instance-store
  disk that the Deep Learning AMI pre-formats, so "the disk that is not the root disk"
  silently selects ephemeral storage.
- **NetVLAD is not in the image.** It downloads 529 MB on first use into the container's
  writable layer, so a wake would stall on a silent re-download without a persistent cache.
- **The frontend caps uploads at 5 GB** inside its image, which multi-GB StrayScanner
  recordings exceed.

Full detail, with measurements and the reasoning behind each design decision, is in
[deploy/aws/NOTES.md](deploy/aws/NOTES.md).

## Layout

| Path | Purpose |
|---|---|
| [`deploy/aws/README.md`](deploy/aws/README.md) | Operator guide: prerequisites, AZ choice, deploy, teardown |
| [`deploy/aws/NOTES.md`](deploy/aws/NOTES.md) | Measurements and every friction found |
| `deploy/aws/template.yaml` | CloudFormation stack — **generated**, do not hand-edit |
| `deploy/aws/template.src.yaml` | Source for the above |
| `deploy/aws/build.py` | Assembles the template; `--check` detects drift |
| `deploy/aws/docker-compose.aws.yaml` | Compose override — the only place upstream behaviour changes |
| `deploy/aws/fusionauth/` | Identity provider stack and its first-boot configuration |
| `deploy/aws/nginx/`, `scripts/`, `systemd/` | Config, boot and idle-shutdown logic |

## Secrets

No secret appears in this repository, in the template, or in user-data. `AUTH_SECRET`, the
FusionAuth client credentials and the database password live in Secrets Manager and are
fetched at boot by the instance role, re-read on every start so rotation needs no redeploy.

## Licence

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Open AR Cloud.

This matches upstream OpenVPS, which is MIT © 2025 Nokia and contributed to Open AR Cloud.
No upstream code is vendored here, so upstream's copyright is not carried into these files;
the one derived file is `deploy/aws/nginx/mapbuilder.conf`, adapted from upstream's
`mapbuilder/Frontend/nginx.conf` and noted as such in place.
