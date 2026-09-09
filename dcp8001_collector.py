from __future__ import annotations

import argparse
import json
import secrets
import select
import socket
import struct
import sys
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover - used for friendly CLI errors
    serial = None
    list_ports = None


PARTICLE_CHANNELS = (
    ("pm_0_3_um", 0),
    ("pm_0_5_um", 2),
    ("pm_1_0_um", 4),
    ("pm_2_5_um", 6),
    ("pm_5_0_um", 8),
    ("pm_10_0_um", 10),
)

FLOAT_CHANNELS = (
    ("flow", 12),
    ("temperature", 14),
    ("humidity", 16),
    ("dew_point", 18),
    ("wind_speed", 20),
    ("pressure_diff", 22),
)

ALARM_BITS = (
    ("pm_0_3_um", 0),
    ("pm_0_5_um", 1),
    ("pm_1_0_um", 2),
    ("pm_2_5_um", 3),
    ("pm_5_0_um", 4),
    ("pm_10_0_um", 5),
)

CLEANLINESS_LABELS = {
    3: "< CLASS 4",
    4: "CLASS 4",
    5: "CLASS 5",
    6: "CLASS 6",
    7: "CLASS 7",
    8: "CLASS 8",
    9: "CLASS 9",
    10: "> CLASS 9",
}

PARTICLE_UNIT_LABELS = {
    0: "PCS/L",
    1: "PCS/28.3L",
    2: "PCS/m3",
    3: "PCS/2.83L",
}
REQUIRED_PARTICLE_UNIT_CODE = 1
PROTOCOL_PROFILE = "dpc8001-g-protocol-2025-06-04"


class UnsupportedParticleUnitError(ValueError):
    """Raised when device values cannot safely use the configured ft3 thresholds."""


@dataclass
class Dcp8001Reading:
    timestamp: float
    slave: int
    particles: dict[str, int]
    environment: dict[str, float]
    alarm_raw: int
    alarms: dict[str, bool]
    cleanliness_code: int
    cleanliness_label: str
    particle_unit_code: int
    particle_unit_label: str
    protocol_profile: str


def validate_particle_unit(registers: list[int]) -> tuple[int, str]:
    if len(registers) != 1:
        raise ValueError("Particle display unit response must contain one register")
    code = int(registers[0])
    label = PARTICLE_UNIT_LABELS.get(code, f"UNKNOWN({code})")
    if code != REQUIRED_PARTICLE_UNIT_CODE:
        raise UnsupportedParticleUnitError(
            "Particle unit is "
            f"{label} (register 133={code}); configure the device to PCS/28.3L "
            "before using particles/ft3 thresholds"
        )
    return code, label


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def append_crc(frame: bytes) -> bytes:
    crc = crc16_modbus(frame)
    return frame + bytes((crc & 0xFF, (crc >> 8) & 0xFF))


def check_crc(frame: bytes) -> None:
    if len(frame) < 4:
        raise ValueError("Response is too short")
    expected = crc16_modbus(frame[:-2])
    actual = frame[-2] | (frame[-1] << 8)
    if actual != expected:
        raise ValueError(f"CRC mismatch: got 0x{actual:04X}, expected 0x{expected:04X}")


def registers_from_data(data: bytes) -> list[int]:
    if len(data) % 2:
        raise ValueError("Register payload has odd byte length")
    return [int.from_bytes(data[i : i + 2], "big") for i in range(0, len(data), 2)]


def u32_low_word_first(registers: list[int], index: int) -> int:
    low = registers[index]
    high = registers[index + 1]
    return (high << 16) | low


def float_low_word_first(registers: list[int], index: int) -> float:
    low = registers[index]
    high = registers[index + 1]
    raw = high.to_bytes(2, "big") + low.to_bytes(2, "big")
    return struct.unpack(">f", raw)[0]


