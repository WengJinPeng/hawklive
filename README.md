# DPC8001-G WiFi / Modbus Collector

This collector is currently scoped to the `DPC8001-G` model. Internal service,
directory, and script names that still contain `DCP8001` are retained as legacy
installation identifiers so an upgrade does not silently create a second data
directory or Windows service.
It talks directly to an RS485 device over Modbus RTU or to a WiFi/Ethernet
endpoint over standard Modbus TCP (MBAP). The collector does not use
`Board.exe`.

## Verified DCP-8001-G Protocol

The supplied June 2025 communication protocol confirms:

- The protocol explicitly specifies RS485 / Modbus RTU and CRC-framed examples.
- Register `151` labels the default Modbus TCP port as `502`, but the protocol
  provides no MBAP or transparent RTU-over-TCP request/response example.
- A 2026-09-04 read-only field test against `192.168.2.30:502` proved standard
  MBAP: Unit ID `1`, function `0x03`, registers `0-1`, and the echoed
  transaction ID all returned a valid response. Sending an RTU CRC frame to the
  same port produced an MBAP exception, so Ethernet is not transparent
  RTU-over-TCP on the tested device.
- Default Modbus unit/slave address: `1`.
- Realtime read: function `0x03`, holding registers `0` through `25`.
- Particle channels: registers `0` through `11`, U32 with low register first.
- Temperature and humidity: registers `14` through `17`, IEEE-754 Float with
  low register first.
- Particle unit: holding register `133`, rechecked on every sampling cycle. The current safety policy accepts only
  value `1` (`PCS/28.3L`, approximately particles/ft³); other units stop that
  device's read with an explicit error instead of relabelling or guessing a
  conversion. The unit code, label, and protocol profile are stored with each
  new reading locally and in the cloud.
- The dashboard only reads the instrument; device-control commands are not
  exposed to customer accounts.

Important: the separate device user manual swaps the meanings of registers
`12/14/16/18` compared with the communication protocol. The implementation
already follows the dedicated communication protocol. On 2026-09-03 the
project owner relayed the manufacturer's decision that DPC8001-G follows that
protocol when the documents conflict, so no parser change was required.
Real-device value and unit validation is still required before production acceptance. See
[`OPEN-QUESTIONS-MANUFACTURER-CUSTOMER.md`](OPEN-QUESTIONS-MANUFACTURER-CUSTOMER.md).
The document-to-code evidence and unresolved contradictions are summarized in
[`DOCUMENT-TRACEABILITY-AUDIT.md`](DOCUMENT-TRACEABILITY-AUDIT.md).
The seven hard conflicts, one unsafe discovery example, and the exact questions
requiring a manufacturer decision are isolated in
[`DOCUMENT-CONFLICTS-DPC8001-G.md`](DOCUMENT-CONFLICTS-DPC8001-G.md).

The protocol does not explain how to enter the WiFi SSID/password or initially
discover the assigned IP address. The Windows collector now scans every active,
private IPv4 `/24` adapter network for TCP port `502`; this can find candidates
without manual IP entry, but only a successful Modbus register read can prove a
candidate is the intended DPC8001-G. Reserve confirmed device addresses in the
router so they do not change.

The zero-input Windows activation, retryable customer package, multi-NIC scan,
multi-workshop assignment boundary, background-task recovery, and remaining
manufacturer/customer confirmations are specified in
[`AUTOMATED-ONBOARDING.md`](AUTOMATED-ONBOARDING.md).

The tested network module can deliver a complete response left behind by a
previous closed TCP client. Discovery and collection therefore use fresh,
non-constant transaction IDs, drain pre-request stale data, ignore complete
frames for other transaction IDs, and reuse a persistent connection while
polling. Probes read the complete two-register U32 field at addresses `0-1`;
the real device rejects the earlier half-field quantity of one.

## Cloud email alerts

The cloud workspace now has **Settings → Email alerts**. This is cloud-only:
the Windows collector continues to upload alarm events and never sends email.
No SMTP credentials are synced to the collector or exposed to customer users.

