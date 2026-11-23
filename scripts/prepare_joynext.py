#!/usr/bin/env python3
"""Assemble original P33400P packages into a private rootfs tar, never on the host."""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT.parent / "mib3carplaytest"
ARCHIVE_NAME = "MOI3_SEQREL_EU_VWMQB_P33409P_3G8919360EP"
ARCHIVE = Path.home() / "Downloads" / (ARCHIVE_NAME + ".zip")
INVENTORY = ROOT / "joynext/p33400p-packages.json"
STAGE = ROOT / "runtime/joynext"
TRAIN = "MOI3_EU_VWMQB_P33400P"
SYSTEM = set("""common config-eu root-mib3 dataformats ext-busybox ext-boost ext-poco
ext-systemd ext-libcap ext-libpam ext-expat ext-libbsd ext-pcre ext-xz ext-libdrm ext-libevdev
ext-libinput ext-libxkbcommon ext-mtdev ext-wayland ivi-shell bsp_ext-gbm
bsp_ext-mmngr-mmngr bsp_ext-mmngr-mmngrbuf bsp_ext-opengl bsp_ext-wayland-egl
bsp_ext-wayland-kms bsp_ext-weston bsp_ext-vspm bsp_ext-omx system-displaymanager
system-displaymanager-mib3 ext-strace waitfor ext-optee_client ext-wslay""".split())


def selected(name):
    return (name == "emmc.images.rootfs"
            or name.startswith(("emmc.libs.", "emmc.basic-svcs.", "emmc.communication."))
            or name.removeprefix("emmc.system.") in SYSTEM
            or name.startswith("emmc.hmi.") and not any(x in name for x in ("low", "pcc-anim")))


def guest_path(name):
    path = PurePosixPath(name)
    if ".." in path.parts:
        raise ValueError(f"Path traversal: {name}")
    result = str(path).lstrip("/")
    if result in ("", "."):
        raise ValueError(f"Empty guest path: {name}")
    return result


def add_package(src, dst, module, files):
    for entry in src:
        name = guest_path(entry.name)
        if name.startswith("+"):
            continue  # Package manager metadata belongs in its own package, not guest /.
        if not (entry.isfile() or entry.isdir() or entry.issym() or entry.islnk()):
            raise ValueError(f"Unsupported tar entry: {name}")
        record = {"module": module, "type": entry.type.decode(), "mode": entry.mode}
        data = None
        if entry.isfile():
            with src.extractfile(entry) as member:
                data = member.read()
            assert len(data) == entry.size
            record.update(size=entry.size, sha256=hashlib.sha256(data).hexdigest())
        elif entry.issym() or entry.islnk():
            record["link"] = entry.linkname
            if entry.islnk():
                entry.linkname = guest_path(entry.linkname)
        if name in files:
            previous = {k: v for k, v in files[name].items() if k != "module"}
            current = {k: v for k, v in record.items() if k != "module"}
            if not entry.isdir() and previous != current:
                raise ValueError(f"Conflicting packages at {name}")
            continue
        # No package may put content through a link; symlinks stay in the guest tar.
        for parent in PurePosixPath(name).parents:
            if files.get(str(parent), {}).get("type") in ("1", "2"):
                raise ValueError(f"Link ancestor of {name}")
        if entry.issym() or entry.islnk():
            if any(p.startswith(name + "/") for p in files):
                raise ValueError(f"Link replaces directory: {name}")
        entry.name = name
        entry.uid = entry.gid = 0
        entry.uname = entry.gname = "root"
        entry.pax_headers = {}
        dst.addfile(entry, io.BytesIO(data) if data is not None else None)
        files[name] = record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=ARCHIVE,
                        help="Path to the original P33409P ZIP containing the P33400P train")
    archive_path = parser.parse_args().archive.expanduser().resolve(strict=True)
    modules = json.loads(INVENTORY.read_text())
    assert modules and all(selected(m["module"]) and m["manifest"].startswith("Meta/" + TRAIN + "/")
                           for m in modules), "Unexpected firmware inventory"

    STAGE.mkdir(parents=True, exist_ok=True)
    target = STAGE / "rootfs.tar"
    assert not target.exists() and not (STAGE / "manifest.json").exists(), "Staging already exists"
    files = {}
    partial = STAGE / "rootfs.tar.partial"
    try:
        with zipfile.ZipFile(archive_path) as archive, tarfile.open(partial, "w") as output:
            prefix = ARCHIVE_NAME + "/Data/"
            for module in sorted(modules, key=lambda m: (m["module"] != "emmc.images.rootfs", m["module"])):
                cached = SOURCE / "mib3tools/analysis/payloads" / module["source"]
                payload = cached.open("rb") if cached.is_file() else archive.open(prefix + module["source"])
                with payload, tempfile.TemporaryFile(dir=STAGE) as spool:
                    sha = hashlib.sha256()
                    while chunk := payload.read(1024 * 1024):
                        sha.update(chunk)
                        spool.write(chunk)
                    if sha.hexdigest() != module["sha256"]:
                        raise ValueError(f"Firmware payload SHA-256 mismatch: {module['module']}")
                    spool.seek(0)
                    with tarfile.open(fileobj=spool, mode="r:gz") as package:
                        add_package(package, output, module["module"], files)
                print(module["module"], flush=True)
        partial.rename(target)
        with target.open("rb") as src:
            sha = hashlib.sha256()
            while chunk := src.read(1024 * 1024):
                sha.update(chunk)
        manifest = {"train": TRAIN, "variant": "high (lab assumption; H22 fit not established)",
                    "archive": str(archive_path), "inventory": str(INVENTORY),
                    "rootfs_sha256": sha.hexdigest(), "modules": modules, "files": files}
        (STAGE / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        print(f"Verified {len(modules)} packages, {len(files)} paths; {target}")
    finally:
        partial.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
