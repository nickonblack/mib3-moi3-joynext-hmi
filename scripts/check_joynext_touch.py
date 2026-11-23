#!/usr/bin/env python3
"""Exercise native viewer mouse handlers against an active, isolated original HMI."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import time
from run_joynext import STAGE, check_isolation


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    output = args.report.resolve()
    renderer = json.loads((output / "weston-isolation.json").read_text())
    check_isolation(renderer)
    check_isolation(json.loads((output / "isolation.json").read_text()))
    for attempt in range(450):
        if "WORKUNIT_SYSTEM_SETUP" in (output / "hmi.log").read_text():
            break
        time.sleep(0.2)
    else:
        raise RuntimeError("System setup did not start within 90 seconds")
    screenshots = output / "touch"
    screenshots.mkdir(exist_ok=True)
    log_offset = (output / "hmi.log").stat().st_size
    steps = ["espera 2", "toque 360 175", "espera 1", "tecla Меню", "espera 1"]
    tiles = [("settings", 1060, 170, "HMI_SYSTEMSETUP"), ("radio", 210, 170, "HMI_RADIO"),
             ("vehicle", 500, 170, "HMI_CAR"), ("users", 780, 170, "HMI_USERMANAGEMENT"),
             ("media", 210, 410, "HMI_MEDIA"), ("sound", 500, 410, "HMI_TONE"),
             ("help", 780, 410, "HMI_QUICKSTARTGUIDE")]
    for name, x, y, _ in tiles:
        steps += [f"toque {x} {y}", "espera 1.5", f"quadro {screenshots / (name + '.png')}"]
        if name == "settings":
            steps += [f"foto {screenshots / 'viewer-ru.png'}"]
        steps += ["tecla Меню", "espera 1"]
    steps += ["sai"]
    viewer = STAGE / "viewer/JOYNEXT Emulator Lab.app/Contents/MacOS/joynext-viewer"
    with (screenshots / "viewer.log").open("w") as log:
        subprocess.run([str(viewer), "--container", renderer["Name"].lstrip("/"),
                        "--ponte", "/bench/glfwd/ponte", "--soquete", "/tmp/moi3-quadros.sock",
                        "--entrada", "/run/display/dsi-input", "--roteiro", ";".join(steps)],
                       stdout=log, stderr=subprocess.STDOUT, check=True, timeout=90)
    hmi = (output / "hmi.log").read_bytes()[log_offset:].decode()
    assert "SYSTEM_SETUP_MAIN" in hmi and "HMI_SYSTEMSETUP" in hmi, "Touch did not open Settings"
    assert "tecla desconhecida" not in (screenshots / "viewer.log").read_text(), "Translated Menu key not found"
    actual_routes = iter(re.findall(r"doRequestContent content: (HMI_\w+)", hmi))
    for name, _, _, expected in tiles:
        assert "HMI_GRIDMENU" in actual_routes, "No return to menu before " + name
        assert expected in actual_routes, "Touch did not open " + name
        assert (screenshots / (name + ".png")).stat().st_size > 1000, name
    routes = [line for line in hmi.splitlines() if "doRequestContent content:" in line or "enterView(" in line]
    evidence = {"result": "PASS native mouse → pixel coordinates → DSI Gesture2 → all seven menu targets",
                "menu_tiles_exercised": [tile[0] for tile in tiles], "hmi_routes": routes,
                "screenshots": str(screenshots), "scope": "Routing/screens only; missing service backends remain unavailable"}
    (output / "touch-check.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
