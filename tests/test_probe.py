import os
import sys
import threading
import socketserver
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import mczprobe  # noqa: E402

FRAME = "01|0B|2|0|0|0|2D|2D|FF|FF|FF|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|2C|FF|0|3|0|0|0"


class DecodeTests(unittest.TestCase):
    def test_decodes_temperatures_state_and_power(self):
        info = mczprobe.decode_info(FRAME)
        self.assertEqual(info["etat"], 11)
        self.assertEqual(info["etat_libelle"], "Puissance 1")
        self.assertEqual(info["temperature_ambiante"], 22.5)  # 0x2D = 45, ÷ 2
        self.assertEqual(info["consigne_temperature"], 22.0)  # 0x2C = 44, ÷ 2
        self.assertEqual(info["niveau_puissance"], 3)

    def test_rejects_other_frame_types(self):
        with self.assertRaises(ValueError):
            mczprobe.decode_info("00|1|2")


class _Handler(socketserver.BaseRequestHandler):
    received = []

    def handle(self):
        request = b""
        while b"\r\n\r\n" not in request:
            request += self.request.recv(1024)
        self.request.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
        first, second = self.request.recv(2)
        length = second & 0x7F
        mask = self.request.recv(4)
        payload = b""
        while len(payload) < length:
            payload += self.request.recv(length - len(payload))
        _Handler.received.append(bytes(b ^ mask[i % 4] for i, b in enumerate(payload)).decode())
        reply = FRAME.encode()  # serveur -> client : non masqué
        self.request.sendall(bytes([0x81, len(reply)]) + reply)


class WebSocketTests(unittest.TestCase):
    def test_round_trip_sends_only_the_read_command(self):
        with socketserver.TCPServer(("127.0.0.1", 0), _Handler) as server:
            threading.Thread(target=server.handle_request, daemon=True).start()
            sock = mczprobe.ws_connect("127.0.0.1", server.server_address[1])
            mczprobe.ws_send_text(sock, "C|RecuperoInfo")
            frame = mczprobe.ws_recv_text(sock)
            sock.close()
        self.assertEqual(_Handler.received, ["C|RecuperoInfo"])
        self.assertTrue(frame.startswith("01|"))


if __name__ == "__main__":
    unittest.main()