class Dcp8001Client:
    def __init__(
        self,
        port: str,
        slave: int = 1,
        baudrate: int = 9600,
        timeout: float = 1.0,
    ) -> None:
        if serial is None:
            raise RuntimeError("pyserial is not installed. Run: py -m pip install -r requirements.txt")
        self.slave = slave
        self.ser = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=timeout,
        )

    def close(self) -> None:
        self.ser.close()

    def __enter__(self) -> Dcp8001Client:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _request(self, payload: bytes, expected_min_len: int) -> bytes:
        frame = append_crc(payload)
        self.ser.reset_input_buffer()
        self.ser.write(frame)
        self.ser.flush()
        response = self.ser.read(expected_min_len)
        if len(response) < expected_min_len:
            more = self.ser.read(256)
            response += more
        check_crc(response)
        if response[0] != self.slave:
            raise ValueError(f"Unexpected slave address: got {response[0]}, expected {self.slave}")
        if response[1] & 0x80:
            code = response[2] if len(response) > 2 else None
            raise ValueError(f"Modbus exception response: function=0x{response[1]:02X}, code={code}")
        return response

    def read_holding_registers(self, start: int, quantity: int) -> list[int]:
        payload = bytes(
            (
                self.slave,
                0x03,
                (start >> 8) & 0xFF,
                start & 0xFF,
                (quantity >> 8) & 0xFF,
                quantity & 0xFF,
            )
        )
        expected_len = 5 + quantity * 2
        response = self._request(payload, expected_len)
        if response[1] != 0x03:
            raise ValueError(f"Unexpected function code: 0x{response[1]:02X}")
        byte_count = response[2]
        if byte_count != quantity * 2:
            raise ValueError(f"Unexpected byte count: got {byte_count}, expected {quantity * 2}")
        return registers_from_data(response[3 : 3 + byte_count])

    def write_single_register(self, address: int, value: int) -> None:
        payload = bytes(
            (
                self.slave,
                0x06,
                (address >> 8) & 0xFF,
                address & 0xFF,
                (value >> 8) & 0xFF,
                value & 0xFF,
            )
        )
        response = self._request(payload, 8)
        if response[:-2] != payload:
            raise ValueError("Write response does not echo request")

    def start_sampling(self) -> None:
        self.write_single_register(50, 1)

    def stop_sampling(self) -> None:
        self.write_single_register(50, 0)

    def read_particle_unit(self) -> tuple[int, str]:
        return validate_particle_unit(self.read_holding_registers(133, 1))

    def read_realtime(self) -> Dcp8001Reading:
        particle_unit_code, particle_unit_label = self.read_particle_unit()
        registers = self.read_holding_registers(0, 26)
        particles = {name: u32_low_word_first(registers, idx) for name, idx in PARTICLE_CHANNELS}
        environment = {
            name: round(float_low_word_first(registers, idx), 4)
            for name, idx in FLOAT_CHANNELS
        }
        alarm_raw = registers[24]
        alarms = {name: bool(alarm_raw & (1 << bit)) for name, bit in ALARM_BITS}
        cleanliness_code = registers[25]
        return Dcp8001Reading(
            timestamp=time.time(),
            slave=self.slave,
            particles=particles,
            environment=environment,
            alarm_raw=alarm_raw,
            alarms=alarms,
            cleanliness_code=cleanliness_code,
            cleanliness_label=CLEANLINESS_LABELS.get(cleanliness_code, f"UNKNOWN({cleanliness_code})"),
            particle_unit_code=particle_unit_code,
            particle_unit_label=particle_unit_label,
            protocol_profile=PROTOCOL_PROFILE,
        )


