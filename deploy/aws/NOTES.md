# OpenVPS on AWS — Build Notes

Running log of measurements and every upstream/AWS friction encountered. Written for
whoever picks this up next; the friction list is as much the deliverable as the template.

Upstream pinned at `fc2f470` (2026-07-02, "Merge pull request #10 from nokia/release-patch03").

---

## Phase 0 — Prerequisites & reconnaissance

Date: 2026-08-20. Region: `us-east-1`.

### Service quota — PASS

`Running On-Demand G and VT instances` (`ec2` / `L-DB2E81BA`) = **768 vCPU**, account-level,
no pending change requests. The brief required ≥ 8. `g6.xlarge` is 4 vCPU and `g6.2xlarge`
is 8, so there is enormous headroom; quota will not be the constraint.

### Instance types

| Type | vCPU | RAM | GPU | VRAM |
|---|---|---|---|---|
| `g6.xlarge` | 4 | 16 GiB | NVIDIA L4 | 22.4 GiB |
| `g6.2xlarge` | 8 | 32 GiB | NVIDIA L4 | 22.4 GiB |

Both offered in all five `us-east-1` AZs (`a`–`f`, excluding `e`). Note that the two sizes
share the *same* GPU and the same VRAM — stepping up to `2xlarge` buys CPU and system RAM
only. That matters for the Phase 1 recommendation: if the pressure point turns out to be
VRAM, `2xlarge` does not help and the answer is a different instance family.

### AMI

Resolved from SSM public parameters rather than hardcoded, as required:

- `/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id`
- `/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-24.04/latest/ami-id`

Both exist. Selection and rationale recorded in Phase 1 below.

### FusionAuth credentials — NOT STAGED

Secrets Manager holds no secrets in `us-east-1`. Phase 1 therefore proceeds with a
throwaway FusionAuth instance and throwaway `AUTH_*` values generated on the test box and
never persisted anywhere. Real credentials must be staged before Phase 2.

### Domain

`cloudpose.io` — already the Open AR Cloud infrastructure domain (it hosts
`oscp.cloudpose.io`, `sparcl.cloudpose.io`, `rtc.oscp.cloudpose.io`), correctly delegated
to Route 53 in this account. OpenVPS subdomains will be nested to avoid colliding with the
13 existing records.

> **Operational flag, unrelated to this project:** `cloudpose.io` has auto-renew **disabled**
> and expires **2026-09-11**. Every OARC service on that domain goes dark if it lapses.

Rejected alternatives, recorded so nobody re-investigates them:

- `cloudpose.net` — hosted zone exists in this account but the domain has **no public NS
  delegation at all**. Orphaned zone; the domain is unregistered or lapsed.
- `cloudpose.com` — hosted zone exists in this account but the domain is delegated to
  `nsg1/nsg2.namebrightdns.com`, not Route 53. Also an orphaned zone.
- `open4d.org`, `spatialdds.org` — both viable (Route 53, auto-renew on, long expiry) but
  neither is where OARC services already live.

---

## Upstream frictions found before touching an instance

These were all found by reading the repo, not by hitting them at runtime. Runtime
frictions get appended in Phase 1.

### 1. FusionAuth and Postgres are not in the upstream compose file

