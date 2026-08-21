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

---

## Phase 2 — CloudFormation

### The AZ trap: capacity and a retained volume pull in opposite directions

Phase 1 found `g6.xlarge` capacity is AZ-dependent (friction 11), which argues for a
template that can try several subnets. But an EBS volume is bound to one AZ for life, and
the maps volume is `DeletionPolicy: Retain` precisely so it outlives stacks. **Those two
requirements are in direct conflict**, and the volume wins: once maps exist in
`us-east-1c`, the instance can only ever launch in `us-east-1c`.

So the template takes a single `SubnetId` and treats that choice as permanent. There is no
multi-AZ fallback, because a fallback that stranded the data would be worse than a failure.
The parameter description says so plainly.

The consequence is an **operational risk worth naming**: when the waker calls
`StartInstances` on the stopped GPU host, that start can fail with
`InsufficientInstanceCapacity` if the pinned AZ is momentarily full — and unlike a fresh
launch, there is nowhere else to go. Options, none of them free:

- Accept it. A wake occasionally fails and retries later. Costs nothing.
- An On-Demand Capacity Reservation in the pinned AZ. Removes the risk entirely, but bills
  the full instance rate whether or not the instance is running — which defeats the point
  of the on-demand design.
- Keep a snapshot and be prepared to restore into another AZ. Cheap, manual, slow.

Recommend accepting it, with the snapshot as the disaster path. Flagged rather than buried
because it is the one failure mode this architecture cannot self-heal.

### Elastic IP: defaulted off, deliberately

The brief puts an Elastic IP on the GPU instance. The template supports that
(`AttachElasticIp`) but defaults it to `false`, because the GPU host does not appear to
need one:

- The waker reaches it over the VPC by **private IP**, which is already stable across
  stop/start — an EIP would add nothing.
- Outbound access for image pulls comes from the subnet's auto-assigned public IPv4.
- Public IPv4 now bills at ~$3.60/mo per address whether attached or idle.

The waker is what DNS points at and what genuinely needs an address that never moves, and
the guardrails pre-approve exactly one Elastic IP. Reserving it for the waker seemed the
better use. Set `AttachElasticIp=true` to get the brief's original arrangement.

### Why the waker security group is created here

The GPU group's only ingress source is the waker's group, so the waker group has to exist
before the GPU group can name it. It is created empty in this template, with no instance
attached; Phase 4 launches the waker into it. Ingress rules are separate
`AWS::EC2::SecurityGroupIngress` resources rather than inline, which is what avoids a
circular dependency between the two groups.

### Device naming, and a circular dependency avoided

Device names are not stable on Nitro: `/dev/sdf` in the attachment surfaces as
`/dev/nvmeXn1` with an unpredictable index. The obvious fix is to match on the NVMe serial,
which carries the EBS volume id — but referencing `MapsVolume` from user-data creates a
cycle, because `MapsVolume` takes its AZ from `Instance`. User-data therefore identifies
the data disk as *the whole disk that is not the root disk*, which is unambiguous because
exactly one extra volume is ever attached. It then mounts by UUID in `/etc/fstab` with
`nofail`, so a missing volume produces a failed service rather than an unbootable host.

### First boot vs every boot

`cloud-init` user-data runs once. Bringing the stack up has to happen on **every** boot,
including after the idle-shutdown timer stops the instance. So user-data does one-time
setup — volume, clone, build — and installs `openvps.service`, a `oneshot`
`RemainAfterExit` unit that owns `compose up` from then on.

`ExecStartPre` re-renders `docker.env` from Secrets Manager on every start, so a rotated
secret is picked up by a stop/start with no redeploy. `docker.env` is written with
`umask 077` and `chmod 600`, and the renderer never echoes a value.

### Failing fast

Two deliberate choices, both about not wasting 50 minutes:

- User-data checks for outbound internet before doing anything else. Without a NAT gateway
  the subnet must be public with auto-assign public IPv4 on; if it is not, every clone and
  pull would fail slowly. The check catches it in seconds.
- Every error path calls `signal FAILURE`, so `CreationPolicy` rolls back promptly instead
  of waiting out its `PT50M` timeout.

### Build cache is pruned on first boot

Phase 1 measured ~71 GB of build cache on top of a 112 GB steady state. User-data runs
`docker builder prune -af` after building, which is what makes a 200 GB root comfortable
rather than marginal.

### Three bugs the first stack deployment exposed

Recorded in full because two of them are the kind that pass every test and then bite in
production.

**1. `CreationPolicy` on the instance deadlocks against the volume attachment.**

The natural way to gate stack completion on a successful bootstrap is a `CreationPolicy`
with a resource signal. It cannot work here. `MapsVolumeAttachment` depends on `Instance`,
so CloudFormation waits for user-data to signal success *before* attaching the volume —
while user-data is waiting for that volume to appear. Neither side can move.

The fix is a `WaitCondition` that `DependsOn: Instance` instead. The instance reaches
`CREATE_COMPLETE` as soon as it is running, the attachment proceeds, user-data finds the
disk, and the signal goes to the wait handle rather than to the instance resource.

**2. "The disk that is not the root disk" selects the instance store — and would have
destroyed every map.**