class Dcp8001TcpClient:
    def __init__(
        self,
        host: str,
        tcp_port: int = 502,
        slave: int = 1,
        timeout: float = 1.0,
        connect_settle: float = 1.0,
    ) -> None:
        self.host = host
        self.tcp_port = tcp_port
        self.slave = slave
        self.timeout = timeout
        self.connect_settle = max(0.0, connect_settle)
        # Avoid reusing transaction 1 on every short-lived TCP connection. This
        # is standard-compatible and reduces collisions with cached stale frames.
        self.transaction_id = secrets.randbits(16)
        self._first_request = True
        self.diagnostic_stage = "connected"
        self.request_sent = False
        self._read_deadline: float | None = None
        self.sock = socket.create_connection((host, tcp_port), timeout=timeout)
        self.sock.settimeout(timeout)

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # A disconnected peer still needs the local descriptor closed.
        self.sock.close()

    def __enter__(self) -> Dcp8001TcpClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _next_transaction_id(self) -> int:
        self.transaction_id = (self.transaction_id + 1) & 0xFFFF
        return self.transaction_id

    def _request_pdu(self, pdu: bytes) -> bytes:
        self.request_sent = False
        self.diagnostic_stage = "initial_settle"
        if self._first_request:
            self._first_request = False
            self._discard_initial_data()
        transaction_id = self._next_transaction_id()
        length = len(pdu) + 1
        header = (
            transaction_id.to_bytes(2, "big")
            + b"\x00\x00"
            + length.to_bytes(2, "big")
            + bytes((self.slave,))
        )
        deadline = time.monotonic() + self.timeout
        read_deadline = getattr(self, "_read_deadline", None)
        if read_deadline is not None:
            deadline = min(deadline, read_deadline)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Timed out reading complete Modbus TCP sample")
        self.sock.settimeout(remaining)
        self.diagnostic_stage = "send_request"
        self.request_sent = None  # sendall failure can mean partially transmitted bytes.
        self.sock.sendall(header + pdu)
        self.request_sent = True
        self.diagnostic_stage = "response_header"
        try:
            while True:
                self.diagnostic_stage = "response_header"
                response_header = self._recv_exact(7, deadline)
                rx_transaction_id = int.from_bytes(response_header[0:2], "big")
                protocol_id = int.from_bytes(response_header[2:4], "big")
                rx_length = int.from_bytes(response_header[4:6], "big")
                unit_id = response_header[6]
                if protocol_id != 0:
                    raise ValueError(f"Unexpected Modbus TCP protocol id: {protocol_id}")
                if not 2 <= rx_length <= 254:
                    raise ValueError(f"Unexpected Modbus TCP length: {rx_length}")
                self.diagnostic_stage = "response_body"
                body = self._recv_exact(rx_length - 1, deadline)

                # Some WiFi adapters leave a response from an older transaction in
                # the socket when a client connects. Consume complete stale frames
                # and accept only the response that belongs to this request.
                if rx_transaction_id != transaction_id:
                    continue
                if unit_id != self.slave:
                    raise ValueError(f"Unexpected slave address: got {unit_id}, expected {self.slave}")
                if body[0] & 0x80:
                    code = body[1] if len(body) > 1 else None
                    raise ValueError(f"Modbus exception response: function=0x{body[0]:02X}, code={code}")
                self.diagnostic_stage = "response_validated"
                return body
        finally:
            self.sock.settimeout(self.timeout)

    def _discard_initial_data(self) -> None:
        """Let a newly accepted WiFi socket settle and discard unsolicited bytes."""
        deadline = time.monotonic() + self.connect_settle
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([self.sock], [], [], remaining)
            if not ready:
                break
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("Socket closed while waiting for initial Modbus TCP data")
        self.sock.settimeout(self.timeout)

    def _recv_exact(self, length: int, deadline: float | None = None) -> bytes:
        chunks = bytearray()
        while len(chunks) < length:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Timed out waiting for Modbus TCP response")
                self.sock.settimeout(remaining)
            chunk = self.sock.recv(length - len(chunks))
            if not chunk:
                raise ConnectionError("Socket closed while reading response")
            chunks.extend(chunk)
        return bytes(chunks)

    def read_holding_registers(self, start: int, quantity: int) -> list[int]:
        pdu = bytes(
            (
                0x03,
                (start >> 8) & 0xFF,
                start & 0xFF,
                (quantity >> 8) & 0xFF,
                quantity & 0xFF,
            )
        )
        response = self._request_pdu(pdu)
        if response[0] != 0x03:
            raise ValueError(f"Unexpected function code: 0x{response[0]:02X}")
        byte_count = response[1]
        if byte_count != quantity * 2:
            raise ValueError(f"Unexpected byte count: got {byte_count}, expected {quantity * 2}")
        return registers_from_data(response[2 : 2 + byte_count])

    def write_single_register(self, address: int, value: int) -> None:
        pdu = bytes(
            (
                0x06,
                (address >> 8) & 0xFF,
                address & 0xFF,
                (value >> 8) & 0xFF,
                value & 0xFF,
            )
        )
        response = self._request_pdu(pdu)
        if response != pdu:
            raise ValueError("Write response does not echo request")

    def start_sampling(self) -> None:
        self.write_single_register(50, 1)

    def stop_sampling(self) -> None:
        self.write_single_register(50, 0)

    def read_particle_unit(self) -> tuple[int, str]:
        return validate_particle_unit(self.read_holding_registers(133, 1))

    def read_realtime(self) -> Dcp8001Reading:
        # Fourteen small requests make one sample. Per-request timeouts alone
        # allow a degraded gateway to occupy a worker for fourteen timeouts.
        self._read_deadline = time.monotonic() + self.timeout * 2 + self.connect_settle
        try:
            return self._read_realtime_sample()
        finally:
            self._read_deadline = None

    def _read_realtime_sample(self) -> Dcp8001Reading:
        particle_unit_code, particle_unit_label = self.read_particle_unit()
        # Keep responses small for embedded bridges with limited socket buffers.
        # Each request still contains one complete U32/Float value.
        registers: list[int] = []
        for start in range(0, 26, 2):
            registers.extend(self.read_holding_registers(start, 2))
        particles = {name: u32_low_word_first(registers, idx) for name, idx in PARTICLE_CHANNELS}
        environment = {
            name: round(float_low_word_first(registers, idx), 4)
            for name, idx in FLOAT_CHANNELS
        }
        alarm_raw = registers[24]
        alarms = {name: bool(alarm_raw & (1 << bit)) for name, bit in ALARM_BITS}
        cleanliness_code = registers[25]
        return Dcp8001Reading(
            timestamp=time.time(),
            slave=self.slave,
            particles=particles,
            environment=environment,
            alarm_raw=alarm_raw,
            alarms=alarms,
            cleanliness_code=cleanliness_code,
            cleanliness_label=CLEANLINESS_LABELS.get(cleanliness_code, f"UNKNOWN({cleanliness_code})"),
            particle_unit_code=particle_unit_code,
            particle_unit_label=particle_unit_label,
            protocol_profile=PROTOCOL_PROFILE,
        )


