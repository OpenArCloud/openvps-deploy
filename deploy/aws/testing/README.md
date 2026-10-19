# End-to-end test method

The exact method used to produce the baseline numbers in NOTES.md, so a second
implementation can be held to the same measurement rather than a re-invented one.

Run everything on the instance over SSM. There is no ingress.

## 0. Get the scan onto the instance

There is no ingress and `send-command` will not carry 120 MB, so stage it in S3 and pull it
with the instance role. **The template's role has no S3 permission** — it is scoped to one
secret, one log group and `DescribeStackResource`. Add a temporary policy and remove it at
teardown rather than putting S3 in the template.

```sh
BUCKET=openvps-scan-$(openssl rand -hex 6)
aws s3api create-bucket --bucket "$BUCKET"
aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3 cp c041e1cfc2.zip "s3://$BUCKET/c041e1cfc2.zip"   # keep the original filename

ROLE=$(aws cloudformation describe-stack-resource --stack-name <stack> \
        --logical-resource-id InstanceRole \
        --query 'StackResourceDetail.PhysicalResourceId' --output text)
aws iam put-role-policy --role-name "$ROLE" --policy-name scan-read --policy-document "{
  \"Version\":\"2012-10-17\",
  \"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\"],
                  \"Resource\":\"arn:aws:s3:::$BUCKET/*\"}]}"

# on the instance
aws s3 cp s3://$BUCKET/c041e1cfc2.zip /home/ubuntu/c041e1cfc2.zip

# teardown
aws iam delete-role-policy --role-name "$ROLE" --policy-name scan-read
aws s3 rm "s3://$BUCKET" --recursive && aws s3api delete-bucket --bucket "$BUCKET"
```

## 1. Log in

FusionAuth session handling is fiddly and incidental to whatever you are actually testing.
`oauth-login.py` does the whole authorization-code flow and leaves a cookie jar at
`/tmp/py-cookies.txt` that `curl -b` can use.

```sh
python3 oauth-login.py          # on the instance
```

It reads `MAPBUILDER_URL` and `AUTH_FUSIONAUTH_ISSUER` from `/opt/openvps/docker.env`, and
the admin credentials from `/opt/openvps/fusionauth.env`. Success looks like:

```
/maps without a session -> HTTP 401
signin -> 302
step 1: login page HTTP 200, 1 form(s)
  posted 22 fields -> 302
step 2: app callback -> HTTP 200
/auth/session -> {"user":{"email":"..."},"expires":"..."}
/maps with a session -> HTTP 200: {"statuses":[]}
```

Three things break this and none of them say so clearly:

- **FusionAuth must be 1.49.2.** On 1.50+ the hardcoded `offline_access` scope forces a
  consent screen that returns HTTP 500. The container is healthy either way; only a login
  reveals it.
- **`AUTH_FUSIONAUTH_SECRET` must be alphanumeric.** Base64 punctuation breaks
  `client_secret_basic` with `invalid_client`, even though both sides hold identical values.
- The login form carries ~22 hidden fields (state, nonce, PKCE challenge, tenantId). Posting
  only credentials silently re-renders the form. The script replays them all.

## 2. Upload and map

```sh
# The extractor derives the expected inner directory from the zip's filename stem, so the
# archive must keep its original StrayScanner name.
curl -b /tmp/py-cookies.txt \
  -F "map-name=test" -F "file=@/home/ubuntu/c041e1cfc2.zip" \
  "$MAPBUILDER_URL/uploadStrayRecordingZip"

# poll /maps until datasetExtract is Completed, then:
curl -b /tmp/py-cookies.txt -X POST -H 'Content-Type: application/json' \
  -d '{"program_steps":{"hloc_extract_features":true,"hloc_find_image_pairs":true,
                        "hloc_matches_from_pairs":true,"hloc_build_model":true},
       "hloc_reconstruction":{"feature_conf":"superpoint_aachen","matcher_conf":"superglue",
                              "retrieval_conf":"netvlad","pairs_strategy":"from_retrieval",
                              "optimize_poses":true}}' \
  "$MAPBUILDER_URL/maps/$DATASET_ID/hloc/registerConfig"     # returns mapId

curl -b /tmp/py-cookies.txt -X POST "$MAPBUILDER_URL/maps/$DATASET_ID/hloc/$MAP_ID/process"
```