The first version identified the data volume by elimination: the one whole disk that is not
the root disk. That is wrong on precisely the hardware recommended here. **Every g6 ships an
instance-store SSD** — 250 GB on `g6.xlarge`, 450 GB on `g6.2xlarge`:

```
g6.xlarge    InstanceStore=True   250 GB ssd
g6.2xlarge   InstanceStore=True   450 GB ssd
```

So there are two non-root disks, and the loop would have settled on whichever came last —
quite possibly the ephemeral one. It would then have formatted it, mounted it at
`MY_SHARED_MAPS_DIR`, and worked flawlessly. Until the first stop/start, at which point
instance-store contents are gone and every map with them.

This one deserves emphasis because of what Phase 3 is: an idle-shutdown timer whose entire
job is to stop the instance regularly. The bug would not have been a rare edge case, it
would have fired on a routine automated event, and the failure would have looked like
"maps mysteriously disappeared overnight" rather than anything traceable to volume setup.

The fix keys on the NVMe serial. EBS volumes report a serial of the form `vol0123abc…`;
instance-store disks do not. Filtering on that prefix, and excluding the root disk by name,
is unambiguous — and it does not reintroduce the `MapsVolume` reference that would recreate
the dependency cycle.

**3. The fail-fast egress probe was itself too aggressive.**

The single-shot connectivity check fired 51 seconds into boot and failed the stack, even
though the instance did have a public IP (`3.85.208.87`) in a subnet with
`MapPublicIpOnLaunch=true`. A check meant to save 50 minutes cost a full deploy cycle
instead. It now retries for 90 seconds and, before giving up, dumps `ip -br addr`, the
route table, `/etc/resolv.conf`, and a DNS lookup — so the next failure explains itself.

**Process note.** The first attempt used `--on-failure DELETE`, so the rollback terminated
the instance and took the bootstrap log with it, leaving only "Received FAILURE signal" to
work from. Use `--on-failure DO_NOTHING` while iterating on user-data; the cost of a
lingering failed stack is far smaller than the cost of a blind retry.

### Capacity is volatile enough to change the design's risk profile

The AZ trap described above was written as a theoretical concern. It then materialised
twice in one session, in opposite directions:

| Time | Type | Outcome |
|---|---|---|
| Phase 1 | `g6.xlarge` | refused in `us-east-1a` and `1b`; launched in `1c` |
| Phase 2 | `g6.2xlarge` | refused in `1c`; AWS named `1a`, `1b`, `1d`, `1f` as available |

Capacity is specific to **both** AZ and instance type, and it moved within a few hours. The
AZ that worked for Phase 1 was the one that failed for Phase 2.

Two conclusions:

- Pinning the AZ is not a rare-disaster risk, it is a routine operating condition. A waker
  `StartInstances` will eventually fail with `InsufficientInstanceCapacity` and have
  nowhere to fall back to.
- An AZ-migration runbook is mandatory, not optional. It is now in the README as a normal
  procedure, along with a `run-instances --dry-run` capacity probe to run *before*
  committing to a subnet.

This also retroactively justifies keeping the maps volume as a separate `Retain`ed resource
rather than a block-device mapping on the instance: it is what turns an AZ move into a
snapshot-and-restore instead of a rebuild-and-lose-everything.

### Device detection: proof, and why the original heuristic was worse than it looked

Block devices on the deployed `g6.2xlarge`:

```
nvme0n1  vol08e755c8bb951d66c  disk  200G                → root (/)
nvme1n1  AWS21A4D01387D1732F0  disk  419.1G LVM2_member  → /opt/dlami/nvme  (instance store)
nvme2n1  vol0ec604ebcc8e5c53a  disk  200G   ext4         → /home/ubuntu/data/maps
```

The serial-prefix rule works: `nvme2n1` was selected, `nvme1n1` skipped.

The instance store turns out to be a worse trap than first assessed. **The Deep Learning AMI
already formats it** — as an LVM volume group mounted at `/opt/dlami/nvme`. So under the
original "not the root disk" heuristic, the loop would have landed on `nvme1n1`, found an
existing filesystem via `blkid`, taken the "existing filesystem found; leaving it alone"
branch, and mounted the ephemeral disk as `MY_SHARED_MAPS_DIR` **without formatting
anything and without emitting a single error**. Bootstrap would have reported success.

The failure would then have surfaced only after the first stop/start, as maps that silently
vanished — with nothing in any log pointing at volume setup. The serial check is what makes
this deterministic rather than a coin flip on device enumeration order.

### Healthchecks: two separate bugs, both mine

The first end-to-end deploy built all four images and started all four containers, then
failed because `openvps.service` used `--wait` and two of the healthchecks I added never
passed. Upstream's `backend` check was fine throughout. Both bugs are in the checks, not the
services — the services were serving correctly the whole time.

**`localhost` resolves to `::1` first.** These alpine images ship an `/etc/hosts` mapping
`localhost` to both `127.0.0.1` and `::1`; busybox wget tries IPv6 first; nginx listens on
`0.0.0.0:80` only. So `wget http://localhost/` returns "Connection refused" against a
perfectly healthy service. Use `127.0.0.1` explicitly, never `localhost`, in a container
healthcheck.

