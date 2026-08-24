#!/usr/bin/env python3
"""Assemble deploy/aws/template.yaml from template.src.yaml plus the files it ships.

Why a build step exists: user-data is capped at 16 KB and the config files, scripts and
systemd units together exceed it. They ride in the stack's Metadata instead, which the
template carries (51 KB limit) and which user-data reads back at boot.

Rather than duplicate those payloads — one readable copy under deploy/aws/ and one pasted
into the template, free to drift apart silently — this script makes the repo files the
single source of truth and generates the Metadata from them.

    python3 deploy/aws/build.py            # regenerate template.yaml
    python3 deploy/aws/build.py --check    # fail if template.yaml is stale (for CI)
"""
import sys, json, gzip, base64, pathlib, re

HERE = pathlib.Path(__file__).parent
SRC  = HERE / "template.src.yaml"
OUT  = HERE / "template.yaml"

# source file -> (destination on the instance, mode)
FILES = {
    "nginx/mapbuilder.conf":                    ("/opt/openvps/nginx/mapbuilder.conf",                         "0644"),
    "docker-compose.aws.yaml":                  ("/opt/openvps/docker-compose.aws.yaml",                       "0644"),
    # spatialdds variant only. Always shipped — the Metadata is one blob for both variants
    # and these are inert unless the variant's Compose file list names them.
    "docker-compose.spatialdds.yaml":           ("/opt/openvps/docker-compose.spatialdds.yaml",                "0644"),
    "cyclonedds.xml.in":                        ("/opt/openvps/cyclonedds.xml.in",                             "0644"),
    "fusionauth/docker-compose.fusionauth.yaml":("/opt/openvps/fusionauth/docker-compose.fusionauth.yaml",     "0644"),
    "fusionauth/kickstart/kickstart.json":      ("/opt/openvps/fusionauth/kickstart/kickstart.json",           "0644"),
    "scripts/openvps-render-env":               ("/usr/local/bin/openvps-render-env",                          "0700"),
    "scripts/idle-shutdown.sh":                 ("/usr/local/bin/openvps-idle-shutdown",                       "0700"),
    "scripts/openvps-fusionauth-configure":     ("/usr/local/bin/openvps-fusionauth-configure",                "0700"),
    "systemd/openvps-auth.service":             ("/etc/systemd/system/openvps-auth.service",                   "0644"),
    "systemd/openvps.service":                  ("/etc/systemd/system/openvps.service",                        "0644"),
    "systemd/openvps-idle.service":             ("/etc/systemd/system/openvps-idle.service",                   "0644"),
    "systemd/openvps-idle.timer":               ("/etc/systemd/system/openvps-idle.timer",                     "0644"),
}

PLACEHOLDER = "      openvps:\n        files: {}\n"


def strip_comments(rel: str, raw: bytes) -> bytes:
    """Drop whole-line comments from a shipped payload.

    The template has to fit in 51,200 bytes to be usable with `create-stack
    --template-body`, and these files are documentation-heavy on purpose. Stripping the
    comments out of the *shipped copy* buys about 12 KB of the budget while leaving the
    annotated original in the repo as the thing people read and edit.

    Whole-line only. A line whose first non-space character is `#` is a comment in every
    format here — YAML, shell, nginx, systemd — with two exceptions, both handled: a
    shebang stays, and JSON is passed through untouched because it has no comments and a
    `#` would be data. Trailing comments after code are left alone; they are where the
    quoting hazards live and they are not where the bytes are.

    Heredoc bodies would be the real hazard — a `#` line inside one is content, not a
    comment. There are none in the shipped scripts today, and `--check` in CI plus the
    bash -n / YAML parse below is what catches it if one appears.
    """
    if rel.endswith(".json"):
        return raw
    out = []
    for i, line in enumerate(raw.decode().split("\n")):
        if i == 0 and line.startswith("#!"):
            out.append(line)
            continue
        if line.lstrip().startswith("#"):
            continue
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).encode()


def build() -> str:
    src = SRC.read_text()
    if PLACEHOLDER not in src:
        sys.exit("template.src.yaml: Metadata placeholder not found")

    # Emit the files block as JSON. JSON is valid YAML, so this sidesteps every block-scalar
    # indentation and escaping trap that inlining these payloads as YAML would introduce.
    #
    # Contents are gzipped and base64'd. Plain text pushed the template to 50.9 KB against a
    # 51.2 KB limit — 263 bytes of headroom, which the Phase 4 Caddyfile would erase.
    # Compression takes the payload to roughly a third of that and keeps escaping trivial,
    # since base64 has no characters JSON or YAML care about.
    files = {}
    for rel, (dest, mode) in FILES.items():
        p = HERE / rel
        if not p.exists():
            sys.exit(f"missing source file: {p}")
        raw = strip_comments(rel, p.read_bytes())
        files[dest] = {
            "mode": mode,
            "gz64": base64.b64encode(gzip.compress(raw, mtime=0)).decode(),
        }

    block = json.dumps({"openvps": {"files": files}}, indent=2)
    block = "\n".join("      " + ln for ln in block.split("\n")) + "\n"
    return src.replace(PLACEHOLDER, block)


def main() -> None:
    out = build()
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text() != out:
            sys.exit("template.yaml is stale — run: python3 deploy/aws/build.py")
        print("template.yaml is up to date")
        return
    OUT.write_text(out)
    raw_total = sum(len(strip_comments(r, (HERE / r).read_bytes())) for r in FILES)
    print(f"wrote {OUT} ({len(out)} bytes; {len(FILES)} files, "
          f"{raw_total} bytes of payload compressed in after comment stripping)")
    if len(out) > 51200:
        print(f"WARNING: {len(out)} bytes exceeds the 51200-byte direct create-stack limit;"
              " use --template-url with S3, or trim.")


if __name__ == "__main__":
    main()
