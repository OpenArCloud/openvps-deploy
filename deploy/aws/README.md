# Deploying OpenVPS on AWS

Operator guide. For what this repo is and how the pieces fit, see the
[root README](../../README.md). For measurements and the friction log, see [NOTES.md](NOTES.md).

## Before you start

1. **Quota.** `Running On-Demand G and VT instances` needs to be at least 8 vCPU:
   ```sh
   aws service-quotas get-service-quota --service-code ec2 --quota-code L-DB2E81BA
   ```
2. **A secret.** A Secrets Manager secret holding JSON with these keys:

   | key | what it is |
   |---|---|
   | `AUTH_SECRET` | Auth.js session secret, shared by both apps |
   | `AUTH_FUSIONAUTH_ID` | OAuth client id, a UUID. FusionAuth adopts this value |
   | `AUTH_FUSIONAUTH_SECRET` | OAuth client secret, likewise adopted |
   | `FUSIONAUTH_DB_PASSWORD` | Postgres password |
   | `FUSIONAUTH_API_KEY` | FusionAuth API key, created by kickstart |
   | `FUSIONAUTH_ADMIN_EMAIL` | first admin user |
   | `FUSIONAUTH_ADMIN_PASSWORD` | their password |

   You generate these; FusionAuth is configured to use them rather than making its own. That
   way the client id and secret agree on both sides without any copying back and forth.
   `AUTH_FUSIONAUTH_ISSUER` is optional and defaults to `https://auth.<DomainName>`.

   `AUTH_FUSIONAUTH_SECRET` must be **alphanumeric only**. Auth.js authenticates with
   `client_secret_basic`, and a base64 secret containing `+`, `/` or `=` makes FusionAuth
   reject the token exchange with `invalid_client` even though both sides hold identical
   values. Generate it with:

   ```sh
   tr -dc 'A-Za-z0-9' </dev/urandom | head -c 48
   ```
3. **A domain**, with a Route 53 hosted zone. Four names point at the waker: `build`,
   `align`, `vps` and `auth`. Or use `sslip.io` against the waker's IP and skip DNS setup.
4. **An availability zone with capacity.** See below.

## Choosing an AZ

This choice is permanent. The maps volume is retained so it outlives stacks, and an EBS
volume is bound to one AZ for life, so the instance can only ever launch there. There is no
multi-AZ fallback because a fallback that stranded the maps would be worse than a failed
launch.

GPU capacity is scarce and it moves. Within a couple of hours in `us-east-1` we saw
`g6.xlarge` available in `1c` and nowhere else, then `g6.2xlarge` available in `1c` and `1d`
but not `1a`, `1b` or `1f`, then `1d` lose it too. Probe before you commit.

`run-instances --dry-run` will not tell you this. It only checks permissions and happily
returns success in a zone with no capacity at all. You have to actually launch:

```sh
AMI=$(aws ssm get-parameter \
  --name /aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id \
  --query Parameter.Value --output text)

for SUB in <subnet-a> <subnet-b> <subnet-c> <subnet-d> <subnet-f>; do
  ID=$(aws ec2 run-instances --image-id "$AMI" --instance-type g4dn.2xlarge --subnet-id "$SUB" \
        --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":75,"VolumeType":"gp3","DeleteOnTermination":true}}]' \
        --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=capacity-probe}]' \
        --query 'Instances[0].InstanceId' --output text 2>&1)
  case "$ID" in
    i-*) echo "$SUB yes"; aws ec2 terminate-instances --instance-ids "$ID" >/dev/null ;;
    *)   echo "$SUB no" ;;
  esac
done
```

Pick a zone that has capacity for your fallback instance type as well, not just the default.
Resizing is a stop, modify and start in the pinned AZ, so if that zone has `g4dn.2xlarge`
but not `g6.2xlarge` you can never move up.

`g4dn.2xlarge` is the default because it was available in all five zones when `g6.2xlarge`
was available in one, costs 23% less, and produces identical reconstructions. Its T4 is
slower at SuperGlue matching, which matters more as scans get larger. NOTES.md has the
numbers.

## Using it

Two things trip people up on first use:

**Keep the zip's original name.** `stray_zip_extract.py` works out the directory it expects
inside the archive from the filename, so `c041e1cfc2.zip` must still be called that. Rename
it to `scan.zip` and extraction fails with `No such file or directory: /tmp/tmpXXXX/scan`,
which does not obviously point at the filename.

**A map needs a georeference before it can be served.** MapLocalizer's `load_map` requires a
`transform.json` beside the reconstruction and returns
`Failed to load map transform <id>` without one. MapAligner writes it; you can also POST one
directly:

```sh
curl -X POST -H 'Content-Type: application/json' \
  -d '{"latitude":47.4979,"longitude":19.0402,"height":100.0,
       "matrix":[[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]}' \
  https://build.<domain>/maps/<datasetId>/hloc/<mapId>/transform
```

## Deploying

```sh
python3 build.py     # regenerate template.yaml after changing any file it ships

aws cloudformation create-stack --stack-name openvps-aws \
  --template-body file://template.yaml \
  --capabilities CAPABILITY_IAM \
  --parameters \
    ParameterKey=VpcId,ParameterValue=vpc-xxxx \
    ParameterKey=SubnetId,ParameterValue=subnet-xxxx \
    ParameterKey=SecretsManagerArn,ParameterValue=arn:aws:secretsmanager:... \
    ParameterKey=DomainName,ParameterValue=vps.example.org
```

About 25 minutes to first boot. Use `--on-failure DO_NOTHING` while you are changing
user-data, otherwise a rollback terminates the instance and takes
`/var/log/openvps-bootstrap.log` with it.

The subnet has to be public with auto-assign public IPv4 on. There is no NAT gateway, and
user-data checks for egress up front so a misconfigured subnet fails in seconds rather than
after the build.

Useful parameters: `InstanceType`, `RootVolumeSize` (200, and 150 is the practical floor),
`MapsVolumeSize`, `IdleShutdownMinutes` (30, or 0 to disable), `AttachElasticIp` (off, since
the waker reaches the host by private IP).

## Variants

`Variant` chooses which OpenVPS is deployed. Everything else — instance sizing, storage,
idle shutdown, the waker, the secret — is shared.

| | `geopose` (default) | `spatialdds` |
|---|---|---|
| Upstream | `openvps:main` | `openvps:spatialdds` |
| Protocol | OSCP GeoPose Protocol over HTTP | SpatialDDS 1.7 VPS binding over DDS, plus the HTTP endpoint |
| Extra ports | — | 8088 from the waker; RTPS 7400–7500 between hosts in the group |

```sh
aws cloudformation create-stack --stack-name openvps-spatialdds \
  --template-body file://template.yaml \
  --capabilities CAPABILITY_IAM \
  --parameters ParameterKey=Variant,ParameterValue=spatialdds \
               ParameterKey=VpcId,ParameterValue=vpc-xxxx \
               ...
```

Both variants pin an explicit commit, in the `VariantConfig` mapping in
`template.src.yaml`. Neither tracks a branch: a branch would let two deployments of the
same stack run different software with nothing recording the difference. `UpstreamRef`
overrides the pin for a one-off test and is empty by default.

`Variant` cannot usefully be changed on an existing stack — the clone happens once, on
first boot, and user-data does not re-run. Deploy a second stack.

### The spatialdds variant

`maplocalizer` runs with `network_mode: host`, because RTPS advertises the address it binds
to and a container address is not routable from another host. One consequence to know: that
takes it off the Compose bridge network, so `http://maplocalizer:8000` stops resolving. The
override repoints the backend at `host.docker.internal`; any other service that reaches
`maplocalizer` by name needs the same.

A VPC carries no multicast, so `cyclonedds.xml` disables it and discovers against an
explicit peer list. Empty is right for one host. For two, pass the other's private IP:

```sh
ParameterKey=DdsPeers,ParameterValue='udp/10.0.1.42'
```

The web bridge is not deployed yet — it is vendored into the branch in a later phase. The
pieces around it are already in place and inert: the 8088 ingress rule, the idle detector's
extra port and container, and the `WebBridgeEndpoint` output. When it lands it needs a
waker route:

```
vps-bridge.<domain> {
    reverse_proxy <private-ip>:8088
}
```

Caddy proxies WebSockets natively. There is no waker in this repository yet, only its
security group — so that route has nowhere to go until one exists.

## Getting at it

No SSH, no key pair.

```sh
aws ssm start-session --target <instance-id>

# MapBuilder on http://localhost:8080
aws ssm start-session --target <instance-id> \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["80"],"localPortNumber":["8080"]}'
```

Ports on the GPU host, all reachable only from the waker's security group: 80 MapBuilder,
3001 MapAligner, 8000 MapLocalizer, 9011 FusionAuth. Port 443 on the frontend is bound to
loopback and carries upstream's self-signed cert; ignore it, Caddy terminates TLS.

## Idle shutdown

A timer checks every five minutes and stops the instance once it has been idle for
`IdleShutdownMinutes`. Idle means no CUDA process, no off-box connection to a service port,
no non-loopback HTTP in any service log, and nothing written under the maps directory.