## 3. Select the map

```sh
curl -b /tmp/py-cookies.txt -X PUT "$MAPBUILDER_URL/maps/$DATASET_ID/select"
curl "$MAPLOCALIZER_URL/current_map_id"      # poll until it reports your mapId
```

On `b2dff1f` this works straight away: `hlocMapScaleEstimation` writes `transform.json`
during mapping. On older commits it fails with `Failed to load map transform` until one
exists — see below.

## 4. Measure

```sh
docker cp localization-eval.py openvps-maplocalizer-1:/tmp/
docker exec openvps-maplocalizer-1 python3 /tmp/localization-eval.py "$DATASET_ID" "$MAP_ID"
```

The method:

- **Query images are members of the map.** This is a self-consistency check, not an accuracy
  measurement. That is deliberate: it is deterministic, so any deviation is the transport or
  the pipeline, not the vision. It is the right shape for comparing two paths against each
  other, and the wrong shape for claiming absolute accuracy.
- **Ground truth** is `pycolmap`'s `projection_center()` for the same image, read from the
  reconstruction the server itself loaded.
- **Compared against `resp["poses"][0]["pose"]["t"]`, with no transform applied.** This is
  the trap: `poses` come back in the map's own reconstruction frame, with **neither** the
  metric scale from `transform.json` **nor** the graphics→ENU rotation. Applying either
  produces a multi-metre error that is entirely an artefact of the comparison.
- Frames are selected **by name**, not by index into a sorted list, so a different
  registration order cannot silently change what is being compared.

## Baseline

Scan `c041e1cfc2.zip`, 2143 frames, `g4dn.2xlarge`, upstream `b2dff1f`:

```
reconstruction : 82 registered images, 14,885 points3D, 1.22107 px mean reprojection error
transform.json : latitude/longitude/height = null, matrix scale = 0.241733

frame_0633.jpg   2.29 s   3.44 mm
frame_1067.jpg   3.83 s   3.75 mm
frame_1680.jpg   2.86 s   9.10 mm

mean 5.43 mm in map units (0.13 cm metric), mean latency 2.99 s
```

**Do not compare against the earlier 0.5 cm figure in NOTES.md.** That was a different
measurement on `fc2f470`: a hand-POSTed geodetic transform, comparing returned `lat/lon/h`
against an expected geodetic position. Same scan, same 82 images, different quantity. Mixing
the two is exactly the confusion this file exists to prevent.

## Getting the ids

The upload returns `200` with an **empty body**, so the dataset id comes from `/maps`:

```sh
C="-b /tmp/py-cookies.txt"; B=$MAPBUILDER_URL
DS=$(curl -s $C "$B/maps" | python3 -c \
  "import json,sys;print([s['metadata']['id'] for s in json.load(sys.stdin)['statuses'] if s['metadata']['name']=='test'][0])")

MAP=$(curl -s $C -X POST -H 'Content-Type: application/json' -d @config.json \
        "$B/maps/$DS/hloc/registerConfig" | python3 -c "import json,sys;print(json.load(sys.stdin)['mapId'])")
```

`registerConfig` returns `{"mapId": ...}` directly. Match on `metadata.name` rather than
taking `statuses[0]` once more than one dataset exists.

## Loading a map without the UI

`PUT /maps/{ds}/select` is a convenience wrapper: the backend looks up the dataset's first
hloc `mapId` and calls `GET {MAPLOCALIZER_URL}/load_map/{mapId}`. It is not the only route.

`GET /load_map/{mapId}` on the localizer works directly and needs no session — the localizer
has no auth of its own; it is reachable only because the security group allows the waker.
That is the simpler call if you already know the mapId. `select` additionally returns a
friendly "may take several minutes" body on timeout, which `load_map` does not.