1. Configure `DCP_SMTP_HOST`, `DCP_SMTP_PORT`, `DCP_SMTP_SECURITY` (`starttls`
   or `ssl`), `DCP_SMTP_FROM`, and optional paired `DCP_SMTP_USERNAME` /
   `DCP_SMTP_PASSWORD` in the deployment `.env`. TLS certificate verification is
   mandatory; there is no plaintext fallback. Configuration presence is not a
   connection or delivery test.
2. Rebuild/recreate the cloud API and the new `email-worker` service using the
   deployment's usual Compose procedure. The API runs the additive schema
   migration before the worker starts. Database backups include settings and
   delivery history. Source changes alone do not update an existing deployment.
3. A customer administrator saves up to 10 comma-separated recipient addresses,
   optionally enables recovery notifications, sends a test, and verifies the
   recipient inbox. Tests are limited to one request per customer per 60 seconds.
   Existing legacy manager roles retain the same authorization boundary as
   other customer settings; viewer accounts cannot read or modify recipients.

Alerts cover all cleanrooms for the current customer and honor existing collector
alarm delays. Only physical-device events starting after enablement are eligible;
enabling/re-enabling does not backfill historical events. Demo readings, transient
pending thresholds, offline status, and cloud-sync timeouts do not trigger this
version's email notifications. An event first uploaded after recovery is labelled
as a recovery, not as a live active alarm. Turning off recovery emails suppresses
that notice. Recovery cancels any still-queued active notice.

The database queue is committed with alarm ingestion, deduplicated per event,
notification type and recipient, and processed independently of sampling/upload.
SMTP delivery runs outside database transactions. A worker claims a job with a
unique token and a five-minute lease; attempts count at claim time, including
attempts interrupted by a crash. Expired claims are eligible for another worker,
and a stale worker cannot overwrite the new owner's result. An expired fifth
attempt is marked `DeliveryOutcomeUnknown` rather than retrying forever. This
does not guarantee exactly-once delivery: an interrupted or unusually slow SMTP
session may have been accepted already. Check the inbox before a manual resend.
The UI distinguishes pending from sending. Worker heartbeat readiness is shown
separately from SMTP configuration (stale after five minutes); neither proves
mailbox delivery. Recovery clears pending active notices even if recovery emails
are opted out. Settings disable/removal does not wait for network delivery.

Each recipient is sent a separate bilingual plain-text email with room, device,
metric, trigger/peak values, threshold, timestamp including timezone, and event ID.
Failed deliveries retry after 60, 120, 240 and 480 seconds (five attempts total),
then remain visibly failed. Each recipient has an independent result. The latest
30 delivery rows include room, device, metric, event ID and next retry time;
refresh to update them. Errors omit raw SMTP
responses and credentials. Failed rows are not automatically revived on a config
change: resolve the cause and use a test message to verify service recovery.

Disabling alerts or removing a recipient cancels their pending notices, but cannot
recall already-sent or in-flight mail. A server acceptance (`sent` in the API) is
not inbox delivery proof. SMTP acceptance followed by a process/database failure
can still cause a retry and duplicate delivery; stable Message-ID helps tracing
but does not promise exactly-once delivery. SMTP failure does not block reading or
alarm ingestion. A missing SMTP configuration pauses the worker without consuming
retries. Monitor `email-worker` health/logs and delivery history operationally;
email alone cannot notify about its own outage.

Local regression tests: `python -m unittest test_email_alerts` and
`node --test test_email_ui_runtime.js`. To include PostgreSQL and HTTP integration
tests, explicitly set `DCP_EMAIL_TEST_DATABASE_URL` to an **isolated database named
`emailqa`**. Tests create synthetic records and must never target production.

## Install

Use Python 3.10+ and install the serial dependency:

```powershell
py -m pip install -r requirements.txt
```

用于车间试点时，不要用双击脚本代替后台服务。请按
[`WINDOWS-DEPLOYMENT.md`](WINDOWS-DEPLOYMENT.md) 以管理员身份安装 Windows
服务；服务可在无人登录时自动运行。`start_dashboard.bat` 和隐藏 VBS 仅保留给开发调试。

