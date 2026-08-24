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


def _protected_shell_lines(text: str) -> set:
    """Lines of a shell script where a leading `#` is data, not a comment.

    Two cases, both exact rather than heuristic:

    * **Heredoc bodies.** Tracked from the `<<DELIM` opener to the delimiter line.
    * **Multi-line single-quoted strings** — in practice the `python3 -c '...'` blocks.
      A line that opens one contains an odd number of unescaped single quotes, and the
      line that closes it does too, so parity over non-comment lines brackets the region
      exactly. Comment lines are skipped before counting, because an apostrophe in prose
      ("Cyclone's default") would otherwise flip the state.

    Getting this wrong is silent: a heredoc whose body lost a line still parses, so no
    syntax check catches it. `validate()` compares heredoc bodies before and after as a
    backstop.
    """
    protected, in_heredoc, delim, in_quote = set(), False, None, False
    for i, line in enumerate(text.split("\n")):
        if in_heredoc:
            protected.add(i)
            if line.strip() == delim:
                in_heredoc = False
            continue
        if in_quote:
            protected.add(i)
            if _odd_single_quotes(line):
                in_quote = False
            continue
        if line.lstrip().startswith("#"):
            continue                      # a real comment: never opens anything
        m = re.search(r"<<-?\s*[\'\"]?([A-Za-z_][A-Za-z0-9_]*)[\'\"]?", line)
        if m and "<<<" not in line:
            in_heredoc, delim = True, m.group(1)
            continue
        if _odd_single_quotes(line):
            in_quote = True
    return protected


def _odd_single_quotes(line: str) -> bool:
    """True if the line leaves a single-quoted string open (odd count, backslash-aware)."""
    n, esc = 0, False
    for c in line:
        if esc:
            esc = False
        elif c == "\\":
            esc = True
        elif c == "'":
            n += 1
    return n % 2 == 1


def strip_comments(rel: str, raw: bytes) -> bytes:
    """Drop whole-line comments from a shipped payload.

    The template must fit in 51,200 bytes to be usable with `create-stack --template-body`,
    and these files are documentation-heavy on purpose. Stripping the comments out of the
    *shipped copy* buys about 12 KB while leaving the annotated original in the repo as the
    thing people read and edit.

    Whole-line only; the shebang stays and JSON is passed through untouched. A leading `#`
    is unambiguously a comment in YAML, nginx and systemd. In shell it is not — inside a
    heredoc body or a multi-line quoted string it is data, and removing it is corruption
    that still parses — so `_protected_shell_lines` brackets those regions exactly and they
    are left alone. Trailing comments after code are never touched.

    `validate()` is the backstop: for YAML and JSON it compares the parsed structure before
    and after, and for shell it compares heredoc bodies.
    """
    if rel.endswith(".json"):
        return raw
    text = raw.decode()
    protected = _protected_shell_lines(text) if raw.startswith(b"#!") else set()
    out = []
    for i, line in enumerate(text.split("\n")):
        if i == 0 and line.startswith("#!"):
            out.append(line)
            continue
        if i not in protected and line.lstrip().startswith("#"):
            continue
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).encode()


def validate(files: dict, originals: dict) -> None:
    """Re-check every stripped payload against its original.

    For YAML and JSON this is a semantic check, not a syntax one: parse both the original
    and the stripped bytes and require the loaded structures to be equal. A `#` line
    dropped out of a YAML block scalar changes the data and fails here, which a syntax
    check would sail straight past.

    Shell has no equivalent — a heredoc with its body stripped still parses — so the real
    protection there is structural, in `_shell_content_lines`. `bash -n` is kept as a
    backstop against a stripper bug that breaks syntax outright.
    """
    import subprocess, tempfile, os
    import xml.etree.ElementTree as ET

    for dest, spec in sorted(files.items()):
        data = gzip.decompress(base64.b64decode(spec["gz64"]))
        raw = originals[dest]
        try:
            if dest.endswith((".yaml", ".yml")):
                import yaml
                if yaml.safe_load(data) != yaml.safe_load(raw):
                    sys.exit(f"{dest}: comment stripping CHANGED THE DATA, not just comments")
            elif dest.endswith(".json"):
                if json.loads(data) != json.loads(raw):
                    sys.exit(f"{dest}: comment stripping changed the data")
            elif dest.endswith((".xml", ".in")):
                ET.fromstring(data.replace(b"<!--PEERS-->", b""))
            elif data.startswith(b"#!"):
                # Heredoc bodies must survive intact; a stripped one still parses.
                if _heredoc_bodies(raw.decode()) != _heredoc_bodies(data.decode()):
                    sys.exit(f"{dest}: comment stripping altered a heredoc body")
                with tempfile.NamedTemporaryFile(suffix=".sh", delete=False) as fh:
                    fh.write(data)
                    tmp = fh.name
                try:
                    r = subprocess.run(["bash", "-n", tmp], capture_output=True, text=True)
                    if r.returncode:
                        sys.exit(f"{dest}: shell syntax broken after stripping\n{r.stderr}")
                finally:
                    os.unlink(tmp)
        except SystemExit:
            raise
        except Exception as exc:
            sys.exit(f"{dest}: unparseable after comment stripping: {exc}")


def _heredoc_bodies(text: str) -> list:
    """Every heredoc body in a shell script, as a list of line lists."""
    bodies, cur, delim = [], None, None
    for line in text.split("\n"):
        if cur is not None:
            if line.strip() == delim:
                bodies.append(cur)
                cur, delim = None, None
            else:
                cur.append(line)
            continue
        m = re.search(r"<<-?\s*[\'\"]?([A-Za-z_][A-Za-z0-9_]*)[\'\"]?", line)
        if m and not line.lstrip().startswith("#"):
            cur, delim = [], m.group(1)
    return bodies


def build(validate_payloads: bool = True) -> str:
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
    files, originals = {}, {}
    for rel, (dest, mode) in FILES.items():
        p = HERE / rel
        if not p.exists():
            sys.exit(f"missing source file: {p}")
        original = p.read_bytes()
        originals[dest] = original
        raw = strip_comments(rel, original)
        files[dest] = {
            "mode": mode,
            "gz64": base64.b64encode(gzip.compress(raw, mtime=0)).decode(),
        }

    if validate_payloads:
        validate(files, originals)

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
