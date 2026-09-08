from __future__ import annotations

import argparse
import socket
import time

from dcp8001_collector import append_crc, crc16_modbus


def read_rtu_over_tcp(
    host: str,
    tcp_port: int = 502,
    slave: int = 1,
    start: int = 0,
    quantity: int = 2,
    timeout: float = 2.0,
    settle: float = 1.0,
) -> bytes:
    request = append_crc(
        bytes((slave, 0x03)) + start.to_bytes(2, "big") + quantity.to_bytes(2, "big")
    )
    with socket.create_connection((host, tcp_port), timeout=timeout) as connection:
        connection.settimeout(timeout)
        if settle > 0:
            time.sleep(settle)
        connection.sendall(request)
        response = connection.recv(256)
    if len(response) < 5:
        raise RuntimeError(f"RTU-over-TCP response is too short: {response.hex(' ')}")
    received_crc = int.from_bytes(response[-2:], "little")
    calculated_crc = crc16_modbus(response[:-2])
    if received_crc != calculated_crc:
        raise RuntimeError(
            f"RTU-over-TCP CRC mismatch: received={received_crc:04x}, expected={calculated_crc:04x}"
        )
    return response


def main() -> int:
    parser = argparse.ArgumentParser(description="Read one Modbus RTU frame over a TCP socket.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--tcp-port", type=int, default=502)
    parser.add_argument("--slave", type=int, default=1)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--quantity", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--settle", type=float, default=1.0)
    args = parser.parse_args()
    response = read_rtu_over_tcp(
        args.host,
        args.tcp_port,
        args.slave,
        args.start,
        args.quantity,
        args.timeout,
        args.settle,
    )
    print(response.hex(" "))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