## Find Serial Ports

```powershell
py .\dcp8001_collector.py --list-ports
```

## Read Realtime Values

Replace `COM3` with the actual RS485 adapter port.

```powershell
py .\dcp8001_collector.py --port COM3
```

For WiFi / Ethernet devices that expose Modbus TCP, replace the IP address
with the device address:

```powershell
py .\dcp8001_collector.py --host 192.168.1.88
```

Defaults:

- Slave address: `1`
- Modbus TCP port: `502`
- Baud rate: `9600`
- Serial: `8N1`
- Timeout: `1.0s`

## Start / Stop Sampling

```powershell
py .\dcp8001_collector.py --port COM3 --start --allow-device-write
py .\dcp8001_collector.py --port COM3 --stop --allow-device-write
py .\dcp8001_collector.py --host 192.168.1.88 --start --allow-device-write
py .\dcp8001_collector.py --host 192.168.1.88 --stop --allow-device-write
```

`--start` and `--stop` are bench-diagnostic device writes. They are rejected
unless the same invocation includes `--allow-device-write`; the customer UI and
public API do not expose device writes. Do not use them until the exact model,
firmware, and register definition have been confirmed.

## Loop Output For Dashboard

```powershell
py .\dcp8001_collector.py --port COM3 --loop 2 --json
py .\dcp8001_collector.py --host 192.168.1.88 --loop 2 --json
```

The output is one JSON object per read, suitable for forwarding to a WebSocket
or HTTP dashboard service.

Run the standalone collector only while `dashboard_server.py` is stopped.
Some embedded Modbus TCP gateways reliably support only one active collection
session; a second process can cause timeouts or connection resets. When the
dashboard is running, use the local collector page's **Test connection** action,
which shares the per-device I/O lock with background polling.

## Run The Dashboard

For a WiFi/Ethernet device, double-click:

```text
start_dashboard.bat
```

Then open:

```text
http://127.0.0.1:8787
```

On a fresh physical-device installation, the system creates an empty
`Cleanroom 1` and does not invent a device address. Open the local collector
page to scan all active private IPv4 `/24` networks or manually add a device as
a fallback. Existing saved devices are loaded unchanged. Demo mode alone
creates a sample device.

The computer running this dashboard must be on the same network as the device,
for example `192.168.2.x`.

The collector starts in physical-device mode. Simulated values must be enabled
explicitly and are never added to the cloud upload queue. For an isolated local
demo, start it with:

```powershell
$env:DCP_DEMO_MODE = "1"
py .\dashboard_server.py
```

Unset `DCP_DEMO_MODE` (or set it to `0`) before installation on a real site.

Runtime defaults:

- Device polling and alarm evaluation: every 10 seconds.
- Up to 8 devices are polled concurrently; each completed worker can schedule its
  next device without waiting for slow peers. Failed connections are closed and
  retried after 2, 4, 8, then at most 10 seconds by default (plus I/O time).
- A collector accepts up to 100 active devices by default. Override the bounded
  worker and capacity settings with `DCP_POLL_WORKERS`,
  `DCP_OFFLINE_BACKOFF_MAX`, and `DCP_MAX_ACTIVE_DEVICES`.
- Browser refresh: every 10 seconds.
- Normal historical snapshot: every 120 seconds.
- A detected device outage also saves the last successfully received sample,
  and the first recovered sample is saved immediately. Both retain their actual
  reading timestamps and use the existing durable cloud upload queue.
- Alarm activation: 5 continuous minutes outside a configured limit.
- Alarm clearing: 5 continuous minutes back inside all configured limits.

Wi-Fi recovery is automatic while the same configured endpoint becomes available
again. Each complete TCP sample has a deadline of twice `DCP_DEVICE_TIMEOUT`
plus the initial connection settle period; individual register requests also
retain their own timeout. Removed/disabled registrations release cached sockets.
Existing installations that explicitly set `DCP_OFFLINE_BACKOFF_MAX=300` must
change that override to `10` to use the faster recovery policy.

