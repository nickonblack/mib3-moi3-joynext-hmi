#!/usr/bin/env python3
"""Synthetic offline guest profile over the VIWI HTTP/WebSocket contract."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import struct
import threading
from urllib.parse import urlsplit

USER = {"id": "00000000-0000-4000-8000-000000000001", "name": "Lab Guest",
        "authenticationLevel": "level2", "idpUserId": "lab-guest", "isLocked": False,
        "isSpinReset": False, "securityMode": False, "tokenStatus": "ok",
        "lastSync": "1970-01-01T00:00:00.000+00:00", "userRole": "secondaryUser"}
USER["uri"] = "/usermanagement/users/" + USER["id"]
DATA = {
    "/usermanagement/users": [USER],
    "/usermanagement/userContexts": [{"id": "00000000-0000-4000-8000-000000000002", "name": "vehicle",
                                       "activeUser": USER, "identifiedUser": USER,
                                       "ffbKeyAllocation": "off"}],
    "/usermanagement/processes": [],
    "/onlineregistration/primaryUsers": [{"id": "00000000-0000-4000-8000-000000000005",
                                          "name": "Lab primary-user status", "nickname": ""}],
}
SERVICES = [{"id": "00000000-0000-4000-8000-00000000000" + str(index + 3),
             "name": name, "description": "JOYNEXT Emulator Lab synthetic offline service",
             "port": 7770, "uri": "/" + name + "/", "serviceCategories": [category],
             "privileges": [path for path in DATA if path.startswith("/" + name + "/")],
             "versions": [version]}
            for index, (name, category, version) in enumerate([
                ("usermanagement", "auth", "0.6.0"), ("onlineregistration", "communication", "1.1.3")])]
STATE_LOCK = threading.RLock()
LOG_LOCK = threading.Lock()
CLIENTS = set()


def log(event):
    with LOG_LOCK:
        print(json.dumps(event), flush=True)


def resource(event):
    # shortcut: one fixed profile; query filters need implementation before adding more users.
    path = urlsplit(event).path.removeprefix("/api/v1").rstrip("/")
    if not path:
        return SERVICES
    if path in DATA:
        return DATA[path]
    for collection, entries in DATA.items():
        if path.startswith(collection + "/"):
            return next((entry for entry in entries if path == collection + "/" + entry["id"]), None)
    service = next((s for s in SERVICES if path == "/" + s["name"]), None)
    if service:
        return [{"id": key.rsplit("/", 1)[1], "name": key.rsplit("/", 1)[1], "uri": key + "/"}
                for key in DATA if key.startswith(path + "/")]
    return None


def receive(stream):
    def exact(size):
        value = stream.read(size)
        if len(value) != size:
            raise EOFError
        return value
    first, second = exact(2)
    if not first & 128 or first & 112 or not second & 128:
        raise ValueError("Only masked, unfragmented client frames are supported")
    opcode, size = first & 15, second & 127
    if size == 126:
        size = struct.unpack("!H", exact(2))[0]
    elif size == 127:
        size = struct.unpack("!Q", exact(8))[0]
    if size > 65536 or (opcode >= 8 and size > 125):
        raise ValueError("Oversized WebSocket frame")
    mask = exact(4)
    return opcode, bytes(b ^ mask[i % 4] for i, b in enumerate(exact(size)))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.send_lock = threading.Lock()
        self.subscriptions = set()

    def log_message(self, format, *args):
        log({"action": "http", "message": format % args})

    def send_frame(self, payload, opcode=1):
        size = len(payload)
        header = bytes([128 | opcode, size]) if size < 126 else bytes([128 | opcode, 126]) + struct.pack("!H", size)
        with self.send_lock:
            self.wfile.write(header + payload)
            self.wfile.flush()

    def send_json_frame(self, message):
        self.send_frame(json.dumps(message).encode())

    def do_GET(self):
        if self.headers.get("Upgrade", "").lower() == "websocket":
            key = self.headers.get("Sec-WebSocket-Key", "")
            try:
                if len(base64.b64decode(key, validate=True)) != 16:
                    raise ValueError("Invalid WebSocket key")
            except ValueError:
                self.send_error(400)
                return
            accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            with STATE_LOCK:
                CLIENTS.add(self)
            try:
                while True:
                    opcode, payload = receive(self.rfile)
                    if opcode == 8:
                        self.send_frame(payload, 8)
                        break
                    if opcode == 9:
                        self.send_frame(payload, 10)
                        continue
                    if opcode != 1:
                        continue
                    message = json.loads(payload)
                    log({"action": "ws_receive", "message": message})
                    if not isinstance(message, dict) or not isinstance(message.get("event"), str):
                        continue
                    event = message["event"]
                    if message.get("type") == "subscribe":
                        data = resource(event)
                        self.send_json_frame({"type": "subscribe", "event": event, "status": "ok" if data is not None else "error", "code": 200 if data is not None else 404})
                        if data is not None:
                            with STATE_LOCK:
                                self.subscriptions.add(event)
                            self.send_json_frame({"type": "data", "event": event, "status": "ok", "code": 200, "data": data})
                            log({"action": "ws_data", "event": event})
                    elif message.get("type") == "unsubscribe":
                        with STATE_LOCK:
                            self.subscriptions.discard(event)
                        self.send_json_frame({"type": "unsubscribe", "event": event, "status": "ok", "code": 200})
            except (EOFError, OSError, ValueError) as error:
                log({"action": "ws_closed", "reason": str(error)})
            finally:
                with STATE_LOCK:
                    CLIENTS.discard(self)
            self.close_connection = True
            return
        data = resource(self.path)
        body = json.dumps({"status": "ok" if data is not None else "error", "data": data}).encode()
        self.send_response(200 if data is not None else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            size = int(self.headers.get("Content-Length", "-1"))
            if not 0 <= size <= 65536:
                raise ValueError("Invalid body length")
            patch = json.loads(self.rfile.read(size))
            log({"action": "post", "path": self.path, "data": patch})
            item = resource(self.path)
            # shortcut: only the observed HMI-ready write; add other operations with captured contracts.
            if (not any(item is context for context in DATA["/usermanagement/userContexts"])
                    or not isinstance(patch, dict) or set(patch) != {"isHMIReady"}
                    or type(patch["isHMIReady"]) is not bool):
                raise ValueError("Only synthetic user-context isHMIReady can be updated")
            with STATE_LOCK:
                changed = item.get("isHMIReady") != patch["isHMIReady"]
                item.update(patch)
                body = json.dumps({"status": "ok", "data": item}).encode()
                clients = [(client, [event for event in client.subscriptions
                                     if urlsplit(event).path.rstrip("/") in (
                                         "/usermanagement/userContexts", "/usermanagement/userContexts/" + item["id"])])
                           for client in CLIENTS] if changed else []
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            for client, events in clients:
                for event in events:
                    try:
                        client.send_json_frame({"type": "data", "event": event, "status": "ok", "code": 200, "data": resource(event)})
                    except OSError:
                        break
        except (ValueError, OSError) as error:
            self.close_connection = True
            self.send_error(400, str(error))


if __name__ == "__main__":
    servers = [ThreadingHTTPServer(("127.0.0.1", port), Handler) for port in (443, 7770)]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    Path("/run/display/rsi-ready").touch()
    log({"action": "ready", "ports": [443, 7770], "profile": "synthetic guest"})
    threading.Event().wait()
