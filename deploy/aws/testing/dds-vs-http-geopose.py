#!/usr/bin/env python3
"""One image, both faces of the same localizer: compare the geoposes.

The deployed check for the `spatialdds` variant. It asks the running service to
localize one frame twice — once over HTTP `POST /localize/geopose`, once over
DDS `spatialdds/vps/query/v1` — and compares the two geodetic answers. Both
come out of the same reconstruction through the same transform chain, so they
should agree to within floating point; a real difference means one face is
applying something the other is not, which is the only thing this comparison
can tell you and the whole reason it exists.

**This measurement has been rebuilt from memory twice.** The September AWS
session drove it from ad-hoc commands that were never saved, and so did the one
before. That is why it is a file in the repository rather than a paragraph in
NOTES.md.

**What it compares, and the mistake not to repeat.** `localization-eval.py`
compares `poses[0].pose.t` against the reconstruction's own camera centres,
with **no transform applied**, and its docstring warns at length that mixing
that number with the geodetic one produces a multi-metre error that is purely
an artefact. This script never touches `poses`. It compares `geoposes[0]` from
HTTP against `node_geo`'s geopose from DDS — geodetic against geodetic, the
same quantity on both sides. The two scripts measure different things on
purpose and their numbers are not comparable.

**Run it on the instance.** There is no ingress: HTTP is `127.0.0.1:8000` and
DDS discovery is the unicast peer list in `/etc/cyclonedds.xml`. See
`README.md` for the SSM session and the map layout.

    ./dds-vs-http-geopose.py <dataset> <map_id> [--era 1.8] [--frames 3]

**`--era` is the point of the DDS half.** The service is 1.8; the AR client
deployed against it is 1.7 and will be until it migrates. Running this with
`--era 1.7` exercises the cross-era lane against a *deployed* service rather
than the two-participant unit proof in
`maplocalizer/server/tests/test_cross_era_interop.py`, and reports what a
four-member `FrameRef` sees of an eight-member one. One era per run:
cyclonedds-python checks Python class identity in `write()`, so a request
built from one tree cannot be handed to a writer created from the other.

**Status: written, not yet run against a deployed stack.** The instance it
targets was stopped when this was written and the first run is the deploy
batch's. Everything it depends on is pinned by import from the service's own
module rather than restated here — topics, QoS, the query-image role — so the
failure mode if something has moved is an ImportError at startup rather than a
silent mismatch.
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import math
import subprocess
import sys
import time
import uuid
from pathlib import Path

HTTP = "http://127.0.0.1:8000"
SERVER = Path("/opt/openvps/maplocalizer/server")


def _import_era(era: str):
    """The two IDL trees, and the service's own constants.

    The vendored trees live beside the service on the instance. Importing the
    service module rather than restating its topics and QoS is deliberate: a
    renamed topic should break this script loudly, not make it measure nothing.
    """
    for p in (str(SERVER), str(SERVER / "vendor")):
        if p not in sys.path:
            sys.path.insert(0, p)
    from spatialdds import service as svc                      # noqa: E402
    from spatialdds_demo import blob as blobmod                # noqa: E402
    from spatialdds_demo import typed_transport as tt          # noqa: E402

    #: **Every type in the request must come from the era's own tree.**
    #: `spatialdds/service.py` is bound to 1.8, so its `_empty_geopose()` and
    #: `now_time()` helpers return 1.8 objects — borrowing them for a 1.7
    #: request puts a 1.8 `GeoPose` inside it and `write()` fails with
    #: `DDS_RETCODE_BAD_PARAMETER`, which names nothing useful. Measured on the
    #: deployed stack 2026-10-19, after the 1.8 run passed: the leak is
    #: invisible until the *other* era runs, because on 1.8 the borrowed
    #: helpers happen to be right. Build them here instead.
    if era == "1.8":
        from spatialdds_idl18.spatial.argeo import (            # noqa: E402
            QualityRequirements, VpsRequest, VpsResponse)
        from spatialdds_idl18.spatial.common import FrameRef, ScaleStatus
        # GeoPose and CovMatrix from `core`, which *defines* them. `argeo`
        # re-exports GeoPose as an annotated alias that reports zero members
        # and raises `TypeError: 'ForwardRef' object is not callable` when
        # called. Measured here on 2026-10-19 — the third time this trap has
        # cost something in this programme, and the first time after it was
        # written down. Resolve at the definition, never the re-export.
        from spatialdds_idl18.spatial.core import (
            BlobChunk, BlobRef, CovMatrix, GeoPose)
        from spatialdds_idl18.builtin import Time
    else:
        from spatialdds_idl.spatial.argeo import (              # noqa: E402
            QualityRequirements, VpsRequest, VpsResponse)
        from spatialdds_idl.spatial.common import FrameRef
        from spatialdds_idl.spatial.core import (
            BlobChunk, BlobRef, CovMatrix, GeoPose)
        from spatialdds_idl.builtin import Time
        ScaleStatus = None
    return svc, blobmod, tt, (VpsRequest, VpsResponse, QualityRequirements,
                              FrameRef, ScaleStatus, BlobChunk, BlobRef,
                              GeoPose, CovMatrix, Time)


def _client_frame(FrameRef, ScaleStatus):
    """The client's own frame. Eight members on 1.8, four on 1.7.

    The quartet is set here only so the *request* carries it on 1.8; what this
    script reports is the quartet on the **response's** frame, which is the
    service's claim about its map and the thing 1.7 cannot represent.
    """
    if ScaleStatus is None:
        return FrameRef(uuid="client", fqn="client/body",
                        has_coord_convention=False, coord_convention=0)
    return FrameRef(uuid="client", fqn="client/body",
                    has_coord_convention=False, coord_convention=0,
                    has_scale=True, scale_status=ScaleStatus.SCALE_DECLARED,
                    meters_per_unit=1.0, display_unit="m")


def _enu_metres(a: dict, b: dict) -> float:
    """Separation of two geoposes, in metres.

    A local flat-earth approximation, which is the right tool: the two answers
    are the same point computed twice and are expected to differ by
    centimetres at most, so a metre-accurate conversion over a few metres of
    separation adds nothing a geodesic would.
    """
    lat1, lon1, h1 = a["latitude"], a["longitude"], a["height"]
    lat2, lon2, h2 = b["latitude"], b["longitude"], b["height"]
    mlat = 111320.0
    dn = (lat1 - lat2) * mlat
    de = (lon1 - lon2) * mlat * math.cos(math.radians(lat1))
    return math.sqrt(dn * dn + de * de + (h1 - h2) ** 2)


def _http_localize(root: str, probe, cam) -> tuple[dict, float, str]:
    """`POST /localize/geopose`, as localization-eval.py does it."""
    p = cam.params
    if cam.model.name in ("SIMPLE_PINHOLE", "SIMPLE_RADIAL"):
        fx = fy = float(p[0]); cx, cy = float(p[1]), float(p[2])
    else:
        fx, fy, cx, cy = (float(p[i]) for i in range(4))
    path = glob.glob(f"{root}/**/{probe.name}", recursive=True)[0]
    req = {"type": "geopose", "id": "dds-vs-http", "timestamp": 0,
           "sensors": [{"id": "cam0", "type": "camera"}],
           "sensorReadings": {"cameraReadings": [{
               "timestamp": 0, "sensorId": "cam0",
               "privacy": {"dataRetention": ["nostore"],
                           "dataAcceptableUse": ["localization"],
                           "dataSanitizationApplied": ["nosanitization"],
                           "dataSanitizationRequested": ["nosanitization"]},
               "sequenceNumber": 0, "imageFormat": "JPG",
               "size": [cam.width, cam.height],
               "imageBytes": base64.b64encode(Path(path).read_bytes()).decode(),
               "imageOrientation": {"mirrored": False, "rotation": 0.0},
               "params": {"model": "PINHOLE",
                          "modelParams": [fx, fy, cx, cy]}}]}}
    Path("/tmp/dvh.json").write_text(json.dumps(req))
    t0 = time.time()
    code = subprocess.run(
        ["curl", "-s", "-o", "/tmp/dvh-resp.json", "-w", "%{http_code}",
         "-X", "POST", f"{HTTP}/localize/geopose",
         "-H", "Content-Type: application/json",
         "-H", "Accept: application/vnd.oscp+json;version=2.0",
         "--data-binary", "@/tmp/dvh.json"],
        capture_output=True, text=True).stdout
    dt = time.time() - t0
    body = json.loads(Path("/tmp/dvh-resp.json").read_text())
    gp = (body.get("geoposes") or [None])[0]
    if gp is None:
        raise SystemExit(f"HTTP {code}: no geoposes in the response; "
                         f"body starts {json.dumps(body)[:200]}")
    pos = gp["position"] if "position" in gp else gp
    return ({"latitude": float(pos["lat"] if "lat" in pos else pos["latitude"]),
             "longitude": float(pos["lon"] if "lon" in pos else pos["longitude"]),
             "height": float(pos.get("h", pos.get("height", 0.0)))}, dt, code)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dataset")
    ap.add_argument("map_id")
    ap.add_argument("--era", choices=("1.7", "1.8"), default="1.8")
    ap.add_argument("--frames", type=int, default=3)
    ap.add_argument("--domain", type=int, default=0)
    ap.add_argument("--service-id", default="svc:vps:oarc/aws-test")
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()

    import pycolmap
    from cyclonedds.domain import DomainParticipant

    svc, blobmod, tt, types = _import_era(args.era)
    (VpsRequest, VpsResponse, QualityRequirements,
     FrameRef, ScaleStatus, BlobChunk, BlobRef,
     GeoPose, CovMatrix, Time) = types

    def _now():
        t = time.time()
        return Time(sec=int(t), nanosec=int((t % 1) * 1e9))

    def _blank_geopose():
        return GeoPose(lat_deg=0.0, lon_deg=0.0, alt_m=0.0,
                       q=[0.0, 0.0, 0.0, 1.0], stamp=_now(),
                       cov=CovMatrix(none=0))

    root = f"/uploads/{args.dataset}"
    rec = pycolmap.Reconstruction(
        f"{root}/hlocMaps/{args.map_id}/hloc_reconstruction")
    probes = sorted(rec.images.values(), key=lambda i: i.name)[:args.frames]
    print(f"era {args.era} client against {args.service_id} on domain "
          f"{args.domain}; {rec.num_reg_images()} registered images, "
          f"probing {len(probes)}")

    dp = DomainParticipant(args.domain)
    req_w = tt.make_writer(dp, svc.TOPIC_VPS_QUERY, VpsRequest, svc.QOS_VPS_REQ)
    blob_w = tt.make_writer(dp, svc.TOPIC_BLOB_CHUNK, BlobChunk, svc.QOS_BLOB)
    resp_r = tt.make_reader(dp, svc.TOPIC_VPS_RESULT, VpsResponse,
                            svc.QOS_VPS_RESP)
    time.sleep(1.0)

    deltas: list[float] = []
    for probe in probes:
        cam = rec.cameras[probe.camera_id]
        http_gp, http_dt, code = _http_localize(root, probe, cam)

        path = glob.glob(f"{root}/**/{probe.name}", recursive=True)[0]
        image = Path(path).read_bytes()
        qid, blob_id = str(uuid.uuid4()), str(uuid.uuid4())
        ref = blobmod.blob_ref(blob_id, svc.ROLE_QUERY_IMAGE, image)
        request = VpsRequest(
            query_id=qid, service_id=args.service_id,
            client_frame_ref=_client_frame(FrameRef, ScaleStatus),
            has_prior_geopose=False, prior_geopose=_blank_geopose(),
            query_blobs=[BlobRef(blob_id=ref["blob_id"], role=ref["role"],
                                 checksum=ref["checksum"])],
            query_stream_id="", has_quality_requirements=False,
            quality_requirements=QualityRequirements(max_rmse_m=0.0,
                                                     min_confidence=0.0),
            stamp=_now())
        t0 = time.time()
        req_w.write(request)
        for c in blobmod.chunk(blob_id, image, cls=BlobChunk):
            blob_w.write(c)

        reply, deadline = None, time.time() + args.timeout
        while reply is None and time.time() < deadline:
            for r in tt.take_samples(resp_r) or []:
                if r.query_id == qid:
                    reply = r
                    break
            time.sleep(0.05)
        dds_dt = time.time() - t0

        print(f"\n{probe.name}")
        print(f"  HTTP {code} in {http_dt:.2f}s  "
              f"lat {http_gp['latitude']:.9f} lon {http_gp['longitude']:.9f} "
              f"h {http_gp['height']:.3f}")
        if reply is None:
            print(f"  DDS  no reply within {args.timeout:.0f}s — "
                  f"not a comparison, and not a silent one")
            continue
        print(f"  DDS  {reply.status.name if hasattr(reply.status, 'name') else reply.status}"
              f" in {dds_dt:.2f}s")
        if not reply.has_node_geo:
            print("  DDS  carried no node_geo, so there is no geopose to "
                  "compare; status above is the whole answer")
            continue

        ng = reply.node_geo
        if not ng.has_geopose:
            print("  DDS  node_geo carries no geopose — `poses` are in the "
                  "map's own frame and comparing those against a geodetic "
                  "answer is the error localization-eval.py warns about")
            continue
        # `spatial.core.GeoPose` is lat_deg/lon_deg/alt_m. Resolved at the
        # definition rather than through `spatial.argeo`'s re-export, which is
        # an annotated alias reporting zero members — the same trap that made a
        # cross-era member diff print "no change" for a type that had grown.
        gp = ng.geopose
        dds_gp = {"latitude": float(gp.lat_deg),
                  "longitude": float(gp.lon_deg),
                  "height": float(gp.alt_m)}
        print(f"       lat {dds_gp['latitude']:.9f} "
              f"lon {dds_gp['longitude']:.9f} h {dds_gp['height']:.3f}")
        d = _enu_metres(http_gp, dds_gp)
        deltas.append(d)
        print(f"  apart {d*100:.3f} cm — the same point computed twice, so "
              f"anything large is one face applying what the other does not")

        # §2's requirement: what the response's frame says about scale, and
        # what this era's reader can see of it. `NodeGeo.poses` is a sequence
        # of FramedPose, each carrying its own frame_ref.
        poses = list(getattr(ng, "poses", None) or [])
        fr = getattr(poses[0], "frame_ref", None) if poses else None
        if fr is None:
            print("  frame_ref: node_geo carried no poses, so no frame to "
                  "report")
        elif args.era == "1.8":
            print(f"  frame_ref {fr.fqn}: has_scale={fr.has_scale} "
                  f"status={getattr(fr.scale_status, 'name', fr.scale_status)} "
                  f"m/unit={fr.meters_per_unit} unit={fr.display_unit!r}")
        else:
            print(f"  frame_ref {fr.fqn}: four members on 1.7 — the scale "
                  f"quartet the 1.8 service sent is truncated away by "
                  f"appendable evolution, which is the expected behaviour and "
                  f"the reason the cross-era lane works at all")

    if deltas:
        print(f"\n{len(deltas)} compared; max {max(deltas)*100:.3f} cm, "
              f"mean {sum(deltas)/len(deltas)*100:.3f} cm")
    else:
        print("\nnothing compared — see the per-frame lines above for why")
    return 0


if __name__ == "__main__":
    sys.exit(main())