The upload queue can replay readings already saved on the collector. It cannot
reconstruct measurements that were never received while the device was offline;
the current protocol integration has no confirmed device-history replay API.
See [Wi-Fi recovery evidence and field checks](WIFI-RECOVERY-2026-09-09.md).

## Customer Login

On the first startup, the server creates the first customer account. Its random
password is printed to the server console once and is never stored as plain
text. To choose the initial credentials on a brand-new database:

```powershell
$env:DASHBOARD_INITIAL_USERNAME = "customer"
$env:DASHBOARD_INITIAL_PASSWORD = "use-a-long-unique-password"
py .\dashboard_server.py
```

Passwords use PBKDF2-HMAC-SHA256 with a unique salt. Sessions expire after 12
hours and use an HttpOnly, SameSite cookie. In an HTTPS deployment, also set
`DASHBOARD_SECURE_COOKIE=1`.

Configuration, live readings, history, alarms, statistics, and Excel exports
are all filtered by the signed-in account's `customer_id`.

The timing can be shortened for development tests only:

```powershell
$env:DCP_POLL_SECONDS = "1"
$env:DCP_RECORD_SECONDS = "10"
py .\dashboard_server.py
```

## Current Dashboard Features

- Realtime particle counts for 0.3, 0.5, 1.0, 2.5, 5.0, and 10.0 um.
- Realtime flow, temperature, humidity, dew point, wind speed, and pressure differential.
- Demo mode for local testing without hardware.
- Modbus TCP mode for WiFi / Ethernet devices.
- RS485 serial collector for COM-port testing.
- Background monitoring that continues when the browser is closed.
- Per-device readings without cleanroom averaging.
- Enterprise-admin “场所设备” workflow for creating cleanrooms and manually
  registering fixed-IP devices; new devices remain “pending collector
  confirmation” until a real device reading arrives.
- Local device inventory with add, edit, connection test, and soft-disable flows.
- Same-network discovery with multi-select device onboarding.
- Active private IPv4 adapter enumeration for dual-NIC workshop computers.
- Bounded parallel polling and per-device offline backoff for larger sites.
- Customer-editable cleanroom/device display names.
- Customer-editable alarm switches and limits for all six particle channels,
  plus temperature and humidity ranges.
- Five-minute alarm activation and clearing state machine.
- Automatic two-minute local history recording plus immediate alarm transitions.
- History table and historical trend chart.
- Time-range history filtering.
- Chinese/English interface switching on the login page, customer workspace,
  local collector, and plant wallboard. The selected language is saved in the browser and shared
  with other same-origin tabs; customer-defined workshop, device, collector,
  and account names are never machine-translated.
- Chinese/English Excel export that follows the current interface language,
  with a localized summary sheet and one worksheet per selected cleanroom.
- System logs.
- Local SQLite storage, daily online backup, disk health, and retention that
  never automatically deletes unacknowledged cloud records.

Device start/stop, Modbus connection settings, history deletion, and log
deletion are intentionally not exposed in the customer interface or public
API routes.

The administrator workflow, role boundary, API contract, state model, and
remaining production work are documented in
[`ADMIN-TOPOLOGY-DESIGN.md`](ADMIN-TOPOLOGY-DESIGN.md).

## Automated Checks

```powershell
py -m unittest -v
py -m py_compile dashboard_server.py monitoring_service.py
node --check public\app.js
```

The tests cover the five-minute alarm transitions, customer-setting permission
boundary, and Excel workbook structure.

## Windows Field-Test Package

Build the clean Windows test bundle with:

```powershell
py .\build_test_package.py
```

The output is `dist\DPC8001-G-Windows-Test.zip` plus its SHA-256 sidecar.
It contains an isolated dependency setup, collector self-check, demo launcher,
physical-device launcher, and `MANIFEST.sha256`; it intentionally excludes
runtime databases, backups, tokens, `.env`, and local virtual environments.
See [`TEST-PACKAGE-GUIDE.md`](TEST-PACKAGE-GUIDE.md) before sending it to a site.

