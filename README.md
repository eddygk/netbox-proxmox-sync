# netbox-proxmox-sync

Sync [Proxmox VE](https://www.proxmox.com/) and [UniFi](https://ui.com/) into
[NetBox](https://netbox.dev/) as a rich, idempotent inventory model. A lightweight,
self-hostable alternative to the commercial NetBox Labs Proxmox integration —
no NetBox Assurance / Diode / Orb required.

## What it models

**From Proxmox (`sync_v2.py`):**
- VirtualMachines (QEMU + LXC) — auto-created and kept in sync (status, vCPU, memory)
- VM interfaces + MAC addresses, from `net*` config
- IP addresses assigned to the right interface (LXC `ip=`, QEMU via guest agent)
- VLANs from interface `tag=`
- Virtual disks (with storage pool)
- Platform / OS detection (QEMU guest agent, LXC `ostype`)
- Proxmox tags imported as NetBox tags; custom fields (`proxmox_vmid`, node, type, …)
- The Proxmox node as a DCIM device: interfaces, IPs, CPU/memory, Proxmox version
- Optional iDRAC/Redfish hardware enrichment (`--idrac`): serial, firmware, NIC MACs
- **Removal lifecycle**: VMs gone from Proxmox go `offline → decommissioning → deleted`
  on configurable age thresholds

**From UniFi (`fetch_unifi_and_populate.py` → `populate_network.py`):**
- UniFi devices as DCIM devices, clients as IPs
- Wireless LANs (SSIDs) with auth type + VLAN
- Switch ports + point-to-point cabling (host NIC / AP / inter-switch uplinks)
- MAC correlation: annotates NetBox interfaces with their UniFi connection facts
  (wired switch-port / wireless SSID, observed VLAN, uplink) via custom fields

Everything is **idempotent** (safe to run on a schedule) and supports `--dry-run`.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in your tokens / hosts
# optionally: cp prefixes.example.json prefixes.json  (your VLAN/subnet map)

python3 sync_v2.py --dry-run        # preview Proxmox → NetBox
python3 sync_v2.py                  # apply
python3 fetch_unifi_and_populate.py # UniFi → NetBox (optional)
```

Run hourly via cron:

```cron
0 *  * * * /usr/bin/env python3 /opt/netbox-sync/sync_v2.py >> /var/log/netbox-sync.log 2>&1
15 * * * * /usr/bin/python3 /opt/netbox-sync/fetch_unifi_and_populate.py >> /var/log/netbox-unifi.log 2>&1
```

## Configuration

All config is via environment variables (see `.env.example`). Nothing about your
network is hardcoded. Key knobs:

| Var | Purpose |
|-----|---------|
| `PROXMOX_HOST` / `PROXMOX_TOKEN_ID` / `PROXMOX_TOKEN_SECRET` | Proxmox API |
| `NETBOX_URL` / `NETBOX_TOKEN` | NetBox API |
| `UNIFI_HOST` / `UNIFI_API_KEY` | UniFi (optional) |
| `IDRAC_HOST` + `~/.idrac-credentials` | iDRAC Redfish (optional, `--idrac`) |
| `PREFIXES_FILE` | JSON VLAN/subnet map (else built-in example) |
| `REAP_OFFLINE_DAYS` / `REAP_DECOM_DAYS` / `REAP_DELETE_DAYS` | removal lifecycle |

### Useful flags
- `--dry-run` — log intended writes, change nothing
- `--phases 0,1,2,…` — run only specific phases
- `--only-vmid N` — limit to one guest
- `--idrac` — enable live iDRAC/Redfish node enrichment
- `--no-reap` — disable the removal lifecycle

## Requirements

- NetBox 4.5+ (uses `dcim.mac-addresses`, `virtual-disks`, wireless LANs)
- Proxmox VE 7+ with an API token
- Python 3.10+

## Security

`.env`, `*.json` data snapshots, and credential files are gitignored. Do not commit
real tokens, MACs, or topology — keep them in `.env` / `prefixes.json`.

After cloning, install the pre-commit secret guard (blocks staged credentials,
credential files, and bare MAC addresses before they can be committed):

```bash
scripts/install-hooks.sh
```

GitHub secret-scanning push protection is also enabled on the upstream repo as a
server-side backstop.

## License

MIT
