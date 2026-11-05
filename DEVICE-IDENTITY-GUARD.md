# Explicit LAN device identity (collector 0.6.12)

A configured IP endpoint is not a physical-device identity. This change adds
explicit per-customer/site/device MAC bindings; no first-read enrollment or
automatic reassignment is allowed. Unconfigured sites retain their existing
behavior until an operator supplies a reviewed policy.

For guarded Windows devices, ResolveIpNetEntry2 actively resolves the selected
on-link neighbor before and after every measurement. A changed MAC/interface,
a mismatched approved MAC, missing binding, routing through a gateway, timeout,
or configuration change withholds the measurement from ordinary history,
latest-value uploads and alarm evaluation. Successful but unverified reads are
retained separately in SQLite `identity_unverified_samples` at the normal
history cadence; they are never auto-promoted, uploaded as business readings,
or automatically deleted. If the pre-read identity cannot be resolved, no
measurement is attempted. Existing historical readings are not rewritten.

The resolver has bounded daemon concurrency and timeouts. Slow identity checks
must not exhaust resources or let a late cached result authorize another read.
An offline status remains visible even when its measurement is withheld.

## Deployment and binding

1. Preserve the actual running cloud/collector release and existing configuration.
2. Apply the additive `identity_schema.sql` through cloud schema initialization;
   ship the cloud's updated `edge_configuration_payload` and the Windows artifact.
3. Confirm the target collector heartbeat reports 0.6.12 before activating policy.
   Older collectors ignore identity fields and must not be described as protected.
4. Use `bind_device_identity.py --customer ... --site ... --device ... --mac ...
   --evidence ...` for a read-only preview; `--apply` performs the scoped binding,
   audit event and site revision increment atomically. The operator must review
   physical inventory or established historical identity evidence. Omit `--mac`
   to hold unverified reads pending a binding; this reduces published availability
   but preserves successful raw measurements separately.
5. Observe guarded recovery and rejection on the real collector. Do not infer
   field acceptance from a build or simulated resolver. Do not remap history or
   automatically recover a device solely because an IP answers Modbus.

MAC guards against accidental address reuse on a directly attached LAN. It is
not cryptographic authentication and does not defend against MAC spoofing,
proxy ARP, shared gateways, or physical replacement behind an unchanged adapter.
These cases need a stable sensor serial/protocol identity or a verified network
inventory. A fail-closed policy cannot reconstruct measurements never collected.
The existing signed updater can roll back to an older collector on failed start;
policy enforcement therefore requires verifying the running version and must
not be assumed after rollback. The current cloud ingest schema does not attest
hardware identity independently of the collector.

API references:
- https://learn.microsoft.com/en-us/windows/win32/api/netioapi/nf-netioapi-resolveipnetentry2
- https://learn.microsoft.com/en-us/windows/win32/api/netioapi/ns-netioapi-mib_ipnet_row2

Tests exercise address reuse, mid-sample change, query timeout, identity loss,
raw retention failure, recovery, preserved history, cloud revision races,
legacy configuration omission, tenant/site boundaries and operator audit.
Windows CI additionally checks the native ABI and builds/executes the packaged
collector's existing upgrade/rollback acceptance tests.