**MapAligner's `/` redirects to a public URL.** Its Next.js middleware sends unauthenticated
requests to `${AUTH_URL}/api/auth/signin` — an absolute, *public* URL, which here is
`https://align.vps.cloudpose.io/...`. busybox wget follows redirects, so the check failed
with `bad address` and would have kept failing until public DNS existed. A healthcheck must
never depend on external DNS. The middleware's matcher explicitly excludes `api/auth`,
`_next/static`, `_next/image` and `favicon.ico`, so `/favicon.ico` answers 200 directly with
no redirect and no auth — that is the correct target.

Also: busybox wget has no `--tries` option (`wget [-cqS] [--spider] [-O FILE] ... [-T SEC]`).
Replaced with `-T 5`.

**A healthcheck must not create the activity it measures.** These checks hit nginx every 10
seconds and land in its access log, which the Phase 3 idle detector reads. Healthcheck
traffic originates from `127.0.0.1`, so the idle detector filters that source out —
otherwise the instance would never appear idle and would never shut down.

### Measured: cold stack to serving

On `g6.2xlarge` in `us-east-1d`:

```
05:02:34  bootstrap starts
05:02:48  build starts        (14s for volume, clone, config)
05:26:18  build done          23m30s
```

Roughly **25 minutes from `create-stack` to serving**, against Phase 1's 19m45s for the
build alone on a `g6.xlarge` — the extra covers clone, HLOC submodules, build-cache prune,
and container start. The `WaitCondition` timeout of 3600s has comfortable margin.

Disk after bootstrap, confirming the Phase 1 sizing: root 194 GiB total, **111 GiB used**,
84 GiB free — within a gigabyte of the 112 GB Phase 1 predicted.

---

## Phase 3 — Idle shutdown

A `systemd` timer (`openvps-idle.timer`, every 5 minutes) runs
`/usr/local/bin/openvps-idle-shutdown`. Threshold is the `IdleShutdownMinutes` stack
parameter, written to `/opt/openvps/idle.conf`; `0` disables shutdown without masking the
unit. The instance is **stopped**, not terminated — the root volume keeps the built images
and the HLOC cache.

The brief specifies "no CUDA compute process and no Caddy request for 30 minutes". Caddy
lives on the waker, which does not exist until Phase 4, so the on-host equivalents are used
and the check is deliberately wider than the brief:

| Signal | Rationale |
|---|---|
| `nvidia-smi --query-compute-apps` non-empty | Authoritative: a map is building or a localization is in flight |
| Established off-box TCP connection to :80/:3001/:8000 | A client is connected right now, even if idle mid-request |
| Non-loopback HTTP request in any service log within the window | Recent user traffic |
| A file written under `MY_SHARED_MAPS_DIR` within the window | Catches long CPU-only stages — COLMAP bundle adjustment, zip extraction — that hold no CUDA context and serve no HTTP |

The fourth exists because the brief's two conditions are not jointly sufficient. COLMAP's
bundle adjustment is CPU-bound and can run for many minutes with no CUDA context and no
HTTP traffic; on the brief's criteria alone the instance would shut down mid-reconstruction.

### The monitor must not create the activity it measures

The healthchecks added in Phase 2 hit each service every 10 seconds, and those requests land
in the same access logs the idle detector reads. Left unfiltered, the host would report busy
forever and never shut down — the timer would appear to work while quietly never firing.

Filtering loopback is the fix, but the first attempt got it half right and that is worth
recording, because the failure was invisible:

```
nginx (frontend)         127.0.0.1 - - [21/Aug/2026:05:38:37 +0000] "GET / HTTP/1.1" 200
uvicorn (maplocalizer)   INFO:     127.0.0.1:36072 - "GET / HTTP/1.1" 200 OK
Next.js (mapaligner)     (does not log requests at all)
```

An anchored `^127\.0\.0\.1` filter matches nginx and misses uvicorn, whose client address is
mid-line. So `maplocalizer` reported its own healthcheck as user traffic — observed live as
`busy (http:openvps-maplocalizer-1:6)` on a host with no users at all. Matching `127.0.0.1`
anywhere in the line covers all three formats.

The general lesson: three services, three log formats, and a filter validated against only
one of them. Test an idle detector against every service it reads, on a host you know to be
idle, and confirm it actually reports idle — "no shutdown happened" is not evidence the
logic works.

### Verified behaviour

Tested live with `shutdown`/`systemctl stop` stubbed out:

| Case | Expected | Result |
|---|---|---|
| Healthcheck traffic only | idle → shut down | `idle 2m >= 1m; stopping compose and shutting down` |
| Clock backdated 5 min | shut down | `idle 5m >= 1m` |
| GPU compute process held | busy | `busy (gpu:1); resetting idle clock` |
| `IDLE_MINUTES=0` | disabled | `idle shutdown disabled` |

Shutdown stops `openvps.service` and `sync`s before `shutdown -h now`, so containers exit
cleanly and any in-flight write to the maps volume is flushed before the filesystem goes.

---

## Capacity: the finding that most affects whether this architecture works