## Windows EXE Build

The portable Windows test executable bundles Python and does not require Python
on the target computer. PyInstaller must run on Windows:

```powershell
.\build-windows-exe.ps1
```

The script first builds a diagnostic one-folder application and then the final
single-file `dist\windows\onefile\HawkHive-DPC8001-Collector.exe`. See
[`WINDOWS-EXE-TEST.md`](WINDOWS-EXE-TEST.md) for its field-test boundary.

After the single-file executable has passed Windows validation, place the tested
binary at `dist/HawkHive-DPC8001-Collector-win11-tested.exe` and build the clean
delivery archive:

```bash
python3 build_windows_exe_package.py
```

The archive contains only the executable, checksums, operating guide, and the
explicit pass/not-tested record in
[`WINDOWS-EXE-VERIFICATION.md`](WINDOWS-EXE-VERIFICATION.md).

## Production Boundary

On Windows, runtime state is stored under
`%ProgramData%\HawkHive\DCP8001`, not beside the executable. The edge token is
protected with Windows DPAPI and the installer restricts the directory ACL.
The local cache creates a daily SQLite backup and prunes only cloud-acknowledged
records beyond the configured retention period. PostgreSQL cloud backup and
independent-storage archive remain separate server-side responsibilities.

## Edge-to-Cloud Synchronization

Every local reading has a stable `record_uuid`. Unsynced rows remain in SQLite
until the cloud acknowledges the whole batch. Repeated uploads are safe because
PostgreSQL uses `record_uuid` as the idempotency key.

The default production onboarding no longer asks an administrator to create one
package per PC. Download the customer-scoped Windows ZIP once and copy it to any
number of that customer's PCs. Each PC checks in automatically; the administrator
only accepts or rejects it and may edit its name. No pairing code is required.
Approval creates a distinct site and upload credential for that machine.
One PC/site may still own devices from multiple workshops. The legacy manual
environment-variable flow below remains available for maintenance:

```powershell
$env:DCP_SITE_ID = "site-001"
$env:DCP_CLOUD_URL = "https://monitor.example.com"
$env:DCP_CLOUD_TOKEN = "token-shown-by-provision-edge"
py .\dashboard_server.py
```

Each installation persists a stable collector instance ID. Reusable installer
credentials can only request pending approval; they cannot read or upload data.
Approved PCs never share edge credentials. The cloud permits
only one recently active instance for each site/collector record; re-running a
provisioning command rotates the edge credential and releases the old instance
lease. Never copy one approved collector's token to another computer.

Upload failures stay queued in SQLite with an attempt count and last error.
Retry delay increases automatically up to five minutes. Plain HTTP is rejected
except for localhost development.

Cloud traffic uses separate channels:

- Latest values overwrite `latest_readings` every 10 seconds for the live page.
- Permanent history appends to `readings` every 2 minutes.
- Alarm events upload on activation, peak updates, and clearing.

## PostgreSQL Cloud Ingest API

For direct public IPv4 access with a trusted, automatically renewed HTTPS
certificate and no DNS dependency, follow [`DEPLOY-IP.md`](DEPLOY-IP.md).

For the low-cost Alibaba Cloud Singapore pilot, follow
[`DEPLOY-ALIYUN.md`](DEPLOY-ALIYUN.md). The production Compose stack uses Caddy
for automatic HTTPS and keeps the API and PostgreSQL ports off the public
Internet.

Install the separate cloud dependencies:

```powershell
py -m pip install -r requirements-cloud.txt
$env:DATABASE_URL = "postgresql://user:password@host:5432/dcp8001"
py -m uvicorn cloud_api:app --host 127.0.0.1 --port 8000
```

Or start PostgreSQL and the ingest API with Docker Compose after setting a
strong `POSTGRES_PASSWORD` and a separate, stable activation secret:

```powershell
$env:POSTGRES_PASSWORD = "use-a-long-random-password"
$env:DCP_ACTIVATION_SECRET = "use-another-long-random-secret"
docker compose -f docker-compose.cloud.yml up -d --build
```

