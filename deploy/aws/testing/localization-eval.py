#!/usr/bin/env python3
"""Compare the returned local-frame pose against the reconstruction's own camera centre.

The comparison applies NO transform: `poses` come back in the map's own reconstruction
frame, with neither the metric scale from transform.json nor the graphics->ENU rotation.
Applying either produces a multi-metre error that is purely an artefact of the comparison —
the returned values come out exactly 1/scale times the expected ones. That mistake was made
once already; do not reintroduce it. See testing/README.md.

Comparing the geodetic `geopose` field instead is a different measurement and needs the full
chain the server applies (graphics->ENU rotation, then the matrix, then geodetic conversion
about the anchor). Do not mix the two numbers."""
import json, base64, subprocess, sys, glob, time
import numpy as np, pycolmap

DS, MAP = sys.argv[1], sys.argv[2]
L = "http://127.0.0.1:8000"; ROOT = f"/uploads/{DS}"
REC = f"{ROOT}/hlocMaps/{MAP}/hloc_reconstruction"
XF = json.load(open(f"{ROOT}/hlocMaps/{MAP}/transform.json"))
M = np.array(XF["matrix"], dtype=float)
G2R = np.array([[1,0,0,0],[0,0,-1,0],[0,1,0,0],[0,0,0,1]], dtype=float)

rec = pycolmap.Reconstruction(REC)
imgs = sorted(rec.images.values(), key=lambda i: i.name)
print(f"reconstruction: {rec.num_reg_images()} images, {rec.num_points3D()} points")
print(f"transform: geodetic anchor = {XF.get('latitude')}, scale = {M[0][0]:.6f}\n")

def centre(img):
    """Camera centre in the reconstruction frame. pycolmap moved this accessor between
    versions, so try the ones known to exist and fail loudly if none do — returning None
    would surface a pycolmap API change as a confusing TypeError further down."""
    tried = ("projection_center()", "cam_from_world().inverse().translation",
             "cam_from_world.inverse().translation", "center()")
    for a in tried:
        try: return np.array(eval("img." + a), dtype=float)
        except Exception: continue
    raise RuntimeError(
        f"no working pycolmap accessor for the camera centre of {img.name!r}; "
        f"tried {tried}. pycolmap version: {getattr(pycolmap, '__version__', 'unknown')}")

errs, lats = [], []
# Named, not index-derived: picking by position in a sorted list silently compares
# different frames if registration order changes. Override with argv[3:] to use others.
WANT = sys.argv[3:] or ["frame_0633.jpg", "frame_1067.jpg", "frame_1680.jpg"]
byname = {i.name: i for i in imgs}
probes = [byname[n] for n in WANT if n in byname]
missing = [n for n in WANT if n not in byname]
if missing:
    print(f"  not registered in this reconstruction, skipping: {missing}")
if not probes:
    print("  none of the requested frames are in the map; falling back to quartile positions")
    probes = [imgs[len(imgs)//4], imgs[len(imgs)//2], imgs[3*len(imgs)//4]]
for probe in probes:
    cam = rec.cameras[probe.camera_id]; p = cam.params
    if cam.model.name in ("SIMPLE_PINHOLE","SIMPLE_RADIAL"):
        fx=fy=float(p[0]); cx,cy=float(p[1]),float(p[2])
    else: fx,fy,cx,cy = (float(p[i]) for i in range(4))
    path = glob.glob(f"{ROOT}/**/{probe.name}", recursive=True)[0]
    req = {"type":"geopose","id":"eval","timestamp":0,
      "sensors":[{"id":"cam0","type":"camera"}],
      "sensorReadings":{"cameraReadings":[{"timestamp":0,"sensorId":"cam0",
        "privacy":{"dataRetention":["nostore"],"dataAcceptableUse":["localization"],
                   "dataSanitizationApplied":["nosanitization"],"dataSanitizationRequested":["nosanitization"]},
        "sequenceNumber":0,"imageFormat":"JPG","size":[cam.width,cam.height],
        "imageBytes":base64.b64encode(open(path,"rb").read()).decode(),
        "imageOrientation":{"mirrored":False,"rotation":0.0},
        "params":{"model":"PINHOLE","modelParams":[fx,fy,cx,cy]}}]}}
    open("/tmp/r.json","w").write(json.dumps(req))
    t0=time.time()
    code = subprocess.run(["curl","-s","-o","/tmp/resp.json","-w","%{http_code}","-X","POST",
        f"{L}/localize/geopose","-H","Content-Type: application/json",
        "-H","Accept: application/vnd.oscp+json;version=2.0","--data-binary","@/tmp/r.json"],
        capture_output=True,text=True).stdout
    dt=time.time()-t0; lats.append(dt)
    r = json.load(open("/tmp/resp.json"))
    t = r["poses"][0]["pose"]["t"]
    got = np.array([t["x"], t["y"], t["z"]], dtype=float)
    gt_raw = centre(probe)
    # The server returns `poses` in the map's own (reconstruction) frame: no metric scale,
    # no graphics->ENU rotation. Applying either, as an earlier version of this script did,
    # produces an apparent multi-metre error that is entirely an artefact of the comparison.
    exp = gt_raw
    err = float(np.linalg.norm(got - exp)); errs.append(err)
    print(f"{probe.name}  HTTP {code}  {dt:.2f}s")
    print(f"   reconstruction xyz : [{exp[0]:+.5f} {exp[1]:+.5f} {exp[2]:+.5f}]")
    print(f"   returned       xyz : [{got[0]:+.5f} {got[1]:+.5f} {got[2]:+.5f}]")
    print(f"   error              : {err*1000:.2f} mm  ({err*M[0][0]*100:.2f} cm metric, scale {M[0][0]:.4f})\n")
mean=sum(errs)/len(errs)
print(f"localized {len(errs)}/3   mean error {mean*1000:.2f} mm in map units "
      f"= {mean*M[0][0]*100:.2f} cm metric   mean latency {sum(lats)/len(lats):.2f}s")
r = json.load(open("/tmp/resp.json"))
print("\ngeoposes array from the last response:")
print(json.dumps(r.get("geoposes"), indent=1)[:400])
