#!/usr/bin/env python3
"""Bounded original HMI startup, with original JVM/JNI/libc and no vehicle access."""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "runtime/joynext"


def docker(*args, **kwargs):
    return subprocess.run(["docker", *map(str, args)], check=True, **kwargs)


def check_isolation(info, network="none"):
    host = info["HostConfig"]
    assert info["Config"]["User"] == "1003:1003"
    assert host["NetworkMode"] == network and host["ReadonlyRootfs"]
    assert not host["Privileged"] and host["CapDrop"] == ["ALL"] and not host["CapAdd"]
    assert not host["Devices"] and not host["PortBindings"]
    assert "no-new-privileges" in host["SecurityOpt"] and "seccomp=builtin" in host["SecurityOpt"]
    assert all(not m["RW"] or m["Destination"] == "/out" for m in info["Mounts"] if m["Type"] == "bind")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=30, choices=range(5, 301))
    parser.add_argument("--stock", action="store_true", help="Use original priorities and shared class archive")
    parser.add_argument("--negative-control", action="store_true", help="Accept IPC but withhold NameServer replies")
    parser.add_argument("--nameserver-only", action="store_true", help="DSI negative control: no discovery or service replies")
    parser.add_argument("--graphics-proxy", action="store_true", help="Forward EGL/GLES to isolated Mesa software rendering")
    parser.add_argument("--rsi-mock", action="store_true", help="Serve a synthetic offline user profile in the isolated network namespace")
    parser.add_argument("--viewer", action="store_true", help="Open the Russian JOYNEXT viewer on this session")
    args = parser.parse_args()
    if args.negative_control and args.nameserver_only:
        parser.error("Choose one negative control")
    if args.stock and (args.graphics_proxy or args.rsi_mock):
        parser.error("Mocks require the lab profile")
    viewer = STAGE / "viewer/JOYNEXT Emulator Lab.app"
    if args.viewer and (not args.graphics_proxy or not viewer.is_dir()):
        parser.error("--viewer requires --graphics-proxy; build with python3 scripts/build_joynext_viewer.py")
    lock = json.loads((STAGE / "image-lock.json").read_text())
    image = lock["firmware"]
    output = ROOT / "reports" / datetime.datetime.now(datetime.timezone.utc).strftime("joynext-hmi-%Y%m%dT%H%M%S%fZ")
    output.mkdir(parents=True)
    if args.viewer:
        viewer_lock = json.loads((STAGE / "viewer/viewer-lock.json").read_text())
        assert hashlib.sha256((viewer / "Contents/MacOS/joynext-viewer").read_bytes()).hexdigest() == viewer_lock["executable_sha256"]
        (output / "viewer-lock.json").write_text(json.dumps(viewer_lock, indent=2) + "\n")
    # A shell can reread its script after sleeping; source edits must not alter active sessions.
    harness, bench = output / "harness", output / "build"
    harness.mkdir()
    bench.mkdir()
    for file in ("probe.sh", "nice.conf", "mock_rsi.py", "mock_commgr.c"):
        shutil.copy2(ROOT / "joynext" / file, harness / file)
    shutil.copy2(Path(__file__), harness / "run_joynext.py")
    if not args.stock:
        shutil.copy2(STAGE / "build/mock_commgr", bench / "mock_commgr")
        assert hashlib.sha256((bench / "mock_commgr").read_bytes()).hexdigest() == lock["mock_sha256"]
    if args.graphics_proxy:
        graphics = json.loads((STAGE / "graphics-lock.json").read_text())
        (bench / "glfwd").mkdir()
        for file, sha in graphics["files"].items():
            shutil.copy2(STAGE / "build/glfwd" / file, bench / "glfwd" / file)
            assert hashlib.sha256((bench / "glfwd" / file).read_bytes()).hexdigest() == sha
        for alias, target in [("libGLESv2.so.2", "libGLESv2_adreno.so"), ("libEGL.so.1", "libEGL_adreno.so")]:
            (bench / "glfwd" / alias).symlink_to(target)
        (output / "graphics-lock.json").write_text(json.dumps(graphics, indent=2) + "\n")
        (output / "frames").mkdir()
    name = "joynext-hmi-" + uuid.uuid4().hex[:10]
    compositor = name + "-weston"
    rsi = name + "-rsi"
    volume = name + "-display"
    command = ["create", "--name", name, "--pull", "never", "--platform", "linux/arm64",
               "--network", "none", "--read-only", "--user", "1003:1003", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", "--security-opt", "seccomp=builtin",
               "--memory", "2g", "--cpus", "3", "--cpuset-cpus", "0-2", "--pids-limit", "256", "--shm-size", "64m"]
    for directory, size in [("tmp", "128m"), ("run", "64m"), ("var", "128m"), ("data", "32m")]:
        command += ["--tmpfs", f"/{directory}:rw,nosuid,nodev,size={size},mode=1777"]
    if not args.stock:
        command += ["-v", f"{volume}:/run/display", "-v", f"{bench}:/bench:ro"]
    if args.graphics_proxy:
        command += ["-e", "JOYNEXT_GL_FORWARD=1"]
    if args.rsi_mock:
        command += ["-e", "JOYNEXT_RSI=1"]
    command += ["-v", f"{harness}:/lab:ro", "-v", f"{output}:/out:rw",
                image, "/bin/sh", "/lab/probe.sh", str(args.seconds), "stock" if args.stock else "lab",
                "--observe" if args.negative_control else "--nameserver-only" if args.nameserver_only else "--serve"]
    (output / "command.json").write_text(json.dumps(["docker", *command], indent=2) + "\n")
    snapshots = {"scripts/run_joynext.py": harness / "run_joynext.py"}
    snapshots.update({"joynext/" + name: harness / name for name in ("probe.sh", "nice.conf", "mock_commgr.c", "mock_rsi.py")})
    if not args.stock:
        snapshots["runtime/joynext/build/mock_commgr"] = bench / "mock_commgr"
    (output / "harness-sha256.json").write_text(json.dumps({name: hashlib.sha256(p.read_bytes()).hexdigest()
                                                        for name, p in snapshots.items()}, indent=2) + "\n")
    try:
        if not args.stock:
            docker("volume", "create", "--driver", "local", "--opt", "type=tmpfs", "--opt", "device=tmpfs",
                   "--opt", "o=size=64m,mode=0700,uid=1003,gid=1003", volume, capture_output=True)
            docker("run", "-d", "--name", compositor, "--pull", "never", "--network", "none", "--read-only",
                   "--user", "1003:1003", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                   "--security-opt", "seccomp=builtin", "--memory", "1g" if args.graphics_proxy else "256m", "--pids-limit", "64", "--cpus", "3" if args.graphics_proxy else "1",
                   "--tmpfs", "/tmp:rw,nosuid,nodev,size=32m", "-v", f"{volume}:/run/display",
                   "-v", f"{bench}:/bench:ro",
                   "-v", f"{output}:/out:rw", "-e", "XDG_RUNTIME_DIR=/run/display", lock["tools"],
                   "weston", "--backend=headless-backend.so", "--use-pixman", "--shell=ivi-shell.so",
                   "--width=1280", "--height=720", "--idle-time=0", "--socket=wayland-0", "--no-config", "--log=/out/weston.log",
                   capture_output=True)
            for attempt in range(50):
                ready = subprocess.run(["docker", "exec", compositor, "test", "-S", "/run/display/wayland-0"], capture_output=True)
                if ready.returncode == 0:
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("Weston did not create its socket; see weston.log")
            info = json.loads(docker("inspect", compositor, capture_output=True, text=True).stdout)[0]
            check_isolation(info)
            (output / "weston-isolation.json").write_text(json.dumps(info, indent=2) + "\n")
            if args.graphics_proxy:
                docker("exec", "-d", "-e", "LIBGL_ALWAYS_SOFTWARE=1", "-e", "LP_NUM_THREADS=3", "-e", "GLHOST_KEEP=0",
                       compositor, "sh", "-c", "/bench/glfwd/glhost /run/display/moi3-gl.sock 1280 720 /out/frames > /out/glhost.log 2>&1")
                for attempt in range(50):
                    ready = subprocess.run(["docker", "exec", compositor, "test", "-S", "/run/display/moi3-gl.sock"], capture_output=True)
                    if ready.returncode == 0:
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError("GL forwarding socket missing; see glhost.log")
                if args.viewer:
                    subprocess.run(["open", "-n", str(viewer), "--args", "--container", compositor,
                                    "--ponte", "/bench/glfwd/ponte", "--soquete", "/tmp/moi3-quadros.sock",
                                    "--entrada", "/run/display/dsi-input", "--pasta", str(ROOT / "joynext")], check=True)
        docker(*command, capture_output=True)
        info = json.loads(docker("inspect", name, capture_output=True, text=True).stdout)[0]
        check_isolation(info)
        (output / "isolation.json").write_text(json.dumps(info, indent=2) + "\n")
        with (output / "supervisor.log").open("w") as log:
            if args.rsi_mock:
                docker("start", name, capture_output=True)
                # This namespace has only loopback: its owner is the checked network=none HMI container.
                network = "container:" + info["Id"]
                docker("run", "-d", "--name", rsi, "--pull", "never", "--network", network, "--read-only",
                       "--user", "1003:1003", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                       "--security-opt", "seccomp=builtin", "--memory", "128m", "--pids-limit", "64", "--cpus", "1",
                       "--tmpfs", "/tmp:rw,nosuid,nodev,size=16m", "-v", f"{volume}:/run/display",
                       "-v", f"{harness}:/lab:ro", "-v", f"{output}:/out:rw", lock["tools"],
                       "sh", "-c", "python3 -B -u /lab/mock_rsi.py > /out/rsi.jsonl 2>&1", capture_output=True)
                rsi_info = json.loads(docker("inspect", rsi, capture_output=True, text=True).stdout)[0]
                check_isolation(rsi_info, network)
                (output / "rsi-isolation.json").write_text(json.dumps(rsi_info, indent=2) + "\n")
                result = docker("wait", name, capture_output=True, text=True, timeout=args.seconds + 30)
                docker("logs", name, stdout=log, stderr=subprocess.STDOUT)
                assert result.stdout.strip() == "0", result.stdout
            else:
                docker("start", "-a", name, stdout=log, stderr=subprocess.STDOUT, timeout=args.seconds + 30)
        (STAGE / "last-session.txt").write_text(str(output) + "\n")
        outcome = (output / "outcome.txt").read_text().strip()
        print(f"Original JOYNEXT HMI probe: {outcome}\nEvidence: {output}")
    finally:
        if args.rsi_mock:
            subprocess.run(["docker", "rm", "-f", rsi], capture_output=True, timeout=15)
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15)
        if not args.stock:
            subprocess.run(["docker", "rm", "-f", compositor], capture_output=True, timeout=15)
            subprocess.run(["docker", "volume", "rm", volume], capture_output=True, timeout=15)


if __name__ == "__main__":
    main()