Do not rotate `DCP_ACTIVATION_SECRET` while a downloaded 24-hour activation
package may still be retrying. The cloud image also needs the signed-off EXE and
its `.sha256` sidecar under `release/`; otherwise package download fails closed.

Provision a customer/site upload token inside the API container:

```powershell
docker compose -f docker-compose.cloud.yml exec ingest-api py provision_edge.py `
  --customer-id customer-001 --customer-name "Addvalue" `
  --site-id site-001 --site-name "Addvalue Singapore"
```

The raw token is shown once; PostgreSQL stores only its SHA-256 hash. Put a TLS
reverse proxy in front of port 8000 before any Internet use. The Compose file
binds the raw API to localhost intentionally.

For a new installation, provision the customer login, cleanroom, device,
thresholds, site, and edge token together:

```powershell
docker compose -f docker-compose.cloud.yml exec ingest-api py provision_customer.py `
  --customer-id customer-001 --customer-name "Addvalue" `
  --site-id site-001 --site-name "Addvalue Singapore" `
  --room-id room-001 --room-name "Cleanroom 1" `
  --device-id device-001 --device-name "Device 1" `
  --device-host 192.168.2.30 `
  --username addvalue
```

The generated customer password and edge token are printed once. Cloud login
is limited to five failures per username and client address within 15 minutes.
All cloud reads and settings updates are filtered by `customer_id`.

The cloud container serves the same HawkHive customer webpage and provides:

- Ten-second live values from PostgreSQL `latest_readings`.
- Two-minute permanent history from PostgreSQL `readings`.
- Alarm event history with active and cleared status.
- Customer-only cleanroom/device naming and alarm-limit updates. All six
  cumulative particle channels (≥0.3, ≥0.5, ≥1.0, ≥2.5, ≥5.0, and ≥10.0 µm)
  can be enabled and assigned independent upper limits. Existing installations
  keep only the ≥0.5 µm rule enabled after migration, with its prior limit.
- English Excel reports with Summary plus one worksheet per cleanroom.
- Automatic offline status when no latest value arrives for 45 seconds.

## Backups and Non-Deleting Archive

Docker Compose starts `backup-runner` with the API and PostgreSQL. It creates a
missing backup when first started, then runs every day at 02:00 Singapore time.
The first successful daily backup each month is also copied to the host-mounted
monthly directory. Configure the schedule and host path in the private `.env`:

```dotenv
DCP_BACKUP_TIME=02:00
DCP_BACKUP_TIMEZONE=Asia/Singapore
DCP_MONTHLY_BACKUP_HOST_DIR=./backups/monthly
```

For production, point `DCP_MONTHLY_BACKUP_HOST_DIR` at the separately protected
host, external drive, or network-mounted directory. The current local default
is intended for integration testing.

Common operations are available through one script:

```powershell
.\cloud-ops.ps1 Start
.\cloud-ops.ps1 Status
.\cloud-ops.ps1 Backup
.\cloud-ops.ps1 Logs
.\cloud-ops.ps1 Stop
```

To create a backup outside Docker, when
`DCP_MONTHLY_BACKUP_DIR` points to separately mounted storage, the first daily
backup each month is copied there automatically:

```powershell
$env:DATABASE_URL = "postgresql://user:password@host:5432/dcp8001"
$env:DCP_DAILY_BACKUP_DIR = "D:\DCP-Backups\Daily"
$env:DCP_MONTHLY_BACKUP_DIR = "E:\Independent-Storage\Monthly"
py .\cloud_backup.py
```

Compressed historical archives do not delete database rows:

```powershell
$env:DCP_ARCHIVE_DIR = "E:\Independent-Storage\Archive"
py .\archive_cloud_data.py --older-than-days 365
```

Both tools create SHA-256 integrity information. The Docker backup runner
automates database backups; schedule `archive_cloud_data.py` monthly on the
eventual production cloud host.

## Multi-Device Cleanroom Groups

The dashboard supports cleanroom groups. A cleanroom can contain multiple
devices, and the realtime/history graph overlays all devices in the selected
cleanroom.

