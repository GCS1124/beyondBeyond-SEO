import os
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from beyondseo.network import Config, Transport, check_proxied_target, configured_proxy


class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<html><title>via proxy</title></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class ConnectProxy(threading.Thread):
    """Minimal CONNECT-only proxy that records the requested targets."""

    def __init__(self):
        super().__init__(daemon=True)
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        self.targets = []

    def run(self):
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(client,), daemon=True).start()

    def handle(self, client):
        head = b""
        while b"\r\n\r\n" not in head:
            head += client.recv(4096)
        target = head.split(b"\r\n", 1)[0].split()[1].decode()
        self.targets.append(target)
        host, port = target.rsplit(":", 1)
        upstream = socket.create_connection((host, int(port)))
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")

        def pump(src, dst):
            try:
                while data := src.recv(65536):
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                dst.close()

        threading.Thread(target=pump, args=(client, upstream), daemon=True).start()
        pump(upstream, client)

    def close(self):
        self.listener.close()


class ProxySettingTests(unittest.TestCase):
    def test_unset_means_direct(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(configured_proxy("https://example.com/"))

    def test_explicit_and_env_modes(self):
        with patch.dict(os.environ, {"BEYONDSEO_PROXY": "http://10.0.0.5:3128"}, clear=True):
            self.assertEqual(configured_proxy("https://example.com/"), ("10.0.0.5", 3128))
        env = {"BEYONDSEO_PROXY": "env", "HTTPS_PROXY": "http://127.0.0.1:9000", "NO_PROXY": "skip.example"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(configured_proxy("https://example.com/"), ("127.0.0.1", 9000))
            self.assertIsNone(configured_proxy("https://skip.example/"))
        with patch.dict(os.environ, {"BEYONDSEO_PROXY": "env"}, clear=True):
            self.assertIsNone(configured_proxy("https://example.com/"))

    def test_rejects_non_http_proxy(self):
        with patch.dict(os.environ, {"BEYONDSEO_PROXY": "socks5://127.0.0.1:1080"}, clear=True):
            with self.assertRaises(ValueError):
                configured_proxy("https://example.com/")

    def test_local_targets_stay_blocked(self):
        for url in ("http://localhost/", "http://127.0.0.1/", "http://10.1.2.3/", "http://printer.local/"):
            with self.assertRaises(ValueError, msg=url):
                check_proxied_target(url)
        check_proxied_target("https://www.example.com/")
        check_proxied_target("http://127.0.0.1/", allow_private=True)


class ProxyTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.proxy = ConnectProxy()
        cls.proxy.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.proxy.close()

    def test_fetch_goes_through_connect_tunnel(self):
        url = f"http://127.0.0.1:{self.server.server_port}/"
        env = {"BEYONDSEO_PROXY": f"http://127.0.0.1:{self.proxy.port}"}
        with patch.dict(os.environ, env), patch("beyondseo.network.addresses") as direct:
            response = Transport(Config(url, allow_private=True, delay=0, retries=0)).fetch(url)
        self.assertEqual(response.status, 200, response.error)
        self.assertIn(b"via proxy", response.body)
        self.assertIn(f"127.0.0.1:{self.server.server_port}", self.proxy.targets)
        direct.assert_not_called()


if __name__ == "__main__":
    unittest.main()