That last condition is not in the original design and it matters. COLMAP's bundle adjustment
is CPU-bound and can run for many minutes holding no CUDA context and serving no HTTP, so
without it the instance would shut down mid-reconstruction.

The instance is stopped, not terminated, so the root volume keeps the built images and the
HLOC cache and a wake skips the 25-minute build.

The detector is shared by both variants and extended, not replaced, by `EXTRA_PORTS`,
`EXTRA_CONTAINERS` and `HEARTBEAT_FILE` in `idle.conf`. The `spatialdds` variant adds the
web bridge's port and container, so a browser holding a WebSocket counts as activity.

A native DDS client is invisible to the four checks above: RTPS is UDP so it never shows as
an established TCP connection, it writes no HTTP log, and the GPU check samples an instant
between requests. `HEARTBEAT_FILE` is the fifth check — the localizer touches it when it
serves a DDS request. Until the localizer writes it, the check is inert and an idle-looking
host can stop under a connected DDS client.

```sh
sudo sed -i 's/^IDLE_MINUTES=.*/IDLE_MINUTES=60/' /opt/openvps/idle.conf
journalctl -t openvps-idle -n 20        # what it decided, and why
```

A wake can fail. `StartInstances` needs capacity in the pinned AZ and there is nowhere else
to go. We saw it fail on the first attempt and succeed on the second 51 seconds later, so
whatever calls it has to retry.

## Cost

| | |
|---|---|
| `g4dn.2xlarge`, awake | $0.752/hr |
| `g6.2xlarge` alternative | $0.978/hr |
| `t4g.nano` waker | ~$3.07/mo |
| two 200 GB gp3 volumes | ~$32/mo |

About $39/month standing, plus roughly $0.75 per awake hour. Two hours a day works out
around $85/month.

The root volume is 200 GB because it has to be. The Deep Learning AMI is 50 GB before
anything is built, steady state after building all four images is 112 GB, and an in-place
rebuild needs another 71 GB of build cache on top.

## Moving to another AZ

Expect to need this eventually, given how capacity moves.

```sh
# 1. Stop the host so the filesystem is quiet, then snapshot the maps volume.
aws ec2 stop-instances --instance-ids <instance-id>
aws ec2 wait instance-stopped --instance-ids <instance-id>
SNAP=$(aws ec2 create-snapshot --volume-id <maps-volume-id> \
        --description "openvps maps pre-AZ-migration" --query SnapshotId --output text)
aws ec2 wait snapshot-completed --snapshot-ids "$SNAP"

# 2. Delete the stack. The maps volume is retained, not destroyed.
aws cloudformation delete-stack --stack-name openvps-aws
aws cloudformation wait stack-delete-complete --stack-name openvps-aws

# 3. Recreate in the new AZ. Then create a volume from $SNAP there, stop the instance,
#    detach the empty maps volume, attach the restored one as /dev/sdf and start.
#    User-data mounts by UUID and will not reformat a volume that already has a filesystem.
```

Keep the old volume until the new one checks out, then delete it. An idle 200 GB gp3 volume
is about $16/month.

## Tearing down

```sh
aws cloudformation delete-stack --stack-name openvps-aws
```

The maps volume has `DeletionPolicy: Retain`, so this leaves it behind on purpose. Two
consequences: a recreated stack will not adopt it, you have to attach it by hand; and it
keeps billing until you delete it.

If you want the maps but not the bill, snapshot the volume and delete it. Snapshots are
charged on used blocks after compression, so a volume holding a couple of maps costs pennies
a month against $16 for an idle 200 GB volume.

```sh
SNAP=$(aws ec2 create-snapshot --volume-id <maps-volume-id> \
        --description "openvps maps" \
        --tag-specifications 'ResourceType=snapshot,Tags=[{Key=Project,Value=openvps-aws}]' \
        --query SnapshotId --output text)
aws ec2 wait snapshot-completed --snapshot-ids "$SNAP"   # only delete once this returns
aws ec2 delete-volume --volume-id <maps-volume-id>
```

Restoring: create a volume from the snapshot in the AZ of the new stack, stop the instance,
detach the empty maps volume, attach the restored one as `/dev/sdf`, start. User-data mounts
by UUID and will not reformat a volume that already has a filesystem.

```sh
aws ec2 describe-volumes --filters Name=tag:Project,Values=openvps-aws Name=status,Values=available \
  --query 'Volumes[].{Id:VolumeId,Size:Size,AZ:AvailabilityZone,Created:CreateTime}' --output table
```
