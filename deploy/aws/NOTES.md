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

_In progress._
