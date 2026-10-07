import datetime
import socket
import ssl
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

import cloud_tls


class CloudTlsTests(unittest.TestCase):
    def tearDown(self):
        cloud_tls.cloud_ssl_context.cache_clear()

    def test_packaged_roots_keep_certificate_and_hostname_verification(self):
        self.assertGreaterEqual(cloud_tls.verify_packaged_roots(), 100)
        context = cloud_tls.cloud_ssl_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_https_request_uses_shared_verified_context(self):
        with patch('cloud_tls.urllib.request.urlopen') as opener:
            cloud_tls.cloud_urlopen('https://example.test', timeout=7)
            self.assertIs(opener.call_args.kwargs['context'], cloud_tls.cloud_ssl_context())
            self.assertEqual(opener.call_args.kwargs['timeout'], 7)

    def test_empty_system_store_recovers_only_for_a_trusted_matching_certificate(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Isolated test CA')])
        now = datetime.datetime.now(datetime.timezone.utc)
        ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
              .public_key(key.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(now-datetime.timedelta(days=1)).not_valid_after(now+datetime.timedelta(days=1))
              .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
              .sign(key, hashes.SHA256()))
        leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')]))
                .issuer_name(name).public_key(leaf_key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now-datetime.timedelta(days=1)).not_valid_after(now+datetime.timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]), critical=False)
                .sign(key, hashes.SHA256()))
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'ca.pem').write_bytes(ca.public_bytes(serialization.Encoding.PEM))
            (root/'leaf.pem').write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
            (root/'key.pem').write_bytes(leaf_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(root/'leaf.pem', root/'key.pem')
            server.socket = tls.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                # Simulate the empty default trust store of a fresh SYSTEM account.
                with patch('cloud_tls.ssl.create_default_context', side_effect=lambda: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)), patch('cloud_tls.certifi.where', return_value=str(root/'ca.pem')):
                    cloud_tls.cloud_ssl_context.cache_clear()
                    context = cloud_tls.cloud_ssl_context()
                for context_to_test, host, expected in [(context, 'localhost', True), (context, 'wrong.test', False), (ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT), 'localhost', False)]:
                    with socket.create_connection(server.server_address, timeout=3) as raw:
                        if expected:
                            with context_to_test.wrap_socket(raw, server_hostname=host) as connection:
                                self.assertTrue(connection.getpeercert())
                        else:
                            with self.assertRaises(ssl.SSLCertVerificationError):
                                context_to_test.wrap_socket(raw, server_hostname=host)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=3)
