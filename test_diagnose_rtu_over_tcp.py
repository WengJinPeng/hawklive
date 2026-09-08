from __future__ import annotations

import unittest
from unittest.mock import patch

from dcp8001_collector import append_crc
from diagnose_rtu_over_tcp import read_rtu_over_tcp


class RtuOverTcpDiagnosticTests(unittest.TestCase):
    @patch("diagnose_rtu_over_tcp.time.sleep")
    @patch("diagnose_rtu_over_tcp.socket.create_connection")
    def test_sends_documented_read_as_crc_framed_rtu(self, connect, _sleep) -> None:
        response = append_crc(bytes.fromhex("01 03 04 86 a0 00 01"))

        class FakeSocket:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def settimeout(self, _timeout):
                pass

            def sendall(self, request):
                self.request = request

            def recv(self, _size):
                return response

        connection = FakeSocket()
        connect.return_value = connection

        result = read_rtu_over_tcp("192.168.2.30")

        self.assertEqual(connection.request.hex(" "), "01 03 00 00 00 02 c4 0b")
        self.assertEqual(result, response)


if __name__ == "__main__":
    unittest.main()
