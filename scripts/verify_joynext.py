#!/usr/bin/env python3
"""Verify original HMI mappings, isolated IPC and optional rendering evidence."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
from run_joynext import ROOT, STAGE, check_isolation


def verify(output, negative=False, names_only=False):
    manifest = json.loads((STAGE / "manifest.json").read_text())
    if (output / "harness").is_dir():
        for source, sha in json.loads((output / "harness-sha256.json").read_text()).items():
            snapshot = output / ("build/mock_commgr" if source.endswith("/mock_commgr") else "harness/" + Path(source).name)
            assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == sha, source
    for name in ["isolation.json", "weston-isolation.json"]:
        check_isolation(json.loads((output / name).read_text()))
    if (output / "rsi-isolation.json").exists():
        owner = json.loads((output / "isolation.json").read_text())["Id"]
        check_isolation(json.loads((output / "rsi-isolation.json").read_text()), "container:" + owner)
    status = dict(line.split(":", 1) for line in (output / "java-status.txt").read_text().splitlines())
    assert status["Uid"].split() == ["1003"] * 4
    assert int(status["CapEff"].strip(), 16) == 0
    assert status["NoNewPrivs"].strip() == "1" and status["Seccomp"].strip() == "2"
    assert status["Cpus_allowed_list"].strip() == "0-2"
    assert (output / "java-alive-at-stop.txt").read_text().strip() == "1"
    mapped = set()
    for line in (output / "java-maps.txt").read_text().splitlines():
        parts = line.split()
        if "x" in parts[1] and len(parts) >= 6 and parts[5].startswith("/"):
            mapped.add(parts[5])
    measured = {}
    for line in (output / "java-mapped-sha256.txt").read_text().splitlines():
        sha, path = line.split(None, 1)
        measured[path.strip()] = sha
    assert mapped == set(measured), "Incomplete executable mapping hashes"
    graphics = json.loads((output / "graphics-lock.json").read_text()) if (output / "graphics-lock.json").exists() else None
    if graphics and (output / "build/glfwd").is_dir():
        for name, sha in graphics["files"].items():
            assert hashlib.sha256((output / "build/glfwd" / name).read_bytes()).hexdigest() == sha, name
    laboratory = []
    for path, sha in measured.items():
        if graphics and path == "/bench/glfwd/libGLESv2_adreno.so":
            assert graphics["files"][Path(path).name] == sha
            laboratory.append(path)
        else:
            assert manifest["files"][path.lstrip("/")]["sha256"] == sha, path
    for required in ["/java/bin/javalauncher", "/java/lib/aarch64/server/libjvm.so",
                     "/lib/libc-2.25.so", "/lib/ld-2.25.so", "/tsd/lib/libtsd.dsi.so",
                     "/usr/lib/libtsd.communication.messaging.so"]:
        assert required in measured, required
    for line in (output / "hmi-files-sha256.txt").read_text().splitlines():
        sha, path = line.split(None, 1)
        assert manifest["files"][path.strip().lstrip("/")]["sha256"] == sha
    events = [json.loads(line) for line in (output / "commgr.jsonl").read_text().splitlines()]
    pid = int((output / "java-pid.txt").read_text())
    client = next(e["client"] for e in events if e["action"] == "hello" and e["pid"] == pid and e["uid"] == 1003)
    registrations = []
    for e in events:
        if e["action"] == "receive" and e["client"] == client and e["event"] == 2 and e["type"] == 0:
            name = bytes.fromhex(e["payload_hex"])[1:].split(b"\0")[0].decode()
            registrations.append(name)
            if not negative:
                assert any(r["action"] == "reply" and r["event"] == 3 and r["destination"] == e["sender"] for r in events)
    log = (output / "hmi.log").read_text()
    assert "Launching MIB" in log
    assert '"ivi_application"' in log and "wl_callback" in log
    assert not any(error in log for error in ["Aborted", "SIGSEGV", "UnsatisfiedLinkError", "Insufficient configured threads"])
    if negative:
        assert registrations == ["HMITransceiver"]
        assert not any(e["action"] == "reply" for e in events)
        assert "UI thread started!" not in log and "Starting managed bundles" not in log
        result = "PASS negative control: withheld NameServer reply prevents startup advance"
    else:
        assert registrations == ["HMITransceiver", "HMITransceiverInv"]
        assert sum(e["action"] == "register" and e["success"] for e in events) == 2
        assert "UI thread started!" in log and "Starting managed bundles" in log
        result = "PASS original JOYNEXT HMI startup and NameServer mock roundtrip"
    replies = [e for e in events if e["action"] == "persistence_reply"]
    announces = [e["service"] for e in events if e["action"] == "dsi_announce"]
    if names_only:
        assert not negative and not announces and not replies
        assert "Persistence initial profile already set. Continuing." not in log
        result = "PASS DSI negative control: NameServer works, persistence profile is not ready"
    elif not negative:
        assert "DSIPersistence" in announces and replies
        assert any(e["action"] == "receive" and e["client"] == client and e["event"] == 0x20fe000 for e in events)
        assert not any(e["action"] == "disconnect" and e["client"] == client for e in events)
        reads = []
        for e in events:
            if e["action"] != "receive" or e["client"] != client or e["event"] != 0x203b000 or e["type"] != 3:
                continue
            data = bytes.fromhex(e["payload_hex"])
            if len(data) >= 34 and struct.unpack_from("<I", data, 16)[0] == 1003:
                reads.append((struct.unpack_from("<I", data, 21)[0], struct.unpack_from("<Q", data, 26)[0]))
        assert reads and all(any(r["method"] == 2003 and (r["namespace"], r["key"]) == key for r in replies) for key in reads)
        assert "Persistence initial profile already set. Continuing." in log
        assert "Unsupported DSI Service Version" not in log
        result = "PASS original JOYNEXT HMI ↔ DSIPersistence read/reply and startup profile"
    frame = None
    if graphics:
        assert laboratory, "Graphics proxy did not load"
        renderer = (output / "glhost.log").read_text()
        assert "GL_RENDERER = llvmpipe" in renderer and "GL_VERSION" in renderer
        ppm = (output / "frames/latest.ppm").read_bytes()
        header, width_height, depth, pixels = ppm.split(b"\n", 3)
        assert (header, width_height, depth) == (b"P6", b"1280 720", b"255")
        assert len(pixels) == 1280 * 720 * 3 and len(set(pixels)) > 16
        sequence, width, height = map(int, (output / "frames/seq.txt").read_text().split())
        assert sequence >= 2 and (width, height) == (1280, 720)
        frame = {"width": width, "height": height, "sequence": sequence, "sha256": hashlib.sha256(ppm).hexdigest()}
    rsi = (output / "rsi.jsonl").read_text() if (output / "rsi.jsonl").exists() else ""
    regular = "check And Show WelcomeScreen: REGULAR" in log
    if rsi:
        # Older mock logs occasionally joined two JSON objects on one line across threads.
        rsi_events, cursor = [], 0
        decoder = json.JSONDecoder()
        while cursor < len(rsi):
            if rsi[cursor].isspace():
                cursor += 1
                continue
            event, cursor = decoder.raw_decode(rsi, cursor)
            rsi_events.append(event)
        assert any(e["action"] == "ws_data" and e["event"].startswith("/usermanagement/") for e in rsi_events)
        assert any(e["action"] == "post" and e["data"] == {"isHMIReady": True} for e in rsi_events)
        assert any(e["action"] == "http" and e["message"].startswith('"POST /api/v1/usermanagement/userContexts/')
                   and e["message"].endswith('HTTP/1.1" 200 -') for e in rsi_events)
        assert "DateTimeParseException" not in log and regular
    evidence = {"result": result, "original_executable_mappings": len(measured) - len(laboratory),
                "laboratory_executable_mappings": laboratory,
                "hmi_registrations": registrations, "uid": 1003, "seccomp": 2,
                "wayland": "ivi_application registry bind and sync completed",
                "jvm_alive_at_bounded_stop": True, "rendered_frame": frame,
                "dsi_services": announces, "persistence_replies": len(replies),
                "persistence_writes": sum(r["stored"] for r in replies),
                "rsi_synthetic_profile": bool(rsi), "welcome_regular": regular,
                "input_events": sum(e["action"] == "input" for e in events),
                "limits": ["Volatile synthetic persistence", "Partial DSI service contracts",
                           "No radio/audio/phone/navigation backend or vehicle hardware"]}
    (output / "verification.json").write_text(json.dumps(evidence, indent=2) + "\n")
    return evidence


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("report", nargs="?", type=Path)
    parser.add_argument("--negative-control", action="store_true")
    parser.add_argument("--nameserver-only", action="store_true")
    args = parser.parse_args()
    output = args.report or Path((STAGE / "last-session.txt").read_text().strip())
    print(json.dumps(verify(output, args.negative_control, args.nameserver_only), indent=2))


if __name__ == "__main__":
    main()