def print_ports() -> int:
    if list_ports is None:
        print("pyserial is not installed. Run: py -m pip install -r requirements.txt", file=sys.stderr)
        return 2
    ports = list(list_ports.comports())
    if not ports:
        print("No serial ports found.")
        return 0
    for port in ports:
        print(f"{port.device}\t{port.description}")
    return 0


def print_reading(reading: Dcp8001Reading, as_json: bool) -> None:
    payload = asdict(reading)
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read DCP8001-G realtime data over Modbus RTU or WiFi/Modbus TCP.")
    parser.add_argument("--list-ports", action="store_true", help="List available serial ports and exit.")
    parser.add_argument("--port", help="Serial port, for example COM3.")
    parser.add_argument("--host", help="Modbus TCP host/IP, for example 192.168.1.88.")
    parser.add_argument("--tcp-port", type=int, default=502, help="Modbus TCP port. Default: 502.")
    parser.add_argument("--slave", type=int, default=1, help="Modbus slave address. Default: 1.")
    parser.add_argument("--baudrate", type=int, default=9600, help="Serial baud rate. Default: 9600.")
    parser.add_argument("--timeout", type=float, default=1.0, help="Serial timeout seconds. Default: 1.0.")
    parser.add_argument("--start", action="store_true", help="Send sampling start command before reading.")
    parser.add_argument("--stop", action="store_true", help="Send sampling stop command and exit.")
    parser.add_argument(
        "--allow-device-write",
        action="store_true",
        help="Explicitly authorize the diagnostic --start/--stop write for this invocation.",
    )
    parser.add_argument("--loop", type=positive_int, help="Read repeatedly every N seconds.")
    parser.add_argument("--json", action="store_true", help="Print compact JSON lines.")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_ports:
        return print_ports()
    if not args.port and not args.host:
        print("Missing --port or --host. Use --host for WiFi/Modbus TCP.", file=sys.stderr)
        return 2
    if args.port and args.host:
        print("Use either --port or --host, not both.", file=sys.stderr)
        return 2
    if args.start and args.stop:
        print("Use either --start or --stop, not both.", file=sys.stderr)
        return 2
    if (args.start or args.stop) and not args.allow_device_write:
        print(
            "Device writes are disabled by default. For an authorized bench diagnostic, "
            "repeat with --allow-device-write.",
            file=sys.stderr,
        )
        return 2
    try:
        if args.host:
            client_factory = lambda: Dcp8001TcpClient(args.host, args.tcp_port, args.slave, args.timeout)
        else:
            client_factory = lambda: Dcp8001Client(args.port, args.slave, args.baudrate, args.timeout)
        with client_factory() as client:
            if args.stop:
                client.stop_sampling()
                print("Sampling stopped.")
                return 0
            if args.start:
                client.start_sampling()
                print("Sampling started.", file=sys.stderr)
                time.sleep(0.2)
            while True:
                print_reading(client.read_realtime(), args.json)
                if not args.loop:
                    break
                time.sleep(args.loop)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
