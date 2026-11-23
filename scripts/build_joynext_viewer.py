#!/usr/bin/env python3
"""Build the Russian JOYNEXT viewer from the pinned MIT upstream source and our patch."""
import hashlib
import json
from pathlib import Path
import plistlib
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "runtime/joynext/viewer"
APP = BUILD / "JOYNEXT Emulator Lab.app"
SOURCE = ROOT / "references/lipedesign-mib3-moi3-altscreen-455658b/janela/janela.swift"
PATCH = ROOT / "joynext/viewer-ru.patch"


def main():
    source_sha = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    assert source_sha == "b807d22fb7c0b87e8ce1161bb97ad6501fead41f27cdac55b121bf516ffe4a43", "Upstream viewer changed"
    BUILD.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE, BUILD / "janela.swift")
    subprocess.run(["/usr/bin/patch", "-s", "-p1", "-i", str(PATCH)], cwd=BUILD, check=True)
    executable = APP / "Contents/MacOS/joynext-viewer"
    executable.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["/usr/bin/swiftc", "-O", "-module-cache-path", str(BUILD / "module-cache"),
                    "-o", str(executable.with_suffix(".new")), str(BUILD / "janela.swift")], check=True)
    executable.with_suffix(".new").replace(executable)
    metadata = {"CFBundleName": "JOYNEXT Emulator Lab", "CFBundleDisplayName": "JOYNEXT Emulator Lab",
                "CFBundleIdentifier": "local.joynext.emulator-lab", "CFBundleExecutable": "joynext-viewer",
                "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "1.0", "CFBundleVersion": "1",
                "CFBundleDevelopmentRegion": "ru", "CFBundleLocalizations": ["ru"],
                "LSMinimumSystemVersion": "13.0", "NSHighResolutionCapable": True,
                "NSPrincipalClass": "NSApplication"}
    (APP / "Contents/Info.plist").write_bytes(plistlib.dumps(metadata))
    shutil.copy2(SOURCE.parents[1] / "LICENSE", APP / "Contents/LICENSE")
    record = {"upstream": str(SOURCE.relative_to(ROOT)), "upstream_sha256": source_sha,
              "patch_sha256": hashlib.sha256(PATCH.read_bytes()).hexdigest(),
              "source_sha256": hashlib.sha256((BUILD / "janela.swift").read_bytes()).hexdigest(),
              "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
              "swift": subprocess.run(["/usr/bin/swiftc", "--version"], capture_output=True, text=True, check=True).stdout.strip()}
    (BUILD / "viewer-lock.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print(APP)


if __name__ == "__main__":
    main()