The brief's architecture sketch lists `backend, mapaligner, maplocalizer, frontend,
fusionauth, postgres` as one `docker compose up`. Upstream `docker-compose.yaml` defines
**four** services only: `backend`, `mapaligner`, `maplocalizer`, `frontend`.

FusionAuth is a **separate compose stack**, pulled from the FusionAuth project's own
repo per `docs/FusionAuth.md`, and it brings Postgres *and* Elasticsearch with it. This is
a real architectural fork in the road, not a detail:

- It cannot live on a `t4g.nano` waker — FusionAuth plus Postgres plus Elasticsearch wants
  roughly 4 GB of RAM, and a nano has 0.5 GB.
- If it lives on the GPU instance, authentication is down whenever the GPU instance is
  stopped — which is the normal state in this design. A user hitting the holding page
  cannot log in until the GPU box is fully up, and the `AUTH_FUSIONAUTH_ISSUER` URL points
  at a host that is usually off.

Deferred to Phase 4. Options are a larger always-on waker (a standing charge, so it needs
sign-off), FusionAuth Cloud (likewise), or accepting that login is only available once the
GPU instance is warm. Flagged rather than silently resolved.

### 2. The frontend's 5 GB request body cap is baked into the image

The brief requires that nothing in the proxy path limit request body size, because
MapBuilder ingests multi-GB StrayScanner zips. Upstream `mapbuilder/Frontend/nginx.conf`
sets `client_max_body_size 5000M` on **both** the `:80` and `:443` server blocks, and that
file is `COPY`'d into the image at build time — so it is not overridable by environment
variable.

Two limits will therefore exist in the path (Caddy's, and this one), and only Caddy's is
configuration. Handling: bind-mount a replacement `default.conf` over
`/etc/nginx/conf.d/default.conf` from `docker-compose.aws.yaml`. That keeps upstream
untouched, which is the point of the override file.

### 3. `MAPBUILDER_URL` is in the compose file but not in the README's `docker.env`

Confirmed: `docker-compose.yaml` passes `MAPBUILDER_URL=${MAPBUILDER_URL}` into the
`mapaligner` service's environment, but the README's `docker.env` template does not list
it. Unset, it resolves to an empty string and Compose emits a warning rather than failing.
It is consumed by MapAligner to link back to MapBuilder. Correct value determined in
Phase 1.

### 4. `frontend` binds both 80 and 443 and mounts a self-signed cert

`frontend` publishes `${MAPBUILDER_PORT}:80` and `${MAPBUILDER_PORT_HTTPS}:443` and mounts
`./mapbuilder/certs` (a committed self-signed pair, `nginxCert.pem` / `nginxKey.pem`).
Behind Caddy this should be HTTP-only. Handled in the override file.

### 5. HLOC is a pinned separate clone used as a build context

`Hierarchical-Localization` is cloned beside the repo and consumed via
`additional_contexts: hloc_context=...` by both `backend` and `maplocalizer`. The README
offers three commits:

| Commit | Date | pycolmap range |
|---|---|---|
| `abb2520` | 2024-11-03 | `<= 3.11` |
| `2e2a551` | 2025-07-22 | `>= 3.12, < 4.0` |
| `c13273b` | 2025-12-10 | `== 3.13, < 4.0` |

Choice and rationale recorded in Phase 1. Requires `git submodule update --init --recursive`.

### 6. Known runtime hazards flagged by upstream

- **PyTorch/driver mismatch.** If the driver is older than the wheel expects, the README's
  remedy is pinning `torch==2.4.1 / torchvision==0.19.1 / torchaudio==2.4.1` in the
  `backend` and `maplocalizer` Dockerfiles. Since that means editing upstream Dockerfiles,
  the preferred fix is choosing an AMI whose driver is new enough.
- **`shm_size` bus error.** Upstream *already* sets `shm_size: 4GB` on `backend` and
  `maplocalizer`. So the documented remedy is pre-applied; if the bus error still appears,
  4 GB is not enough and the override file must raise it — worth watching on a 16 GiB box,
  since 8 GB of shared memory across two services is half of system RAM.

---

## Phase 1 — Manual bring-up

Date: 2026-08-21. Instance `g6.xlarge` in `us-east-1c`, AMI
`ami-0909175f265dac51e` (Deep Learning Base OSS Nvidia Driver GPU, Ubuntu 22.04, 20260818),
250 GB encrypted gp3 root (deliberately oversized so real consumption could be measured),
no key pair, no inbound rules, SSM Session Manager only.

**Result: pass.** All four services built and came up healthy, both GPU services see the
L4, and a full hloc SfM reconstruction completed end to end.

### Host

| | |
|---|---|
| GPU | NVIDIA L4, 23034 MiB |
| Driver | 595.91.07 (CUDA 13.2) |
| Docker | 29.7.2 |
| Compose | v5.5.0 |
| RAM | 15 GiB usable |
| vCPU | 4 |

### Measurements

| Metric | Value |
|---|---|
| **Build wall time** | **19 m 45 s** (`docker compose build`, cold, no cache) |
| Cold `up -d` to all-healthy | 53 s |
| **Peak host RAM** | **2921 MB** of 15 GiB — across build, startup, and reconstruction |
| Peak RAM, build stage only | 1837 MB |
| Idle RAM, four services up | 1.4 GiB |
| **Peak VRAM** | **3796 MB** of 23034 MiB (16 %) |
| Idle VRAM | 3 MiB — nothing is resident until work arrives |
| Peak 1-min load | 7.03 on 4 vCPU |

### Image sizes

| Image | Size |
|---|---|
| `openvps-backend` | 41.5 GB |
| `openvps-maplocalizer` | 40.7 GB |
| `openvps-mapaligner` | 267 MB |
| `openvps-frontend` | 74.2 MB |
| Total on disk after layer sharing | 65.53 GB |

The two large images are nearly identical in content — both are
`nvidia/cuda:11.8.0-cudnn8-devel-ubuntu22.04` plus the COLMAP/hloc toolchain plus PyTorch —
so layer sharing recovers most of the apparent 82 GB.

### Disk — the brief's 40 GB root is not viable

| State | Root used |
|---|---|
| AMI, freshly booted, nothing built | 50 GB |
| Peak during build (images + build cache) | **146 GB** |
| Steady state after `docker builder prune -af` | **112 GB** |

The Deep Learning AMI alone is 50 GB before anything is built. Steady state is 112 GB, and
a rebuild transiently needs another ~71 GB of build cache on top. A 40 GB root cannot even
hold the AMI.

Sizing conclusion: **200 GB gp3 root**, which leaves room to rebuild in place. 160 GB works
only if the build cache is pruned before every rebuild. Using a plain Ubuntu AMI instead
would reclaim roughly 50 GB, at the cost of installing and maintaining the NVIDIA driver,
Docker, and the container toolkit — the DLAMI's reliability is worth the disk here.

### Functional verification

| Check | Result |
|---|---|
| `docker run --gpus all nvidia/cuda:12.4.0-base nvidia-smi` | GPU 0: NVIDIA L4 |
| `frontend` :80 | HTTP 200, `<title>OpenVPS MapBuilder</title>` |
| `frontend` :443 (upstream self-signed cert) | HTTP 200 |
| `backend` `/healthcheck` | HTTP 200 `OK` |
| `backend` `/maps` via frontend proxy | HTTP 401 — auth enforced, Auth.js wiring live |
| `maplocalizer` `/openapi.json` | valid FastAPI 3.1.0 schema |
| `mapaligner` `/` | HTTP 200 |
| `torch.cuda.is_available()` in `backend` | True — 2.4.1+cu121, NVIDIA L4 |
| `torch.cuda.is_available()` in `maplocalizer` | True — 2.4.1+cu121, NVIDIA L4 |
| `pycolmap` | 3.13.0 — confirms the `c13273b` pairing |

Only `backend` defines a healthcheck upstream. The other three report `running`, not
`healthy`; the checks above were done by hand. Worth adding healthchecks in the override
file so the waker can tell "up" from "serving".

### End-to-end SfM reconstruction

Ran hloc's `sacre_coeur` set (10 real images, ~1000×700) through the exact stages
`hloc_build_map.py` uses, with OpenVPS's default confs — `superpoint_aachen`, `superglue`,
`netvlad`, retrieval-based pairs.

```
netvlad-retrieval-extract    peak VRAM alloc 1353.9 MB   reserved 3514 MB
pairs-from-retrieval
superpoint-extract     0.4s
superglue-match        3.7s
colmap-reconstruction  3.8s
TOTAL 39.4s for 10 images (3.9 s/img)
10 registered images, 1786 points3D, 7817 observations, mean reproj. error 0.985 px
```

Host-wide peak during the run: 2849 MB RAM, 3796 MB VRAM.

### Localizer VRAM, measured directly

Loading the three models the localizer keeps resident:

| Model | Load time | Cumulative VRAM allocated |
|---|---|---|
| SuperPoint | 0.4 s | 5.0 MB |
| NetVLAD | 26.7 s | 573.4 MB |
| SuperGlue (outdoor) | 0.2 s | 619.3 MB |

Forward passes, batch of one:

| Input | VRAM reserved |
|---|---|
| 1024×768 | 1992 MB |
| 1920×1440 (native StrayScanner) | 6044 MB |

VRAM scales with image resolution, not with scan size — inference is batch-of-one
throughout. Even at native resolution the ceiling is ~6 GB against 23 GB available.

---

## Phase 1 frictions

### 7. NetVLAD downloads 529 MB at first use, into the container writable layer

The largest runtime surprise. SuperPoint (5 MB) and SuperGlue (46 MB) ship inside the image
under `/app/hloc/third_party`, but NetVLAD's 529 MB checkpoint is **not** baked in — it is
fetched on first use into `/root/.cache/torch`, taking 26.7 s.

`docker inspect` confirms the only mount on `maplocalizer` is the maps bind, so that cache
lives in the container's writable layer. It survives `stop`/`start`, but any `compose up`
that *recreates* the container discards it — and user-data running `compose up -d` on every
boot will recreate containers whenever the config changes. The result would be a silent
529 MB download and a ~27 s stall on the first localization after a wake, which is exactly
the latency the waker design is trying to avoid.

Fix in the override file: mount a persistent volume at `/root/.cache/torch` for both
`backend` and `maplocalizer`. Pre-warming it during Phase 2 user-data is also worth doing.

### 8. hloc requests more DataLoader workers than a `g6.xlarge` has cores

During matching:

> `UserWarning: This DataLoader will create 5 worker processes in total. Our suggested max
> number of worker in current system is 4`

hloc's configs assume more than 4 vCPU. It is a warning, not an error, but it means the
4-vCPU instance is oversubscribed during the CPU-heavy stages. Feeds the sizing question
below.

### 9. `maplocalizer`'s `.env` ends up with duplicate keys

The Dockerfile appends `uploadsDir=/uploads` to a `server/.env` that already ships with
`uploadsDir=/path/to/your/maps`, so the file contains the key twice. The later assignment
wins with the loader in use, so behaviour is correct today — but it is fragile and reads as
a mistake. Cosmetic; upstream-worthy.

### 10. `MAPBUILDER_URL` resolved

Setting it makes `docker compose config` parse with no unset-variable warnings, confirming
it is genuinely required rather than vestigial. It is consumed by `mapaligner` to link back
to MapBuilder, so it must be MapBuilder's **public** URL, not an internal service name.

### 11. `g6.xlarge` capacity is AZ-dependent

`us-east-1a` and `us-east-1b` both returned `InsufficientInstanceCapacity`; `us-east-1c`
succeeded. The template must not pin a single subnet — it needs several candidate subnets,
or the stack will intermittently fail to launch through no fault of its own.

---

## Sizing recommendation: move the default to `g6.2xlarge`

On-demand, `us-east-1`:

| Type | vCPU | RAM | $/hr | vs xlarge |
|---|---|---|---|---|
| `g6.xlarge` | 4 | 16 GiB | $0.8048 | — |
| `g6.2xlarge` | 8 | 32 GiB | $0.9776 | +21.5 % |
| `t4g.nano` | 2 | 0.5 GiB | $0.0042 | ~$3.07/mo |

**Neither the GPU nor RAM is the constraint.** Peak VRAM was 3796 MB of 23034 (16 %), and
peak RAM 2921 MB of 15 GiB (19 %). Critically, `g6.xlarge` and `g6.2xlarge` carry the *same*
L4 with the same 22.4 GiB — so stepping up buys CPU and system RAM only, and the GPU is
already oversized for this workload at either size.

**CPU is the constraint.** Load hit 7.03 on 4 vCPU during the build, and hloc explicitly
asks for 5 DataLoader workers on a 4-core box. COLMAP's incremental mapper and bundle
adjustment are CPU-parallel and are the long pole in map building — the one operation whose
duration a user actually waits on.

The argument for `2xlarge` is that it doubles the scarce resource for 21.5 % more, and at
this duty cycle the absolute difference is negligible: at 2 h/day it is about $10.50/month
versus $8.65. The idle-shutdown timer means we pay for work done, not time elapsed, so the
faster instance may not even cost more per map built.

**Honest limit on this recommendation.** The reconstruction measured here was 10 images. A
real StrayScanner scan is hundreds of frames, and COLMAP's memory grows with registered
images and 3D points. Nothing in the measurements suggests 16 GiB would be exceeded, but
that has not been demonstrated, and it is the one variable that could independently force
`2xlarge`. Re-measure with a real multi-GB scan when one is available.

`InstanceType` stays a parameter either way, and resizing is a stop-modify-start with the
data volume detached from the question entirely.
