from __future__ import annotations

import unittest
import socket
from contextlib import redirect_stderr
from io import StringIO
from unittest.mock import MagicMock, call, patch

from dcp8001_collector import (
    Dcp8001TcpClient,
    UnsupportedParticleUnitError,
    append_crc,
    check_crc,
    float_low_word_first,
    main,
    registers_from_data,
    u32_low_word_first,
    validate_particle_unit,
)


class FakeSocket:
    def __init__(self, response: bytes) -> None:
        self.response = bytearray(response)
        self.sent = bytearray()
        self.timeout = None

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)

    def recv(self, length: int) -> bytes:
        if not self.response:
            return b""
        chunk = self.response[:length]
        del self.response[:length]
        return bytes(chunk)


class Dcp8001ProtocolTests(unittest.TestCase):
    """Examples taken from the DCP-8001-G communication protocol."""

    @patch("dcp8001_collector.socket.create_connection")
    def test_keepalive_is_enabled_and_optional_tuning_can_be_unsupported(self, connect):
        sock = connect.return_value
        with patch.object(socket, "SIO_KEEPALIVE_VALS", 0x98000004, create=True):
            sock.ioctl.side_effect = OSError("optional tuning unsupported")
            with Dcp8001TcpClient("192.0.2.1"):
                sock.setsockopt.assert_any_call(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                sock.ioctl.assert_called_once_with(socket.SIO_KEEPALIVE_VALS, (1, 30000, 10000))
        sock.close.assert_called_once_with()

    @patch("dcp8001_collector.socket.create_connection")
    def test_socket_setup_failure_does_not_leak_connection(self, connect):
        connect.return_value.settimeout.side_effect = OSError("setup failed")
        with self.assertRaisesRegex(OSError, "setup failed"):
            Dcp8001TcpClient("192.0.2.1")
        connect.return_value.close.assert_called_once_with()

    def test_truncated_register_payload_is_rejected(self):
        client = object.__new__(Dcp8001TcpClient)
        client._request_pdu = MagicMock(return_value=bytes.fromhex("03 04 00 01"))
        with self.assertRaisesRegex(ValueError, "payload does not match"):
            client.read_holding_registers(0, 2)

    def test_read_0_5_um_command_crc(self) -> None:
        command = append_crc(bytes.fromhex("01 03 00 02 00 02"))
        self.assertEqual(command, bytes.fromhex("01 03 00 02 00 02 65 CB"))
        check_crc(command)

    def test_u32_uses_low_register_first(self) -> None:
        # Protocol example: 86 A0 00 01 represents 100,000 particles.
        registers = registers_from_data(bytes.fromhex("86 A0 00 01"))
        self.assertEqual(u32_low_word_first(registers, 0), 100_000)

    def test_float_uses_low_register_first(self) -> None:
        # Protocol example: low word 04 18, high word 3F 9E represents 1.2345.
        registers = registers_from_data(bytes.fromhex("04 18 3F 9E"))
        self.assertAlmostEqual(float_low_word_first(registers, 0), 1.2345, places=4)

    def test_six_particle_register_payload(self) -> None:
        payload = bytes.fromhex(
            "7B BB 00 02 74 A7 00 00 0B 10 00 00 "
            "05 00 00 01 00 12 00 00 00 03 00 00"
        )
        registers = registers_from_data(payload)
        values = [u32_low_word_first(registers, index) for index in range(0, 12, 2)]
        self.assertEqual(values, [162_747, 29_863, 2_832, 66_816, 18, 3])

    def test_tcp_client_ignores_stale_transaction_frame(self) -> None:
        stale_frame = bytes.fromhex("3e 00 00 00 00 03 00 80 01")
        current_frame = bytes.fromhex("00 01 00 00 00 07 01 03 04 c1 0e 00 08")
        sock = FakeSocket(stale_frame + current_frame)
        client = object.__new__(Dcp8001TcpClient)
        client.host = "192.0.2.1"
        client.tcp_port = 502
        client.slave = 1
        client.timeout = 1.0
        client.connect_settle = 0.0
        client.transaction_id = 0
        client._first_request = False
        client.sock = sock

        registers = client.read_holding_registers(0, 2)

        self.assertEqual(registers, [0xC10E, 0x0008])
        self.assertEqual(sock.sent, bytes.fromhex("00 01 00 00 00 06 01 03 00 00 00 02"))

    @patch("dcp8001_collector.select.select")
    def test_tcp_client_discards_unsolicited_bytes_before_first_request(self, select_call) -> None:
        stale_frame = bytes.fromhex("3e 00 00 00 00 03 00 80 01")
        sock = FakeSocket(stale_frame)
        select_call.side_effect = [([sock], [], []), ([], [], [])]
        client = object.__new__(Dcp8001TcpClient)
        client.timeout = 1.0
        client.connect_settle = 1.0
        client.sock = sock

        client._discard_initial_data()

        self.assertEqual(sock.response, b"")
        self.assertEqual(select_call.call_count, 2)

    def test_tcp_realtime_read_uses_small_aligned_requests(self) -> None:
        registers = [0] * 26
        registers[25] = 7
        chunks = [registers[start : start + 2] for start in range(0, 26, 2)]
        client = object.__new__(Dcp8001TcpClient)
        client.slave = 1
        client.timeout = 1.0
        client.connect_settle = 0.0
        client.read_holding_registers = MagicMock(side_effect=[[1], *chunks])

        reading = client.read_realtime()

        self.assertEqual(reading.cleanliness_code, 7)
        self.assertEqual(
            client.read_holding_registers.call_args_list,
            [call(133, 1), *[call(start, 2) for start in range(0, 26, 2)]],
        )
        self.assertEqual(reading.particle_unit_code, 1)
        self.assertEqual(reading.particle_unit_label, "PCS/28.3L")
        self.assertEqual(reading.protocol_profile, "dpc8001-g-protocol-2025-06-04")

    def test_non_ft3_particle_unit_is_rejected_before_alarm_use(self) -> None:
        with self.assertRaisesRegex(UnsupportedParticleUnitError, "register 133=2"):
            validate_particle_unit([2])

    def test_particle_unit_is_rechecked_to_detect_live_device_changes(self) -> None:
        client = object.__new__(Dcp8001TcpClient)
        client.read_holding_registers = MagicMock(side_effect=[[1], [2]])

        self.assertEqual(client.read_particle_unit(), (1, "PCS/28.3L"))
        with self.assertRaises(UnsupportedParticleUnitError):
            client.read_particle_unit()
        self.assertEqual(client.read_holding_registers.call_count, 2)

    def test_device_write_requires_explicit_diagnostic_authorization(self) -> None:
        stderr = StringIO()
        with redirect_stderr(stderr):
            status = main(["--host", "192.168.1.88", "--stop"])

        self.assertEqual(status, 2)
        self.assertIn("Device writes are disabled by default", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