Customers can edit cleanroom names, device display names, and alarm limits in
Settings. Device grouping, IP address, TCP port, and slave ID remain
administrator-managed and are not exposed in the customer interface.

Default demo grouping:

```json
[
  {
    "name": "Cleanroom 1",
    "devices": [
      { "name": "Device 1", "host": "192.168.2.30", "tcpPort": 502, "slave": 1 },
      { "name": "Device 2", "host": "192.168.2.84", "tcpPort": 502, "slave": 1 }
    ]
  }
]
```

Excel exports use the selected time range, selected devices, and interface
language. They include a localized summary worksheet plus one worksheet per
selected cleanroom.

### Remote collector diagnostics (source version 0.5.3)

After updating **both** the cloud application and the Windows collector, customer
administrators can open **场所设备 → 采集器节点 → 诊断日志**. The screen filters by
cloud receive time (1 hour to 30 days), level and device IP/name/error keyword.
It pages through older entries and exports only the currently loaded filtered
results as JSON. Device occurrence time and cloud receive time are displayed
separately so clock skew or offline replay cannot make old data look current.

- A separate worker posts up to 100 immutable log records to
  `/api/v1/edge/diagnostics/batch` every 30 seconds (2 seconds while draining).
  Failed or incomplete acknowledgements retain the same UUIDs for retry across
  process restarts. The server deduplicates by customer, site and record UUID.
  Logging network failures do not block measurement, heartbeat or other uploads.
- Logs are captured only after the collector has obtained its customer identity.
  Pre-enrollment logs and existing historical local logs are **not** bulk uploaded.
  Capture-time customer/site/instance binding prevents re-enrollment from uploading
  a previous customer's queued history under a new identity.
- The existing local operational log and diagnostic queue each retain at most
  7 days / 20,000 entries. Oldest **diagnostic** entries may expire even when not
  uploaded; measurement/history and audit records are not affected. The five-minute
  `collector_snapshot` reports `logs_discarded_total` so a gap is not mistaken for
  a complete log history. Cloud ingestion prunes its node to 30 days / 50,000 entries
  on the next batch; queries always exclude entries beyond the chosen receive window.
- First/changed device failures are logged immediately; identical repeated failures
  are summarized at five-minute intervals, and recovery logs include total failed
  attempts. Details include device ID, endpoint, elapsed time, retry delay and
  `stage`: `connect`, `initial_settle`, `send_request`, `response_header`,
  `response_body`, or `response_validated`. `request_sent=False` means no request
  was sent for that transaction; `None` means transmission outcome is uncertain;
  `True` means the OS accepted the request bytes, not that the device processed them.
- Snapshots include effective polling/history intervals, running state, version,
  applied configuration and storage state. No register writes or measurement
  frequency changes are introduced by diagnostics.
- Messages are redacted before storage/upload and again at cloud ingestion;
  recognized credentials, authorization, cookies and URL query strings are removed.
  No complete configuration, raw protocol packets or business database is uploaded.
  This is operational logging, not a general arbitrary-file upload interface.
- Cloud querying requires the existing customer's administrator role; viewer
  accounts and other tenants cannot read these logs. No cross-customer engineer
  superuser or remote command execution capability is added.

Validation: `python3 -m unittest test_collector_diagnostics test_monitoring -q`.
For real PostgreSQL API/permission tests, set `DCP_DIAGNOSTICS_TEST_DATABASE_URL`
to an isolated database whose name is exactly `diagnosticsqa`. Tests do not use
production configuration. Building or deploying the cloud does not upgrade a
previously installed Windows EXE; that requires a separate Windows build/update.

### Collector and device connection states

The cloud collector badge uses server-received heartbeats (90-second window), independently of device failures, disk warnings, and configuration errors. Device communication has three states: online after a successful real read, offline after a failed check, and unknown without valid evidence. The collector reports individual checks in its heartbeat; checks expire relative to the configured polling interval/reconnect backoff, and a stopped monitor invalidates cached results. Expired collector heartbeats turn device states unknown, not offline. Older collectors with counts only remain supported, but missing successful reads are unconfirmed rather than evidence of a failed connection. Measurement freshness remains a separate status. Local topology indicates the running local collector process separately from its monitoring service.

