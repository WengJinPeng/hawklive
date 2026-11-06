# Gentle polling (0.6.14)

Baseline: b9b3683 (0.6.13); includes the existing identity guard, passive identity evidence, maintenance and export fixes.

- Normal polling waits 30 seconds after a completed sample instead of 10; explicit DCP_POLL_SECONDS overrides remain respected.
- Each of the 14 sequential Modbus requests waits at least 250 ms after the preceding response instead of 50 ms. A normal round therefore includes at least 3.25 seconds of intentional quiet time, plus device I/O.
- Alarm evaluation uses that same sampling cadence. History remains 120 seconds, rounded to the next successful sample; alarms and recovery still persist immediately.
- One compatibility retry for Modbus exceptions 3/6 retains its existing shared network I/O budget, plus the fixed quiet time required by the retry. No unbounded retry or deadline reset.
- Existing 10/60/120-second reconnect backoff, per-device serialization, read-only register access, identity checks and history ownership remain intact.
- Diagnostic snapshots report request_interval_seconds alongside effective poll_seconds so field activation can be verified independently of the EXE version.

Validation must include real localhost TCP fixtures for slower replies, a gateway requiring 200 ms quiet gaps, half-open connection recovery, single/repeated Modbus exceptions, durable recovery samples, and cancellation. These fixtures do not prove the physical gateway's failure cause or field stability.
