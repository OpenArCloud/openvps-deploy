# OpenVPS on AWS

Deploy [OpenVPS](https://github.com/OpenArCloud/openvps) (MIT, © 2025 Nokia, contributed
to Open AR Cloud) onto a single on-demand GPU EC2 instance, fronted by a small always-on
host that wakes it when a request arrives.

Upstream's `docker-compose.yaml` is used **unmodified**. Everything AWS-specific lives in
`docker-compose.aws.yaml`, applied as a Compose override, so this deployment can track
upstream without maintaining a fork.

> **Status:** Phase 1 (manual bring-up) in progress. The CloudFormation template is not
> written yet. See [NOTES.md](NOTES.md) for measurements and the running friction log.

## Design

```
              cloudpose.io  (Route 53)
                     │
   t4g.nano "waker"  │  always on, ~$3/mo
     Caddy holds the domain and terminates TLS
       ├─ GPU instance stopped → holding page + StartInstances
       └─ GPU instance running → reverse proxy
                     │
   g6.xlarge "openvps"  started on demand
     gp3 root 40 GB    Docker images + HLOC weights, survives stop
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
| `caddy/Caddyfile` | Waker: TLS, holding page, wake-on-request, reverse proxy |
| `scripts/idle-shutdown.sh` | Stops the GPU instance once it has been idle |
| `NOTES.md` | Measurements and every friction found |

## Secrets

No secret ever appears in the template, in user-data, or in this repository.
`AUTH_SECRET`, `AUTH_FUSIONAUTH_ID`, and `AUTH_FUSIONAUTH_SECRET` live in Secrets Manager
and are fetched at boot by the instance role. Access is by SSM Session Manager; there is no
key pair and no SSH ingress.

## Cost

_Filled in once Phase 1 measurements land._

## Teardown

_Filled in with the template. The maps volume carries `DeletionPolicy: Retain`, so
`delete-stack` leaves it behind deliberately._