Over roughly two hours in `us-east-1` on 2026-08-21, GPU capacity moved constantly. Every
row is an observed `RunInstances` result, not a prediction:

| Time (UTC) | Type | AZ | Result |
|---|---|---|---|
| ~03:47 | `g6.xlarge` | `1a`, `1b` | insufficient capacity |
| ~03:47 | `g6.xlarge` | `1c` | launched |
| 04:38 | `g6.2xlarge` | `1c` | insufficient capacity |
| 04:44 | `g6.2xlarge` | `1a` | insufficient capacity |
| ~04:50 | `g6.2xlarge` | `1a`, `1b`, `1f` | insufficient capacity |
| ~04:50 | `g6.2xlarge` | `1c`, `1d` | available |
| ~04:50 | `g6.xlarge` | `1a`, `1b`, `1d` | available |
| ~04:50 | `g6.xlarge` | `1c`, `1f` | insufficient capacity |
| 04:58 | `g6.2xlarge` | `1d` | launched |
| 05:52 | `g6.2xlarge` | `1d` | insufficient capacity |

`us-east-1d` went from serving a `g6.2xlarge` at 04:58 to refusing one at 05:52 — under an
hour. `1c` refused `g6.2xlarge` at 04:38 and offered it at 04:50.

### What this means for the design

The architecture assumes a stopped instance can be started on demand. That assumption is
weaker than it looks:

- **A wake can fail.** `StartInstances` on a stopped instance needs capacity in the AZ the
  instance is already in. There is no fallback, because the maps volume pins the AZ.
- **A stop/start cycle is a gamble that repeats.** Phase 3's idle timer stops the instance
  several times a day by design, so this is not a rare event — the system deliberately
  re-enters the state where it needs capacity again.
- **Deploys need retry logic**, which is why the repo carries a probe-then-deploy loop
  rather than a single `create-stack`.

### Options, for a decision that is not mine to make

1. **Accept and retry.** Free. A wake occasionally fails; the waker retries, or the user
   refreshes. Acceptable for a research/demo service, poor for anything with an SLA.
2. **On-Demand Capacity Reservation** in the pinned AZ. Removes the risk completely and
   guarantees the wake. Costs the full instance rate continuously whether the instance runs
   or not — roughly $700/mo for a `g6.2xlarge`, which defeats the entire on-demand design.
3. **Try a different GPU family.** `g5` (A10G, 24 GB) and `g4dn` (T4, 16 GB) are older and
   often less contended than `g6`. Phase 1 measured peak VRAM at 3.8 GB against 22.4 GB
   available, so the GPU is heavily over-provisioned for this workload and a smaller or
   older card would very likely do. **This is the option worth investigating** and it was
   not in scope for Phase 2 — the workload is CPU-bound, and `g6` was picked before the
   capacity picture was known.
4. **Keep the instance running.** Defeats the purpose and costs ~$700/mo.

Recommendation: accept and retry for now, and evaluate `g5`/`g4dn` before this goes in
front of users. The measurements say the GPU is not the constraint, so trading down on GPU
to buy availability is close to free in performance terms.

### The predicted wake failure, observed — and what it actually cost

This stopped being theoretical during Phase 3 verification. After the idle timer stopped
the instance as designed, the first attempt to wake it returned:

```
06:45:35Z  attempt 1 -> InsufficientInstanceCapacity ... calling StartInstances
06:46:26Z  attempt 2 -> pending
RUNNING after 2 start attempt(s), 73s total
```

The instance had been running in that AZ minutes earlier; stopping it released the hardware
and it was not immediately there to reclaim.

**It recovered on the next attempt, 51 seconds later.** That matters — the failure is
transient, not a hard block, and a waker that retries handles it. It is a latency problem,
not an availability wall. Do not over-read a single observation in either direction: one
retry sufficed here, and nothing about that guarantees one retry always will.

What it does establish:

- **The waker must retry `StartInstances`, not call it once.** A single call fails often
  enough to be seen on the very first test. Phase 4's holding page must poll through
  repeated capacity errors and stay honest with the user rather than spinning.
- **The three-minute cold-request target is at risk, not out of reach.** 73 seconds of
  start plus ~35 seconds of container start leaves room inside three minutes — but only if
  the retry budget stays small. A run of failures would blow it.
- **It reinforces, without proving, the case for another GPU family.** Measured peak VRAM
  was 3.8 GB of 22.4 GB, so `g5` or `g4dn` would very likely run this workload with less
  contention. Worth measuring before Phase 4, not assuming.

### Wake behaviour, verified end to end

| Check | Result |
|---|---|
| Instance stopped itself via the real timer | yes, ~06:39:34Z |
| `StartInstances` | succeeded on retry 2, 73s total |
| Maps volume remounted | by UUID, marker file md5 identical (`ae596130…`) |
| `openvps.service` | `enabled` and `active` with no intervention |
| All four services | healthy ~30s after boot |
| `openvps_hloc_cache` volume | present |
| Images rebuilt? | **no** — a wake skips the 25-minute build entirely |
| Serving | frontend 200, `/maps` 401 |
| Idle timer | re-armed, next run +10min (`OnBootSec`) |

