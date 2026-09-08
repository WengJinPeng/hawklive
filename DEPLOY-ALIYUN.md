# HawkHive Alibaba Cloud Pilot Deployment

This guide deploys the current FastAPI, PostgreSQL, Caddy, and backup stack to
an Alibaba Cloud International Simple Application Server in Singapore.

## 1. Buy the minimum suitable server

- Product: Simple Application Server
- Region: Singapore
- Image: Ubuntu 24.04 LTS (Ubuntu 22.04 LTS is also supported)
- Pilot: 2 vCPU, 1 GiB RAM, 30 GiB system disk, one public IPv4 address
- Term: one year only for the first purchase

Record the first-year total and the normal renewal total before paying. Do not
purchase a paid SSL certificate, managed database, WAF, or extra data disk for
the pilot.

Enable MFA on the Alibaba Cloud account. In the server firewall, allow TCP 80
and 443 from the Internet. Allow TCP 22 only from the administrator's current
public IP whenever practical. Do not expose ports 502, 5432, or 8000.

## 2. Create the temporary hostname

If a free DNS service is not reachable by your users, use a fixed public IP
with a trusted HTTPS certificate instead; see `DEPLOY-IP.md`. A hostname is
not required in that mode.

Create a free DuckDNS subdomain and point it to the server's public IPv4
address. The result should look like `hawkhive-monitor.duckdns.org`. Keep the
DuckDNS account token private; it is not needed by this project when the
server's public IP remains fixed.

## 3. Prepare the 1 GiB server and install Docker

Connect to the server with Alibaba Cloud Workbench or SSH. Follow Docker's
official Ubuntu installation instructions, then verify:

```bash
docker --version
docker compose version
```

Upload the `dcp8001-dev` directory to `/opt/hawkhive`. Do not upload the local
`.env`, `backups`, `__pycache__`, or SQLite database files.

The 1 GiB pilot requires a 2 GiB swap file before the containers are started:

```bash
cd /opt/hawkhive
sudo bash prepare-ubuntu-1gb.sh
```

The production Compose file also uses conservative PostgreSQL memory settings,
rotates container logs, and keeps 14 days of local daily database backups.
Monthly copies are retained until they are moved to independent storage.

## 4. Create production settings

On the server:

```bash
cd /opt/hawkhive
cp .env.example .env
openssl rand -hex 32
openssl rand -hex 32
nano .env
```

Put the two different generated values in `POSTGRES_PASSWORD` and
`DCP_ACTIVATION_SECRET`. Keep the activation secret stable across redeployments.
Set `DASHBOARD_DOMAIN` to
the DuckDNS hostname without `https://`. Keep `DASHBOARD_SECURE_COOKIE=1`.
Never send or commit the `.env` file.

Before building the cloud image, place the approved Windows artifact and its
matching digest at `release/HawkHive-DPC8001-Collector.exe` and
`release/HawkHive-DPC8001-Collector.exe.sha256`. The Windows build script does
this automatically. Missing or mismatched artifacts make package download fail
closed rather than distribute an unverified executable.

## 5. Start and verify the cloud platform

```bash
cd /opt/hawkhive
bash cloud-ops.sh start
bash cloud-ops.sh status
curl "https://${DASHBOARD_DOMAIN}/api/v1/health"
```

Caddy obtains and renews the public HTTPS certificate automatically. The API
and PostgreSQL containers do not publish their ports to the Internet.

## 6. Provision the first customer and edge token

Replace the example identifiers and names, then run:

```bash
docker compose -f docker-compose.cloud.yml exec ingest-api python provision_customer.py \
  --customer-id customer-001 --customer-name "Customer 1" \
  --site-id site-001 --site-name "Customer 1 Site" \
  --room-id room-001 --room-name "Cleanroom 1" \
  --device-id device-001 --device-name "Device 1" \
  --device-host 192.168.2.30 \
  --username customer1
```

The customer password and device upload token are displayed once. Save them in
a password manager and deliver the customer password through a separate secure
channel. Do not put either value in this repository.

## 7. Connect the on-site collector

On the on-site Windows computer, set these values before starting
`dashboard_server.py`:

```powershell
$env:DCP_DEMO_MODE = "0"
$env:DCP_SITE_ID = "site-001"
$env:DCP_CLOUD_URL = "https://hawkhive-monitor.duckdns.org"
$env:DCP_CLOUD_TOKEN = "the-one-time-edge-token"
py .\dashboard_server.py
```

Replace the URL and token with the actual values. The edge computer initiates
outbound HTTPS connections; never forward Modbus TCP port 502 from the customer
site to the Internet.

## 8. Pilot acceptance checks

- Sign in from a phone using mobile data, not the customer's Wi-Fi.
- Confirm live values update and historical rows appear.
- Confirm a second customer account cannot see the first customer's records.
- Disconnect the site Internet connection, collect data, reconnect it, and
  verify the queued history uploads exactly once.
- Run `bash cloud-ops.sh backup`, copy a backup off the server, and perform a
  restore rehearsal before relying on the platform.
- Configure a monthly reminder to download an encrypted backup to independent
  storage until automatic off-site backup is added.