This requires deploying the updated cloud and installing the updated collector to obtain individual device checks; existing Windows 0.3.0 installations do not gain this capability from a cloud-only update.

### Windows automatic updates (0.6.0)

Overwrite installation preserves data within the current installed layout. It
does not automatically discover or import an arbitrary older portable data
directory. Confirm the old runtime directory before a cross-layout upgrade;
starting with an empty data directory can register a separate collector.
The source-directory migration helper preserves committed SQLite WAL records,
settings, and the installation identity together. It refuses conflicting
identities and does not import another database into a destination that already
has settings. These checks do not replace acceptance using an actual 0.3 package
and a disposable copy of its runtime data.

Migration regression: `python -m unittest test_runtime_migration -v`.

Cloud Devices now shows **collector connection**, **device communication**, and **automatic update status** separately. Heartbeats report the installed version, update stage, most recent check, failure/rollback, and supported update capability. Old collectors are explicitly labelled as requiring a one-time upgrade.

Install 0.6.0 once using the customer Windows package. The machine-start task runs a stable supervisor from `Program Files`, which starts exactly one real collector worker. Windows Job Object containment prevents orphan workers when the task is stopped. Collection configuration, instance identity, SQLite history/WAL and the pending-upload queue remain in the same `ProgramData` directory. Portable/demo/source-service installations do not claim automatic-update support.

After startup the supervisor checks the configured HTTPS cloud after one minute, then hourly. Downloads run while collection continues. A release must have a trusted Ed25519 signature, supported protocol, unexpired metadata, a higher semantic version, and matching SHA-256 and byte length; cross-origin redirects and plaintext URLs are rejected. Low free disk space postpones the update without deleting data. The worker stops cleanly before a staged EXE replaces it. A separate random startup nonce and matching application version must pass local health checks for 30 seconds within a 120-second startup window. If startup fails, the previous EXE is restored. An interrupted switch is recovered before starting a worker at next boot; a failed version is blocked until a newer release. The last previous EXE is retained. Restart briefly pauses polling; no claim of uninterrupted sampling is made.

Release publication is an operations action, not a customer-supplied executable or remote-command interface:

1. Build on Windows with `build-windows-exe.ps1`, then verify real installed startup, update, rollback, single-instance behavior and retained data on a Windows test machine.
2. All automatically distributed releases must retain backward compatibility with the previous SQLite schema and updater protocol. Schema-destructive migrations are not eligible for this update channel. The stable bootstrap itself is versioned separately; incompatible bootstrap/protocol changes require an explicitly planned bootstrap upgrade.
3. Run `python publish_collector_update.py --exe release/HawkHive-DPC8001-Collector.exe --version 0.6.0 --key /secure/offline/update-signing-2026.pem --output release/updates`. The key must match the public key embedded in `update_protocol.py`; never put the private signing key in source, installers, the cloud image or customer packages.
4. Publish the immutable digest-named EXE first and `stable.json` last. The cloud reads `release/updates` (override with `DCP_COLLECTOR_UPDATE_DIR`). Retain prior immutable EXEs for in-flight downloads. Until a Windows-tested signed release is published, `/api/v1/collector-updates/stable` reports `available:false` and collectors keep running their current version. Re-sign the release before its maximum 90-day expiry if it remains the current release.

The first signing key was generated at `~/.config/hawkhive/update-signing-2026.pem` on the release operator's computer; only its public verification key is included in this repository. Back it up using the operator's normal credential storage. Automatic distribution must remain unavailable until actual Windows acceptance is complete; Python and simulated process tests alone do not verify Windows file locking, Task Scheduler or PyInstaller child processes.

Implementation references: [PyInstaller process restart guidance](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html) and [Ed25519 signing and verification](https://cryptography.io/en/latest/hazmat/primitives/asymmetric/ed25519/).