**The device name changed across the reboot: `/dev/nvme2n1` became `/dev/nvme1n1`.** The
maps volume still mounted correctly because `/etc/fstab` keys on UUID and the bootstrap
keys on the NVMe serial. Had either used a device path, the wake would have mounted the
wrong disk or nothing at all. This is the clearest possible evidence for both choices, and
it appeared on the very first stop/start cycle.

---

## Phase 4 — Waker: not built, and what Phase 3 says about it

Out of scope for this session, but three findings above bear directly on it and should be
read before it is designed.

**1. FusionAuth has no home yet.** It is not in upstream's compose file, needs Postgres and
Elasticsearch alongside it (~4 GB), and so cannot run on a `t4g.nano`. If it runs on the
GPU host, login is unavailable exactly when a user first arrives and the host is still
waking. Options, all with costs: a larger always-on waker (a standing charge needing
sign-off), FusionAuth Cloud (likewise), or accepting that login only works once the GPU
host is warm. This is the biggest unresolved design question in the project.

**2. The upload path has one remaining limit, and it is Caddy's.** Upstream's 5 GB nginx
cap is lifted by the override, and the backend streams through `connect-busboy` with no
`limits` configured, so it imposes none. Caddy must not reintroduce one — and per the
brief, it has to be tested with a real multi-GB upload, not a small `curl`. Note the
override also sets `proxy_request_buffering off` and hour-long timeouts; Caddy needs
matching treatment or a slow multi-GB upload will time out in the proxy instead.

**3. The waker must retry `StartInstances` and degrade honestly.** Verified above: the
first wake attempt failed on capacity and the second succeeded 51 seconds later. A single
call is not enough. The holding page should poll through repeated capacity errors and, past
some threshold, say plainly that GPU capacity is unavailable rather than spinning forever.
The three-minute cold-request target holds only when the retry budget stays small.

