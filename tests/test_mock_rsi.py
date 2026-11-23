import importlib.util
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import socket
import struct
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

spec = importlib.util.spec_from_file_location("mock_rsi", Path(__file__).resolve().parents[1] / "joynext/mock_rsi.py")
mock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mock)


class RsiTest(unittest.TestCase):
    def test_concurrent_json_log(self):
        class SlowWriter(io.StringIO):
            def write(self, value):
                result = super().write(value)
                time.sleep(0.0001)
                return result
        output = SlowWriter()
        with redirect_stdout(output):
            threads = [threading.Thread(target=lambda: [mock.log({"action": "test"}) for _ in range(10)]) for _ in range(10)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(events, [{"action": "test"}] * 100)

    def test_profile_routes(self):
        self.assertEqual(mock.resource("/api/v1/#session"), mock.SERVICES)
        self.assertEqual(mock.resource("/usermanagement/users/?name=guest#session"), [mock.USER])
        self.assertEqual(mock.resource(mock.USER["uri"]), mock.USER)
        self.assertIsNone(mock.resource("/unknown"))
        self.assertIn("nickname", mock.resource("/onlineregistration/primaryUsers")[0])
        self.assertRegex(mock.USER["lastSync"], r"\.\d{3}[+-]\d\d:\d\d$")

    def test_masked_frames_and_limits(self):
        message, mask = b'{"event":"/"}', b"test"
        encoded = bytes(b ^ mask[i % 4] for i, b in enumerate(message))
        frame = bytes([129, 128 | len(message)]) + mask + encoded
        self.assertEqual(mock.receive(io.BytesIO(frame)), (1, message))
        for invalid in [b"", frame[:-1], bytes([1]) + frame[1:], bytes([129, 127]),
                        bytes([129, 255]) + struct.pack("!Q", 65537)]:
            with self.assertRaises((EOFError, ValueError)):
                mock.receive(io.BytesIO(invalid))

    def test_http_and_websocket_contract(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), mock.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            connection.request("GET", "/api/v1/")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.load(response)["data"], mock.SERVICES)
            path = "/api/v1/usermanagement/userContexts/" + mock.DATA["/usermanagement/userContexts"][0]["id"]
            for patch, expected in [({"isHMIReady": True}, 200), ({"isHMIReady": True}, 200),
                                    ({"isHMIReady": "true"}, 400), ({"id": "other"}, 400)]:
                connection.request("POST", path, json.dumps(patch), {"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, expected)
                response.read()
            connection.close()
            with socket.create_connection(("127.0.0.1", server.server_port), timeout=2) as client:
                client.sendall(b"GET /api/v1/ HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                               b"Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n")
                with client.makefile("rb") as stream:
                    self.assertIn(b"101", stream.readline())
                    while stream.readline() != b"\r\n":
                        pass
                    message, mask = b'{"type":"subscribe","event":"/api/v1/#test"}', b"test"
                    client.sendall(bytes([129, 128 | len(message)]) + mask
                                   + bytes(b ^ mask[i % 4] for i, b in enumerate(message)))
                    for kind in ("subscribe", "data"):
                        first, length = stream.read(2)
                        self.assertEqual(first, 129)
                        if length == 126:
                            length = struct.unpack("!H", stream.read(2))[0]
                        payload = json.loads(stream.read(length))
                        self.assertEqual((payload["type"], payload["status"], payload["code"]), (kind, "ok", 200))
                        if kind == "data":
                            self.assertEqual(payload["data"], mock.SERVICES)
                    client.sendall(b"\x88\x80test")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
