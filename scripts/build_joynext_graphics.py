#!/usr/bin/env python3
"""Build upstream GL forwarding against JOYNEXT libc, never against LG libc."""
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
from run_joynext import ROOT, STAGE


def main():
    source = ROOT / "references/lipedesign-mib3-moi3-altscreen-455658b/glforward-moi3"
    manifest = json.loads((STAGE / "manifest.json").read_text())
    lock = json.loads((STAGE / "image-lock.json").read_text())
    build = STAGE / "build"
    firmware = build / "fwlibs"
    firmware.mkdir(exist_ok=True)
    with tarfile.open(STAGE / "rootfs.tar") as archive:
        for path, name in [("lib/libc-2.25.so", "libc.so.6"),
                           ("lib/libpthread-2.25.so", "libpthread.so.0"),
                           ("lib/ld-2.25.so", "ld-linux-aarch64.so.1")]:
            data = archive.extractfile(path).read()
            assert hashlib.sha256(data).hexdigest() == manifest["files"][path]["sha256"]
            (firmware / name).write_bytes(data)
    subprocess.run(["docker", "run", "--rm", "--network", "none", "--read-only", "--user", "1000:1000",
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--security-opt", "seccomp=builtin",
                    "--tmpfs", "/tmp:size=64m", "-v", f"{source}:/src:ro", "-v", f"{build}:/out:rw",
                    "-e", "FWLIBS=/out/fwlibs", lock["tools"], "sh", "/src/build.sh", "/out/glfwd"], check=True)
    output = build / "glfwd"
    for name, target in [("libGLESv2.so.2", "libGLESv2_adreno.so"), ("libEGL.so.1", "libEGL_adreno.so")]:
        link = output / name
        if not link.exists():
            link.symlink_to(target)
    record = {"source": str(source.relative_to(ROOT)), "tools": lock["tools"],
              "libc": "JOYNEXT 2.25", "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in output.iterdir() if p.is_file() and not p.is_symlink()}}
    (STAGE / "graphics-lock.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
