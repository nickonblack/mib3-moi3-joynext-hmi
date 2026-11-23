#!/usr/bin/env python3
"""Import the verified, private P33400P tar. Does not execute firmware or installers."""
import hashlib
import json
import subprocess
from prepare_joynext import ROOT, STAGE


def main():
    lock = STAGE / "image-lock.json"
    manifest = json.loads((STAGE / "manifest.json").read_text())
    with (STAGE / "rootfs.tar").open("rb") as src:
        digest = hashlib.sha256()
        while chunk := src.read(1024 * 1024):
            digest.update(chunk)
        assert digest.hexdigest() == manifest["rootfs_sha256"]
        if lock.exists():
            state = json.loads(lock.read_text())
            assert state["rootfs_sha256"] == digest.hexdigest(), "Firmware image lock mismatch"
        else:
            src.seek(0)
            image = subprocess.run(["docker", "import", "--platform", "linux/arm64", "-",
                                    "joynext-firmware:p33400p-high"], stdin=src,
                                   capture_output=True, text=True, check=True).stdout.strip()
            state = {"firmware": image, "rootfs_sha256": digest.hexdigest()}
            lock.write_text(json.dumps(state, indent=2) + "\n")
    base_id = json.loads(subprocess.run(["docker", "image", "inspect", "joynext-lg-tools:mu0883"],
                                       capture_output=True, text=True, check=True).stdout)[0]["Id"]
    assert state.setdefault("tools_base", base_id) == base_id, "Tools base changed"
    subprocess.run(["docker", "build", "--platform", "linux/arm64", "--pull=false", "-t", "joynext-tools:p33400p",
                    "-f", str(ROOT / "joynext/Dockerfile.tools"), str(ROOT / "joynext")], check=True)
    state["tools"] = json.loads(subprocess.run(["docker", "image", "inspect", "joynext-tools:p33400p"],
                                              capture_output=True, text=True, check=True).stdout)[0]["Id"]
    (STAGE / "build").mkdir(exist_ok=True)
    subprocess.run(["docker", "run", "--rm", "--network", "none", "--read-only", "--user", "1000:1000",
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--security-opt", "seccomp=builtin",
                    "-v", f"{STAGE / 'build'}:/out:rw", state["tools"], "cp", "/mock_commgr", "/out/mock_commgr.new"], check=True)
    # Existing probes may still execute the old inode.
    (STAGE / "build/mock_commgr.new").replace(STAGE / "build/mock_commgr")
    state["mock_sha256"] = hashlib.sha256((STAGE / "build/mock_commgr").read_bytes()).hexdigest()
    lock.write_text(json.dumps(state, indent=2) + "\n")
    print(json.dumps(state))


if __name__ == "__main__":
    main()