Everything the waker needs from Phase 2 is already exported by the stack:
`WakerSecurityGroupId` (its security group, already the GPU host's only ingress source),
`PrivateIp` (stable across stop/start — proxy to this, not to a public address), and
`InstanceId` (for `StartInstances`).

Ports on the GPU host, all reachable only from the waker's security group:

| Service | Port |
|---|---|
| MapBuilder frontend | 80 |
| MapAligner | 3001 |
| MapLocalizer | 8000 |

Port 443 on the frontend is bound to `127.0.0.1` and carries upstream's self-signed cert;
ignore it. Caddy terminates TLS and speaks plain HTTP to port 80.

---

## Session teardown, 2026-08-21

Everything created for testing was removed. Final state verified by API, not assumed:

| Resource | State |
|---|---|
| All `Project=openvps-aws` EC2 instances | terminated (3, including the Phase 1 box) |
| All `Project=openvps-aws` EBS volumes | none remaining |
| `openvps-aws-test` stack | deleted |
| Phase 1 IAM role + instance profile | deleted |
| Phase 1 security group | deleted |
| Capacity-probe instances | all terminated |
| CloudWatch log groups | none remaining |

The Phase 1 instance was terminated rather than left stopped: its purpose was served once
the template built from zero reliably, and keeping it meant ~$20/mo for a 250 GB root
volume whose only value was skipping a 23-minute rebuild.

**One thing deliberately kept:** the Secrets Manager entry
`openvps-aws/PLACEHOLDER-replace-before-real-use` (~$0.40/mo). It holds a random
`AUTH_SECRET` and dummy FusionAuth values with a `_README` key saying so. It exists only so
the stack could be deployed end to end. **Login does not work against it.** Replace the
values with real FusionAuth application credentials, or delete it and pass a different
`SecretsManagerArn`.

Also worth noting: the maps volume was observed surviving `delete-stack` twice — once as
`available` after a failed stack, and once as `MapsVolume DELETE_SKIPPED` in the events of
a clean deletion. Retention works, and it means teardown leaves a billing tail unless the
volume is deleted deliberately.

---

## GPU family evaluation — `g4dn` vs `g5` vs `g6`

Prompted by the wake failure. The question was whether a different family fixes availability
without costing too much performance. Answered by measurement: a capacity probe across all
five `us-east-1` AZs, then a full stack deployed on `g4dn.2xlarge` running the identical
Phase 1 benchmark.

### Availability and price

Real launch-and-terminate probes, all within a few minutes on 2026-08-21:

| Type | AZs with capacity | $/hr | vCPU | RAM | GPU | VRAM |
|---|---|---|---|---|---|---|
| `g6.2xlarge` | **1 of 5** (`1d`) | $0.978 | 8 | 32 GiB | L4 | 22.4 GiB |
| `g5.2xlarge` | 3 of 5 | $1.212 | 8 | 32 GiB | A10G | 22.4 GiB |
| **`g4dn.2xlarge`** | **5 of 5** | **$0.752** | 8 | 32 GiB | T4 | 15 GiB |

`g5` is the worst of the three — more expensive than `g6` and not universally available.
`g4dn.2xlarge` is available everywhere, 23 % cheaper than `g6.2xlarge`, with identical vCPU
and RAM.

### The stack runs unmodified on T4

`create-stack` with `InstanceType=g4dn.2xlarge` reached `CREATE_COMPLETE` with no template
changes beyond widening `AllowedValues`. Build 24 m 35 s (vs 23 m 21 s on `g6.2xlarge`,
~5 % slower). Root disk 111 GiB — identical. All four services healthy, `torch 2.4.1+cu121`
on `Tesla T4` in both GPU containers. The volume detector correctly skipped this family's
209 GB instance store, which the DLAMI again pre-formats as LVM at `/opt/dlami/nvme`.

### Benchmark: the same `sacre_coeur` reconstruction

| | `g6.xlarge` (L4, 4 vCPU) | `g4dn.2xlarge` (T4, 8 vCPU) |
|---|---|---|
| Total, cold (incl. NetVLAD download) | 39.4 s | 39.6 s |
| Total, warm | not measured | 21.8 s |
| Registered images | 10 / 10 | 10 / 10 |
| 3D points | 1786 | 1791–1793 |
| Mean reprojection error | 0.985 px | 0.985 px |
| Peak VRAM allocated | 1353.9 MB | 1353.9 MB |
| Peak VRAM reserved | 3556 MB | 3270 MB |
| Peak host RAM | 2849 MB | 2812 MB |
| Model-load VRAM, native res | 6044 MB | 6044 MB |

Reconstruction quality is identical to three decimal places. VRAM behaviour is identical,
and 6 GB at native resolution fits T4's 15 GiB with room to spare.

**Caveat on the comparison — it is not perfectly controlled.** The L4 run was on a 4-vCPU
`g6.xlarge`; the T4 run was on an 8-vCPU `g4dn.2xlarge`. Total wall time landing within
0.2 s of each other therefore reflects both the slower GPU and the extra CPU, not the GPU
alone.

### Where the T4 actually loses, and why it may matter

Per-stage, warm, on T4 — compared against the L4 stages captured in Phase 1:

| Stage | L4 | T4 | Note |
|---|---|---|---|
| `superpoint-extract` | 0.4 s | 0.5 s | negligible |
| `superglue-match` | 3.7 s | **6.5 s** | **~76 % slower — the GPU-bound stage** |
| `colmap-reconstruction` | 3.8 s | 4.9 s | CPU-bound |
| `netvlad-retrieval-extract` | not captured | 9.6 s | — |

SuperGlue matching is where the older GPU shows. On 10 images that is 6.5 s of a 21.8 s run
and irrelevant. **On a real scan it will not be irrelevant:** retrieval-based matching scales
with pairs (roughly `n × k`), so a 500-frame scan does ~50× the matching of this test, and a
76 % penalty on the dominant stage becomes real minutes.

### Recommendation

**Default to `g4dn.2xlarge`, keep `g6.2xlarge` as a parameter.** A service that cannot start
is worse than one that builds maps more slowly, and 5-of-5 availability against 1-of-5 is
the difference between a waker that works and one that intermittently does not. It is also
23 % cheaper for identical CPU, RAM, and reconstruction quality.

The honest counter-argument: nobody has yet measured a real multi-hundred-frame scan on
either GPU, and that is exactly where the T4's matching penalty concentrates. If map build
time turns out to matter more than availability, `g6.2xlarge` is one parameter away — but
that AZ must then be probed for `g6` capacity first, and pinned before the maps volume
exists.

---

## FusionAuth: where it should live

An earlier note in this file called this "the biggest unresolved design question" and said
FusionAuth needs ~4 GB with Postgres *and* Elasticsearch, so it cannot sit on a `t4g.nano`
and would be offline whenever the GPU host is stopped. **Two thirds of that was wrong**, and
the correction changes the answer.

### Correction 1 — Elasticsearch is optional

FusionAuth's own system requirements state Elasticsearch is optional, with a database-backed
search engine as the alternative, selected by `FUSIONAUTH_SEARCH_TYPE=database`. Their
docker-compose ships an OpenSearch container, and the documentation explicitly says it can
be removed once the search type is changed. The ~4 GB figure came from reading OpenVPS's
`docs/FusionAuth.md`, which just points at that stock compose file.

Actual requirements without a search engine:

| Component | RAM |
|---|---|
| FusionAuth | 512 MB minimum, 1 GB recommended |
| PostgreSQL | 1–2 GB for light usage |
| **Total** | **~2–3 GB** |

### Correction 2 — "offline while the GPU is stopped" barely matters

The concern was that a user arriving at a cold system could not log in. Trace the actual
flow: the user hits the waker, gets the holding page, the waker calls `StartInstances`, the
GPU host boots, and only *then* does the waker proxy them to MapBuilder — which is the point
at which MapBuilder redirects to the FusionAuth issuer. By then FusionAuth is up. The user
cannot log in while the host is stopped, but they cannot do anything else either, and the
holding page already covers that window.

The brief's own architecture diagram put `fusionauth` and `postgres` on the GPU instance.
That was right; the objection was not.

### Options, with real numbers

| Option | Standing cost | Notes |
|---|---|---|
| **On the GPU host** | **$0** | 2–3 GB on a 32 GiB box is nothing. Down when the host is down, which does not matter. |
| `t4g.small` always-on | $12.26/mo | 2 GB — tight for FusionAuth + Postgres together |
| `t4g.medium` always-on | $24.53/mo | 4 GB — comfortable. Auth stays up independent of the GPU. |
| FusionAuth Cloud | from **$75/mo** | Lowest tier. Poor value at this scale. |
| AWS Cognito | ~$0 (free tier 10k MAU) | **Does not drop in** — see below |

### Why Cognito does not simply drop in

Tempting, because it is managed and effectively free. But upstream pins the identity
provider harder than it first appears. Both services use the Auth.js FusionAuth provider and
derive every endpoint from `AUTH_FUSIONAUTH_ISSUER`:

```js
userinfo:      issuer + "/oauth2/userinfo"
authorization: issuer + "/oauth2/authorize"
token:         issuer + "/oauth2/token"
params: { scope: "offline_access openid profile email" }
```

Cognito is close but not equal:

- Its user-attributes endpoint is `/oauth2/userInfo` — **capital I**. Upstream sends
  lowercase.
- Upstream hardcodes `offline_access` in the requested scope. Cognito's system-reserved
  scopes are `openid`, `email`, `phone`, `profile` and `aws.cognito.signin.user.admin`;
  requesting a scope not associated with the app client fails authentication. Whether
  `offline_access` can be made to work on Cognito is genuinely unclear from the
  documentation and would need testing.

Both could in principle be papered over in Caddy — rewrite the path case, strip the scope —
but that is a fragile proxy hack sitting in the authentication path, to save money that a
`t4g.medium` would cost anyway. Not recommended without testing that actually settles the
scope question.

### Recommendation

**Run FusionAuth on the GPU host**, as a second compose file alongside (never inside)
upstream's, with `FUSIONAUTH_SEARCH_TYPE=database` and no search container. Cost: nothing.

Three things that need care:

1. **Postgres data must outlive the instance.** Put it on the retained data volume, not the
   root volume and not an anonymous docker volume. That means changing the data volume's
   mount point from `/home/ubuntu/data/maps` to `/home/ubuntu/data`, keeping
   `MY_SHARED_MAPS_DIR=/home/ubuntu/data/maps` as a subdirectory, and giving FusionAuth's
   Postgres `/home/ubuntu/data/fusionauth-db`. User accounts then survive stack deletion
   exactly as maps do.
2. **Cold start grows.** FusionAuth plus a Postgres first-run migration adds perhaps 30–60 s
   on top of the ~110 s already measured for wake plus container start. That eats into the
   three-minute target and should be measured, not assumed.
3. **The issuer URL is load-bearing.** `AUTH_FUSIONAUTH_ISSUER` must be the public
   `https://auth.<domain>`, and FusionAuth's configured redirect URLs must match the public
   MapBuilder and MapAligner URLs exactly, or login fails with a misconfiguration error.
   Kickstart JSON can pin this configuration reproducibly rather than by hand.

Choose the `t4g.medium` instead only if auth needs to be available independently of the GPU
host — for example if other OARC services should share the same identity provider. That is
a product decision, not a technical one.

---

## FusionAuth on the GPU host — implementation

Decided per the analysis above. FusionAuth 1.69.0 and PostgreSQL 16.9 run as their own
compose project (`openvps-auth`), never inside upstream's file.

### Kickstart is what makes this reproducible

The client id and secret must agree in two places at once: inside FusionAuth, and in the
`docker.env` the apps read. The usual sequence — configure FusionAuth by hand, let it
generate a client secret, copy that into Secrets Manager — has to be repeated on every
rebuild and is the sort of manual step that silently rots.

`kickstart/kickstart.json` inverts it. The values are generated once *into* Secrets Manager,
and kickstart tells FusionAuth to adopt them:

```
POST /api/application/#{ENV.AUTH_FUSIONAUTH_ID}
  oauthConfiguration.clientSecret = #{ENV.AUTH_FUSIONAUTH_SECRET}
  authorizedRedirectURLs = [ #{ENV.MAPBUILDER_URL}/auth/callback/fusionauth, ... ]
```

Kickstart applies only against an empty database, so it configures a fresh deployment and
never fights an existing one. It also pins the redirect URLs, which upstream's own docs call
out as the thing that must match exactly or login fails with a misconfiguration error.
Request order matters — the application must exist before a user can be registered to it.

No search container: `FUSIONAUTH_SEARCH_TYPE=database`, per FusionAuth's documented option.

### Postgres data lives on the retained volume

The data volume now mounts at `/home/ubuntu/data` rather than directly at the maps
directory, with `maps/` and `fusionauth-db/` as siblings. That database holds every user
account **and** the OAuth client secret the apps authenticate with — losing it does not just
lose logins, it strands the credentials the apps are configured with. It has to survive
stack deletion exactly as the maps do, so it belongs on the `Retain`ed volume rather than
the root disk or an anonymous docker volume.

Ordering: `openvps.service` is `After=openvps-auth.service`, so the apps never start against
a dead issuer. The idle detector counts port 9011 and the FusionAuth container as activity —
a login in flight must not be shut down under — and on shutdown stops the apps before the
database, so Postgres closes cleanly rather than being killed mid-transaction.

## User-data hit its 16 KB ceiling, so config moved into stack Metadata

Adding FusionAuth pushed user-data to 16,465 bytes against a hard 16,384 limit, with roughly
8 KB of FusionAuth files still to add. Inlining everything had simply run out of room.

Config files, scripts and unit files now travel in the **Instance resource's `Metadata`**,
which rides in the template (51,200-byte limit) rather than in user-data. At boot, user-data
reads them back with `describe-stack-resource` and writes them to disk.

| | before | after |
|---|---|---|
| user-data | 16,465 / 16,384 — over | **8,441 / 16,384** |
| template | 34,510 / 51,200 | **40,422 / 51,200** |

Payloads are gzipped and base64'd. Plain text worked but left 263 bytes of template
headroom, which the Phase 4 Caddyfile would have erased immediately. Base64 also contains
nothing YAML or JSON must escape, so the whole class of block-scalar indentation bugs
disappears.

Two useful side effects: the payloads need no `Fn::Sub` escaping at all — no more `${!VAR}`
— and they are byte-identical to the annotated copies under `deploy/aws/`, comments and all,
instead of stripped-down duplicates.

### The build step, and why it is a real tradeoff

`template.yaml` is now **generated** by `build.py` from `template.src.yaml` plus the files
it ships. `build.py --check` fails if the generated file is stale, which is the CI hook.

This exists to kill duplication: without it the same payloads live twice — once readably in
`deploy/aws/`, once pasted into the template — free to drift apart with nothing to catch it.

The cost is honest and worth stating: `template.yaml` is no longer hand-editable. Anyone who
edits it directly loses their change on the next build. The header says so, and `--check`
catches it in CI, but it is a real papercut.

**The alternative, if that tradeoff is unwelcome:** publish this repository and have
user-data `git clone` it at boot, exactly as it already clones upstream and HLOC. That
removes the size pressure and the duplication in one move, with no build step. It was not
done here only because the repo is not published yet. Worth revisiting before Phase 4 adds
the Caddyfile.

### Verified: 30/30 on a deployed stack

Files delivered from Metadata with correct modes, Postgres and FusionAuth healthy on the
retained volume, kickstart applied, and the issuer correct:

```
name        : OpenVPS
clientId    : <the value from Secrets Manager, adopted by FusionAuth>
redirectURLs: https://build.vps.cloudpose.io/auth/callback/fusionauth
              https://align.vps.cloudpose.io/api/auth/callback/fusionauth
grants      : ['authorization_code', 'refresh_token']
issuer      : https://auth.vps.cloudpose.io
```

A clean start of the auth stack from an empty database takes **23 s** through systemd —
better than the 30–60 s estimated, so the effect on the three-minute cold-request target is
smaller than feared.

### Four bugs this exposed, three of them silent

**1. Postgres cannot create its data directory in a root-owned mount.** The host directory
was created `root:root` mode 700; the alpine Postgres image runs as uid 70. The container
entered a restart loop logging `mkdir: can't create directory '/var/lib/postgresql/data/pgdata':
Permission denied`. Fixing the ownership afterwards is not enough on its own — a half-made
`pgdata` is left behind and has to be removed before initdb will run. The directory must be
owned by uid 70 *before* the container first starts.

**2. The stack reported CREATE_COMPLETE with the identity provider dead.** This is the worst
of the four. `openvps.service` only `Wants` `openvps-auth.service`, so a broken FusionAuth
does not stop the apps, and user-data only started the apps. The wait condition got its
success signal while login was completely non-functional. A false green is worse than a
failure: nothing looks wrong until someone tries to log in. User-data now starts the auth
unit explicitly, dumps compose state and both containers' logs on failure, and fails the
stack.

**3. Kickstart aborts entirely on an undefined variable.** `#{defaultTenantId}` is not
implicitly available. Referencing it produced one log line —
`You may not use an undefined variable` — and **no** API key, **no** application, nothing.
Everything downstream then failed in ways that pointed elsewhere: the API key was missing,
so tenant queries returned non-JSON, which looked like a FusionAuth problem rather than a
kickstart syntax problem. Kickstart is all-or-nothing; check its log line on first boot.

**4. `FUSIONAUTH_APP_URL` is an internal address, not the public issuer.** Setting it to the
public hostname made every internal cache-reload notification fail
(`Failed to request a cache reload for [Instance]`), which left the OIDC discovery document
serving a stale issuer even though the tenant record was correct. Two different concepts
that both look like "the URL of this FusionAuth": the tenant *issuer* is the public
identity that goes into JWTs and discovery; `FUSIONAUTH_APP_URL` is how the node reaches
itself. Only the first should ever be public.

And one false alarm worth recording: the discovery document is cached for a few seconds
after a tenant PATCH, so verifying the issuer immediately reports the old value. The check
now polls for up to 120 s. The verification itself earned its place — it is what caught
bug 4.

### Why the issuer is set from the API rather than kickstart

Kickstart only runs against an empty database. Setting the issuer there would mean a later
change to `DomainName` silently leaves a stale issuer in place, with login failing at token
validation for reasons nothing surfaces. `openvps-fusionauth-configure` runs as
`ExecStartPost` on every start, is idempotent, and verifies the result, so the issuer tracks
the stack parameter.