The pool holds `maxLoadedMaps` (8 by default, set in the image's `server/.env`). Loading a
second map does **not** evict the first below that limit — both stay resident and eviction is
LRU only once the count reaches the limit. But `currentMapId` moves to whichever was loaded
or touched last, and `POST /localize/geopose` resolves against `currentMapId`. So with two
maps loaded, queries go to the most recently selected one; there is no per-request map
argument. `GET /health` reports both `loaded_map_ids` and `current_map_id`.

## Wall clock, measured

On `g4dn.2xlarge`, upstream `b2dff1f`, the 2143-frame scan:

| | |
|---|---|
| `create-stack` to instance running | ~2 min (IAM instance-profile consistency) |
| First-boot build (clone, HLOC submodules, four images) | **26 m 03 s** |
| Build done to all four healthy (prune, compose up) | ~1 m 45 s |
| **create-stack to serving** | **~30 min** |
| Login | seconds |
| S3 pull of the 120 MB scan | ~2 s |
| Upload through the API | 0.26 s (loopback) |
| `datasetExtract` + thumbnail | under 30 s |
| **Mapping, whole `process` run** | **217.7 s** (`hlocMapBuild` 193.3 s of it) |
| Map load into the localizer | under 10 s |
| One localization query | ~3 s |

So roughly **35–40 minutes from nothing to a queryable map**, and about **4 minutes per
additional map** on an instance that is already up. The expensive part is once per instance,
not per test: build once, map once, then iterate. A one-hour booking is enough for a build
plus a map plus a large query suite; three hours is only needed if you are rebuilding images
between cases.

## If you need a georeferenced map

No genuine MapAligner georeference exists for this scan. `hlocMapScaleEstimation` writes only
a scale; the geodetic fields stay `null`, so `geopose` returns zeros and `geoposes` is empty.

You do **not** need the GUI. POST a transform directly — it is deterministic input, which is
what a comparison needs anyway:

```sh
curl -b /tmp/py-cookies.txt -X POST -H 'Content-Type: application/json' \
  -d '{"latitude":47.4979,"longitude":19.0402,"height":100.0,
       "matrix":[[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]}' \
  "$MAPBUILDER_URL/maps/$DATASET_ID/hloc/$MAP_ID/transform"
curl "$MAPLOCALIZER_URL/load_transform/$MAP_ID"      # re-read without reloading the map
```

With a geodetic anchor present, `geopose` carries real lat/lon/h. Comparing *those* needs the
full chain the server applies — graphics→ENU rotation (`+90°` about X), then the matrix, then
geodetic conversion about the anchor. That is the chain `localization-eval.py` deliberately
does **not** apply to `poses`.

## The `spatialdds` variant's deployed check

`dds-vs-http-geopose.py` is the other half of the method: it localizes one
frame twice against the *same running service* — HTTP `POST /localize/geopose`
and DDS `spatialdds/vps/query/v1` — and compares the two geodetic answers.

```sh
# on the instance, in the maplocalizer container's network namespace
./dds-vs-http-geopose.py <dataset> <map_id> --era 1.8
./dds-vs-http-geopose.py <dataset> <map_id> --era 1.7   # the cross-era lane
```

**It compares `geoposes` against `geoposes`, never against `poses`.** That
distinction is the one this file spends its length on above: `poses` are in the
map's own reconstruction frame with no transform applied, and mixing them with
a geodetic answer yields a multi-metre error that is entirely an artefact of
the comparison. Both numbers here come out of the full chain, so they are the
same quantity measured through two faces and should agree to centimetres.

**Why it exists as a file.** This measurement was driven from ad-hoc commands
in two separate AWS sessions and saved in neither, so it was rebuilt from
memory twice. The numbers it produces are only worth something if the method
that produced them is reproducible.

**`--era` is the cross-era lane against a deployed service.**
`maplocalizer/server/tests/test_cross_era_interop.py` proves a 1.7 request
still returns a pose using two participants in one process on a local bus.
`--era 1.7` asks the same question of the real thing, and reports what a
four-member `FrameRef` sees of the eight-member one the 1.8 service sends —
the scale quartet is truncated away by appendable evolution, which is the
expected behaviour and the reason the lane works. One era per run: Cyclone
checks Python class identity in `write()`.

Everything it needs from the service — topic names, QoS profiles, the
query-image role, the empty-geopose helper — is imported from
`spatialdds/service.py` rather than restated, so a rename breaks it at startup
instead of quietly making it measure nothing.
