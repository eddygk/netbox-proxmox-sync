#!/usr/bin/env python3
"""
Proxmox -> NetBox sync, v2 (richer inventory model).

Runs ALONGSIDE the lightweight v1 sync.py. It is idempotent and supports
--dry-run. It does NOT delete anything and does NOT modify v1 or its cron.

Builds toward the official NetBox Labs Proxmox object mapping, scoped to a
single Proxmox node and a single NetBox site (set via env):

  Phase 0  custom fields (proxmox_* / idrac_*)
  Phase 1  VM interfaces + MACs       (from net* config)
  Phase 2  IP assignment to interfaces (LXC ip=, QEMU agent MAC-correlated)
  Phase 3  VLAN tags                  (net* tag=NN -> untagged_vlan)
  Phase 4  node1 DCIM interfaces      (/nodes/<node>/network)
  Phase 4b iDRAC enrichment           (Track A static seed; Track B --idrac live Redfish)
  Phase 5  virtual disks              (scsi*/virtio*/sata*/ide*/rootfs/mp*)
  Phase 6  platform / OS detection

Reuses the proven idempotent NetBoxClient from populate_network.py and the
ProxmoxClient/load_env from sync.py (both in the same directory; imported, not
modified).

Usage:
  sync_v2.py [--dry-run] [--phases 0,1,2,...] [--only-vmid N] [--idrac]
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import ipaddress
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable

import requests
import urllib3

# v1 modules live in the same directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from populate_network import NetBoxClient, compare_value, slugify  # noqa: E402
from sync import ProxmoxClient, guest_payload, load_env, normalize_ip  # noqa: E402

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

LOG = logging.getLogger("netbox-sync-v2")

DEFAULT_ENV_FILE = "/opt/netbox-sync/.env"
IDRAC_CRED_FILE = os.path.expanduser("~/.idrac-credentials")

ALL_PHASES = ["0", "1", "2", "3", "4", "5", "6"]

# Content-type strings (app_label.model) for custom-field scoping.
CT_VM = "virtualization.virtualmachine"
CT_VDISK = "virtualization.virtualdisk"
CT_DEVICE = "dcim.device"

# Optional static facts to seed onto the Proxmox node's DCIM device (Track A),
# for hardware data the Proxmox API can't provide (service tag, iDRAC IP, BIOS/
# firmware versions). Populate via env if you know them; otherwise left blank and
# Track B (live iDRAC/Redfish, --idrac) fills them in. Nothing here is required.
NODE_STATIC = {k: v for k, v in {
    "serial": os.environ.get("NODE_SERIAL", ""),
    "idrac_service_tag": os.environ.get("NODE_SERVICE_TAG", ""),
    "idrac_ip": os.environ.get("IDRAC_HOST", ""),
    "bios_version": os.environ.get("NODE_BIOS_VERSION", ""),
    "idrac_firmware": os.environ.get("NODE_IDRAC_FIRMWARE", ""),
}.items() if v}

# Real data disks only; efidisk*/tpmstate* are tiny firmware-state volumes (noise).
DISK_PREFIXES = ("scsi", "virtio", "sata", "ide", "rootfs", "mp")


class Stats:
    """Per-object-type created/updated/unchanged tally for the run summary."""

    def __init__(self) -> None:
        self.counts: dict[str, dict[str, int]] = {}

    def bump(self, kind: str, result: str) -> None:
        self.counts.setdefault(kind, {"created": 0, "updated": 0, "unchanged": 0, "skipped": 0})
        self.counts[kind][result] = self.counts[kind].get(result, 0) + 1

    def render(self) -> str:
        lines = ["Summary (created/updated/unchanged):"]
        for kind in sorted(self.counts):
            c = self.counts[kind]
            lines.append(
                f"  {kind:24s} c={c['created']:<4d} u={c['updated']:<4d} "
                f"unchanged={c['unchanged']:<4d} skipped={c.get('skipped', 0)}"
            )
        return "\n".join(lines)


# ----------------------------------------------------------------------------
# Proxmox config DSL parsers
# ----------------------------------------------------------------------------

MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")


def _kv(value: str) -> dict[str, str]:
    """Parse the comma-separated key=value Proxmox option string into a dict.

    The first token may be positional (e.g. QEMU `virtio=MAC` or a disk
    `pool:volume`); callers handle that separately. Returns lowercased keys.
    """
    out: dict[str, str] = {}
    for part in str(value).split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip().lower()] = v.strip()
    return out


def parse_net(value: str) -> dict[str, Any]:
    """Parse a QEMU or LXC net* config value.

    QEMU: ``virtio=AA:BB:CC:00:11:22,bridge=vmbr0,tag=20``
    LXC:  ``name=eth0,bridge=vmbr1,hwaddr=AA:..,ip=10.0.20.11/24,tag=20,type=veth``
    """
    kv = _kv(value)
    mac = None
    model = None
    # QEMU encodes model=MAC as the first token (e.g. virtio=FA:..).
    first = str(value).split(",", 1)[0].strip()
    if "=" in first:
        fk, fv = first.split("=", 1)
        if MAC_RE.match(fv.strip()):
            model = fk.strip().lower()
            mac = fv.strip().upper()
    # LXC encodes hwaddr=.
    if not mac and kv.get("hwaddr") and MAC_RE.match(kv["hwaddr"]):
        mac = kv["hwaddr"].upper()
    tag = kv.get("tag")
    return {
        "name": kv.get("name"),           # LXC interface name (e.g. eth0); None for QEMU
        "mac": mac,
        "model": model,                    # QEMU nic model (virtio/e1000/...)
        "bridge": kv.get("bridge"),
        "ip": None if (kv.get("ip", "").lower() in ("", "dhcp")) else kv.get("ip"),
        "ip6": None if (kv.get("ip6", "").lower() in ("", "dhcp", "auto")) else kv.get("ip6"),
        "gw": kv.get("gw"),
        "tag": int(tag) if (tag and tag.isdigit()) else None,
    }


def _size_to_mb(size: str | None) -> int | None:
    if not size:
        return None
    m = re.match(r"^\s*([\d.]+)\s*([KMGTP]?)\s*$", str(size), re.IGNORECASE)
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2).upper()
    factor = {"": 1 / 1024, "K": 1 / 1024 / 1024, "M": 1, "G": 1024,
              "T": 1024 * 1024, "P": 1024 * 1024 * 1024}.get(unit, 1)
    return int(round(num * factor)) or 1


def parse_disk(key: str, value: str) -> dict[str, Any] | None:
    """Parse a disk config entry into {name, storage_pool, size_mb}.

    Examples:
      virtio0 = RAID0-ZFS:vm-100-disk-1,aio=threads,size=32G
      rootfs  = RAID0-ZFS:subvol-103-disk-0,size=2G
      ide2    = local:iso/foo.iso,media=cdrom   -> skipped (cdrom)
    """
    sval = str(value)
    kv = _kv(sval)
    if kv.get("media") == "cdrom":
        return None
    first = sval.split(",", 1)[0].strip()
    if first.lower() in ("none", "") or first.lower().startswith("none"):
        return None
    storage_pool = None
    volume = first
    if ":" in first:
        storage_pool, volume = first.split(":", 1)
    # Skip ISO/cdrom volumes that slipped past media= check.
    if "iso/" in volume or volume.lower().endswith(".iso"):
        return None
    return {
        "name": key,                       # e.g. virtio0 / rootfs / mp0
        "storage_pool": storage_pool,
        "volume": volume,
        "size_mb": _size_to_mb(kv.get("size")),
    }


def iter_net_entries(cfg_data: dict) -> Iterable[tuple[str, dict[str, Any]]]:
    for key in sorted(k for k in cfg_data if re.fullmatch(r"net\d+", k)):
        yield key, parse_net(cfg_data[key])


def iter_disk_entries(cfg_data: dict) -> Iterable[dict[str, Any]]:
    for key in sorted(cfg_data):
        if not any(key == p or re.fullmatch(rf"{p}\d+", key) for p in DISK_PREFIXES):
            continue
        parsed = parse_disk(key, cfg_data[key])
        if parsed:
            yield parsed


# ----------------------------------------------------------------------------
# iDRAC / Redfish (Track B)
# ----------------------------------------------------------------------------

class IDRACClient:
    """Minimal Redfish reader for the node1 iDRAC. Never raises to the caller of
    its public methods on network/auth failure -- returns None so the sync can
    continue (the BMC must never be able to abort the inventory run)."""

    def __init__(self, host: str, user: str, password: str) -> None:
        self.base = f"https://{host}/redfish/v1"
        self.session = requests.Session()
        self.session.verify = False
        self.session.auth = (user, password)

    @classmethod
    def from_env(cls) -> "IDRACClient | None":
        host = os.environ.get("IDRAC_HOST", "").strip()
        if not host:
            LOG.warning("iDRAC: IDRAC_HOST not set in env; skipping Track B")
            return None
        user = os.environ.get("IDRAC_USER", "").strip()
        password = os.environ.get("IDRAC_PASS", "").strip()
        if not user and os.path.exists(IDRAC_CRED_FILE):
            try:
                raw = Path(IDRAC_CRED_FILE).read_text().strip()
                if ":" in raw:
                    user, password = raw.split(":", 1)
            except OSError as exc:
                LOG.warning("iDRAC: could not read %s: %s", IDRAC_CRED_FILE, exc)
        if not user or not password:
            LOG.warning("iDRAC: no credentials (env or %s); skipping Track B", IDRAC_CRED_FILE)
            return None
        return cls(host, user, password)

    def _get(self, path: str) -> dict | None:
        try:
            r = self.session.get(f"{self.base}{path}", timeout=15)
            if not r.ok:
                LOG.warning("iDRAC GET %s -> %s", path, r.status_code)
                return None
            return r.json()
        except requests.RequestException as exc:
            LOG.warning("iDRAC GET %s failed: %s", path, exc)
            return None

    def system(self) -> dict | None:
        return self._get("/Systems/System.Embedded.1")

    def ethernet_interfaces(self) -> list[dict]:
        idx = self._get("/Systems/System.Embedded.1/EthernetInterfaces")
        if not idx:
            return []
        out = []
        for m in idx.get("Members", []):
            oid = m.get("@odata.id")
            if not oid:
                continue
            detail = self._get(oid.replace("/redfish/v1", "", 1))
            if detail:
                out.append(detail)
        return out


# ----------------------------------------------------------------------------
# Orchestrator
# ----------------------------------------------------------------------------

class SyncV2:
    def __init__(self, px: ProxmoxClient, nb: NetBoxClient, stats: Stats,
                 dry_run: bool, idrac_overwrite: bool) -> None:
        self.px = px
        self.nb = nb
        self.stats = stats
        self.dry_run = dry_run
        self.idrac_overwrite = idrac_overwrite
        self.cluster_id = int(os.environ.get("CLUSTER_ID", "1"))
        self.site_id = int(os.environ.get("SITE_ID", "1"))
        self.node_name = os.environ.get("PROXMOX_NODE", "node1")
        # Caches (populated lazily).
        self._cf_names: set[str] | None = None
        self._vlans_by_vid: dict[int, dict] = {}
        self._platforms: dict[str, dict] = {}
        self._tags_by_slug: dict[str, dict] | None = None
        self.run_ts = datetime.now(timezone.utc).isoformat()

    # -- generic upsert helper -------------------------------------------------

    def _upsert(self, kind: str, endpoint: str, lookup: dict, payload: dict,
                compare_fields: Iterable[str]) -> dict | None:
        """Find by `lookup` params; create if absent, else PATCH changed fields.
        Returns the resulting object (or a dry-run stub)."""
        found = self.nb.list_all(endpoint, **lookup)
        if not found:
            created = self.nb.post(endpoint, payload)
            self.stats.bump(kind, "created")
            return created
        obj = found[0]
        diff = {}
        for f in compare_fields:
            if compare_value(obj.get(f)) != compare_value(payload.get(f)):
                diff[f] = payload[f]
        if diff:
            updated = self.nb.patch(f"{endpoint}{obj['id']}/", diff) or {**obj, **diff}
            self.stats.bump(kind, "updated")
            return updated
        self.stats.bump(kind, "unchanged")
        return obj

    # -- Phase 0: custom fields ------------------------------------------------

    def ensure_custom_field(self, name: str, cf_type: str, object_types: list[str],
                            label: str, choices: list[str] | None = None) -> None:
        if self._cf_names is None:
            self._cf_names = {c["name"] for c in self.nb.list_all("extras/custom-fields/")}
        if name in self._cf_names:
            self.stats.bump("custom-field", "unchanged")
            return
        payload: dict[str, Any] = {
            "name": name, "label": label, "type": cf_type,
            "object_types": object_types, "required": False,
        }
        if choices is not None:
            # NetBox 4.x: select fields need a choice set.
            cs_name = f"{name}_choices"
            existing_cs = self.nb.list_all("extras/custom-field-choice-sets/", name=cs_name)
            if existing_cs:
                cs_id = existing_cs[0]["id"]
            else:
                cs = self.nb.post("extras/custom-field-choice-sets/",
                                  {"name": cs_name, "extra_choices": [[c, c] for c in choices]})
                cs_id = cs.get("id")
            payload["choice_set"] = cs_id
        self.nb.post("extras/custom-fields/", payload)
        self._cf_names.add(name)
        self.stats.bump("custom-field", "created")

    def phase0_custom_fields(self) -> None:
        LOG.info("Phase 0: custom fields")
        self.ensure_custom_field("proxmox_vmid", "integer", [CT_VM], "Proxmox VMID")
        self.ensure_custom_field("proxmox_node", "text", [CT_VM], "Proxmox Node")
        self.ensure_custom_field("proxmox_vm_type", "select", [CT_VM], "Proxmox VM Type",
                                 choices=["qemu", "lxc"])
        self.ensure_custom_field("proxmox_ha_state", "text", [CT_VM], "Proxmox HA State")
        self.ensure_custom_field("proxmox_storage_pool", "text", [CT_VDISK], "Proxmox Storage Pool")
        self.ensure_custom_field("idrac_service_tag", "text", [CT_DEVICE], "iDRAC Service Tag")
        self.ensure_custom_field("idrac_ip", "text", [CT_DEVICE], "iDRAC IP")
        self.ensure_custom_field("bios_version", "text", [CT_DEVICE], "BIOS Version")
        self.ensure_custom_field("idrac_firmware", "text", [CT_DEVICE], "iDRAC Firmware")
        self.ensure_custom_field("proxmox_cpu_count", "integer", [CT_DEVICE], "Proxmox CPU Count")
        self.ensure_custom_field("proxmox_memory_mb", "integer", [CT_DEVICE], "Proxmox Memory (MB)")
        self.ensure_custom_field("proxmox_last_seen", "datetime", [CT_VM], "Proxmox Last Seen")

    # -- VM lookup -------------------------------------------------------------

    def get_vm(self, name: str) -> dict | None:
        found = self.nb.list_all("virtualization/virtual-machines/", name=name)
        return found[0] if found else None

    def create_vm(self, item: dict, cfg_data: dict, gtype: str) -> dict | None:
        """Create a NetBox VirtualMachine for a Proxmox guest not yet present.
        Reuses v1's guest_payload but omits `disk` (Phase 5 VirtualDisks own the
        aggregate; setting it here would conflict once disks exist)."""
        payload = guest_payload(item, cfg_data, gtype)
        payload.pop("disk", None)
        payload["cluster"] = self.cluster_id
        payload["site"] = self.site_id
        created = self.nb.post("virtualization/virtual-machines/", payload)
        self.stats.bump("vm", "created")
        return created

    def refresh_vm_core(self, vm: dict, item: dict, gtype: str) -> None:
        """Keep core VM fields (status, vcpus, memory) in sync with Proxmox for
        EXISTING VMs. (v1 used to do this; it's retired, so v2 owns it now.)
        `disk` is intentionally excluded -- Phase 5 VirtualDisks own the aggregate."""
        payload = guest_payload(item, self.px.get_config(gtype, int(item["vmid"])), gtype)
        diff = {}
        for f in ("status", "vcpus", "memory"):
            cur = compare_value(vm.get(f))
            want = payload[f]
            # vcpus reads back as float (2.0) vs Proxmox int (2) -> compare numerically.
            if f == "vcpus":
                if float(cur or 0) != float(want or 0):
                    diff[f] = want
            elif str(cur) != str(want):
                diff[f] = want
        if diff:
            self.nb.patch(f"virtualization/virtual-machines/{vm['id']}/", diff)
            self.stats.bump("vm", "updated")
        else:
            self.stats.bump("vm", "unchanged")

    # -- Phase 1: VM interfaces + MACs ----------------------------------------

    def ensure_vminterface(self, vm: dict, name: str, bridge: str | None) -> dict | None:
        # NetBox VMInterface.bridge references another interface, not the Proxmox
        # host bridge -- record the Proxmox bridge in the description instead.
        payload = {"virtual_machine": vm["id"], "name": name, "enabled": True}
        if bridge:
            payload["description"] = f"Proxmox bridge: {bridge}"
        return self._upsert("vm-interface", "virtualization/interfaces/",
                            {"virtual_machine_id": vm["id"], "name": name},
                            payload, compare_fields=["enabled", "description"])

    def ensure_mac(self, interface: dict, mac: str) -> dict | None:
        mac = mac.upper()
        found = self.nb.list_all("dcim/mac-addresses/", mac_address=mac)
        existing = None
        for m in found:
            ao = m.get("assigned_object")
            if ao and compare_value(m.get("assigned_object_id") or (ao.get("id") if isinstance(ao, dict) else None)) == interface["id"]:
                existing = m
                break
        if not existing and found:
            existing = found[0]
        if existing is None:
            macobj = self.nb.post("dcim/mac-addresses/", {
                "mac_address": mac,
                "assigned_object_type": "virtualization.vminterface",
                "assigned_object_id": interface["id"],
            })
            self.stats.bump("mac-address", "created")
        else:
            macobj = existing
            self.stats.bump("mac-address", "unchanged")
        # Ensure it's the interface's primary MAC.
        cur_primary = compare_value(interface.get("primary_mac_address"))
        if macobj and cur_primary != macobj.get("id"):
            self.nb.patch(f"virtualization/interfaces/{interface['id']}/",
                          {"primary_mac_address": macobj["id"]})
        return macobj

    # -- Phase 3: VLAN ---------------------------------------------------------

    def get_vlan(self, vid: int) -> dict | None:
        if not self._vlans_by_vid:
            for v in self.nb.list_all("ipam/vlans/"):
                if v.get("vid") is not None:
                    self._vlans_by_vid[v["vid"]] = v
        if vid in self._vlans_by_vid:
            return self._vlans_by_vid[vid]
        created = self.nb.post("ipam/vlans/", {
            "vid": vid, "name": f"VLAN {vid}", "status": "active", "site": self.site_id,
        })
        self.stats.bump("vlan", "created")
        if created:
            self._vlans_by_vid[vid] = created
        return created

    def set_interface_vlan(self, interface: dict, vid: int) -> None:
        vlan = self.get_vlan(vid)
        if not vlan:
            return
        diff = {}
        if compare_value(interface.get("untagged_vlan")) != vlan["id"]:
            diff["untagged_vlan"] = vlan["id"]
        if interface.get("mode") not in ("access", {"value": "access"}):
            cur_mode = compare_value(interface.get("mode"))
            if cur_mode != "access":
                diff["mode"] = "access"
        if diff:
            self.nb.patch(f"virtualization/interfaces/{interface['id']}/", diff)
            self.stats.bump("vm-interface-vlan", "updated")
        else:
            self.stats.bump("vm-interface-vlan", "unchanged")

    # -- Phase 2: IP assignment ------------------------------------------------

    def assign_ip(self, address: str, interface: dict, vm_name: str) -> dict | None:
        try:
            ipaddress.ip_interface(address)
        except ValueError:
            return None
        found = self.nb.list_all("ipam/ip-addresses/", address=address)
        target_type = "virtualization.vminterface"
        if not found:
            created = self.nb.post("ipam/ip-addresses/", {
                "address": address, "status": "active",
                "assigned_object_type": target_type, "assigned_object_id": interface["id"],
                "description": f"Proxmox guest: {vm_name}",
            })
            self.stats.bump("ip-address", "created")
            return created
        ip = found[0]
        cur_aid = compare_value(ip.get("assigned_object_id") or
                                (ip.get("assigned_object", {}) or {}).get("id"))
        if cur_aid != interface["id"]:
            updated = self.nb.patch(f"ipam/ip-addresses/{ip['id']}/", {
                "assigned_object_type": target_type, "assigned_object_id": interface["id"],
            }) or ip
            self.stats.bump("ip-address", "updated")  # re-linked a floating IP
            return updated
        self.stats.bump("ip-address", "unchanged")
        return ip

    def set_primary_ip(self, vm: dict, ip_obj: dict | None) -> None:
        if not ip_obj:
            return
        addr = ip_obj.get("address", "")
        field = "primary_ip6" if ":" in addr else "primary_ip4"
        if compare_value(vm.get(field)) != ip_obj.get("id"):
            self.nb.patch(f"virtualization/virtual-machines/{vm['id']}/", {field: ip_obj["id"]})

    # -- Phase 6: platform -----------------------------------------------------

    def ensure_platform(self, slug: str, name: str) -> dict | None:
        if not self._platforms:
            self._platforms = {p["slug"]: p for p in self.nb.list_all("dcim/platforms/")}
        if slug in self._platforms:
            self.stats.bump("platform", "unchanged")
            return self._platforms[slug]
        created = self.nb.post("dcim/platforms/", {"slug": slug, "name": name})
        self.stats.bump("platform", "created")
        if created:
            self._platforms[slug] = created
        return created

    def detect_platform(self, gtype: str, vmid: int, cfg_data: dict) -> tuple[str, str] | None:
        if gtype == "qemu":
            try:
                data = self.px.get(f"/nodes/{self.node_name}/qemu/{vmid}/agent/get-osinfo")
                osinfo = data.get("result", {}) if isinstance(data, dict) else {}
                oid = (osinfo.get("id") or "").lower()
                ver = osinfo.get("version-id") or ""
                if oid:
                    slug = slugify(f"{oid}-{ver}") if ver else slugify(oid)
                    name = osinfo.get("pretty-name") or f"{oid} {ver}".strip()
                    return slug, name
            except Exception:
                pass
        ostype = (cfg_data.get("ostype") or "").lower()
        mapping = {"l26": ("linux", "Linux"), "debian": ("debian", "Debian"),
                   "ubuntu": ("ubuntu", "Ubuntu"), "centos": ("centos", "CentOS"),
                   "fedora": ("fedora", "Fedora"), "alpine": ("alpine", "Alpine"),
                   "archlinux": ("arch", "Arch Linux"), "win10": ("windows", "Windows"),
                   "win11": ("windows", "Windows"), "win2k22": ("windows", "Windows")}
        if ostype in mapping:
            return mapping[ostype]
        if ostype:
            return slugify(ostype), ostype
        return None

    # -- Phase 5: virtual disks ------------------------------------------------

    def ensure_virtual_disk(self, vm: dict, disk: dict) -> None:
        payload = {"virtual_machine": vm["id"], "name": disk["name"]}
        if disk.get("size_mb"):
            payload["size"] = disk["size_mb"]
        if disk.get("storage_pool"):
            payload["custom_fields"] = {"proxmox_storage_pool": disk["storage_pool"]}
        self._upsert("virtual-disk", "virtualization/virtual-disks/",
                     {"virtual_machine_id": vm["id"], "name": disk["name"]},
                     payload, compare_fields=["size"])

    # -- VM-level custom fields ------------------------------------------------

    def set_vm_custom_fields(self, vm: dict, vmid: int, gtype: str, ha_state: str) -> None:
        desired = {
            "proxmox_vmid": vmid, "proxmox_node": self.node_name,
            "proxmox_vm_type": gtype, "proxmox_ha_state": ha_state or "",
        }
        cur = vm.get("custom_fields") or {}
        core_changed = any(compare_value(cur.get(k)) != v for k, v in desired.items())
        # Always stamp last_seen (this is the liveness signal the reaper reads).
        payload = {**desired, "proxmox_last_seen": self.run_ts}
        if core_changed:
            self.stats.bump("vm-custom-fields", "updated")
        else:
            self.stats.bump("vm-custom-fields", "unchanged")
        self.nb.patch(f"virtualization/virtual-machines/{vm['id']}/", {"custom_fields": payload})

    # -- tags ------------------------------------------------------------------

    def ensure_tag(self, name: str) -> dict | None:
        slug = slugify(name)
        if self._tags_by_slug is None:
            self._tags_by_slug = {t["slug"]: t for t in self.nb.list_all("extras/tags/")}
        if slug in self._tags_by_slug:
            return self._tags_by_slug[slug]
        created = self.nb.post("extras/tags/", {"name": name, "slug": slug})
        self.stats.bump("tag", "created")
        if created:
            self._tags_by_slug[slug] = created
        return created

    @staticmethod
    def _clean_proxmox_tags(raw: str) -> list[str]:
        """Proxmox `tags` are ';'-delimited and mix IPs with real labels
        (e.g. '10.0.20.6;adblock;community-script'). Keep only label tokens."""
        out = []
        for tok in re.split(r"[;,]", raw or ""):
            tok = tok.strip()
            if not tok:
                continue
            try:
                ipaddress.ip_address(tok)
                continue  # drop bare IPs
            except ValueError:
                pass
            out.append(tok)
        return out

    def apply_vm_tags(self, vm: dict, gtype: str, proxmox_tags: str) -> None:
        # Base discovery tags + per-VM Proxmox tags.
        want = ["proxmox-ve", "discovered", f"proxmox-{self.node_name}"]
        want += self._clean_proxmox_tags(proxmox_tags)
        tag_objs = [t for t in (self.ensure_tag(n) for n in want) if t]
        want_ids = sorted({t["id"] for t in tag_objs})
        cur_ids = sorted({compare_value(t) for t in (vm.get("tags") or [])})
        if want_ids != cur_ids:
            self.nb.patch(f"virtualization/virtual-machines/{vm['id']}/",
                          {"tags": [{"id": i} for i in want_ids]})
            self.stats.bump("vm-tags", "updated")
        else:
            self.stats.bump("vm-tags", "unchanged")

    def vm_tags_map(self) -> dict[int, str]:
        out: dict[int, str] = {}
        try:
            for r in self.px.get("/cluster/resources?type=vm"):
                if r.get("vmid") is not None and r.get("tags"):
                    out[int(r["vmid"])] = r["tags"]
        except Exception:
            pass
        return out

    # -- per-guest driver ------------------------------------------------------

    def process_guest(self, gtype: str, item: dict, phases: set[str], ha_states: dict[int, str],
                      vm_tags: dict[int, str]) -> None:
        vmid = int(item["vmid"])
        name = item.get("name") or f"{gtype}-{vmid}"
        cfg_data = self.px.get_config(gtype, vmid)
        vm = self.get_vm(name)
        if not vm:
            # Auto-create: sync_v2 fully owns VM records (v1 cron retired).
            LOG.info("creating %s (vmid %s) in NetBox", name, vmid)
            vm = self.create_vm(item, cfg_data, gtype)
            if not vm:
                LOG.warning("failed to create %s (vmid %s); skipping enrichment", name, vmid)
                self.stats.bump("vm", "skipped")
                return
        else:
            self.refresh_vm_core(vm, item, gtype)

        # Build a MAC->agent-IP map for QEMU (Phase 2).
        agent_ips: dict[str, list[str]] = {}
        if gtype == "qemu" and "2" in phases:
            try:
                data = self.px.get(f"/nodes/{self.node_name}/qemu/{vmid}/agent/network-get-interfaces")
                for iface in (data.get("result", []) if isinstance(data, dict) else []):
                    mac = (iface.get("hardware-address") or "").upper()
                    if not mac or mac == "00:00:00:00:00:00":
                        continue
                    for a in iface.get("ip-addresses", []):
                        ip = a.get("ip-address"); pfx = a.get("prefix")
                        if not ip or ip.startswith("127.") or ip == "::1" or ip.startswith("fe80:"):
                            continue
                        agent_ips.setdefault(mac, []).append(f"{ip}/{pfx}" if pfx else normalize_ip(ip))
            except Exception:
                pass

        primary_candidate: dict | None = None
        for key, net in iter_net_entries(cfg_data):
            iface_name = net["name"] or key  # LXC eth0 / QEMU net0
            if "1" in phases:
                iface = self.ensure_vminterface(vm, iface_name, net.get("bridge"))
                if iface and net.get("mac"):
                    self.ensure_mac(iface, net["mac"])
            else:
                existing = self.nb.list_all("virtualization/interfaces/",
                                            virtual_machine_id=vm["id"], name=iface_name)
                iface = existing[0] if existing else None
            if not iface:
                continue
            if "3" in phases and net.get("tag"):
                self.set_interface_vlan(iface, net["tag"])
            if "2" in phases:
                ips: list[str] = []
                if net.get("ip"):
                    ips.append(net["ip"])
                if net.get("mac") and net["mac"] in agent_ips:
                    ips.extend(agent_ips[net["mac"]])
                for ip in dict.fromkeys(ips):  # dedupe, preserve order
                    ip_obj = self.assign_ip(ip, iface, name)
                    if ip_obj and primary_candidate is None and "/32" not in ip and "/128" not in ip:
                        primary_candidate = ip_obj

        if "2" in phases and primary_candidate:
            self.set_primary_ip(vm, primary_candidate)

        if "5" in phases:
            for disk in iter_disk_entries(cfg_data):
                self.ensure_virtual_disk(vm, disk)

        if "6" in phases:
            plat = self.detect_platform(gtype, vmid, cfg_data)
            if plat:
                p = self.ensure_platform(*plat)
                if p and compare_value(vm.get("platform")) != p["id"]:
                    self.nb.patch(f"virtualization/virtual-machines/{vm['id']}/", {"platform": p["id"]})

        if "0" in phases:  # custom fields populated when scaffolding ran
            self.set_vm_custom_fields(vm, vmid, gtype, ha_states.get(vmid, ""))
            self.apply_vm_tags(vm, gtype, vm_tags.get(vmid, ""))

    # -- Phase 4 / 4b: node enrichment ----------------------------------------

    def get_node_device(self) -> dict | None:
        found = self.nb.list_all("dcim/devices/", name=self.node_name)
        return found[0] if found else None

    def ensure_node_interface(self, device: dict, name: str, iftype: str) -> dict | None:
        payload = {"device": device["id"], "name": name, "type": iftype, "enabled": True}
        return self._upsert("node-interface", "dcim/interfaces/",
                            {"device_id": device["id"], "name": name},
                            payload, compare_fields=["type"])

    def assign_device_ip(self, address: str, interface: dict) -> dict | None:
        try:
            ipaddress.ip_interface(address)
        except ValueError:
            return None
        found = self.nb.list_all("ipam/ip-addresses/", address=address)
        tt = "dcim.interface"
        if not found:
            created = self.nb.post("ipam/ip-addresses/", {
                "address": address, "status": "active",
                "assigned_object_type": tt, "assigned_object_id": interface["id"],
                "description": f"{self.node_name} {interface['name']}",
            })
            self.stats.bump("node-ip", "created")
            return created
        ip = found[0]
        cur_aid = compare_value(ip.get("assigned_object_id") or
                                (ip.get("assigned_object", {}) or {}).get("id"))
        if cur_aid != interface["id"]:
            updated = self.nb.patch(f"ipam/ip-addresses/{ip['id']}/", {
                "assigned_object_type": tt, "assigned_object_id": interface["id"]}) or ip
            self.stats.bump("node-ip", "updated")
            return updated
        self.stats.bump("node-ip", "unchanged")
        return ip

    def phase4_node(self, idrac: IDRACClient | None) -> None:
        LOG.info("Phase 4: node DCIM enrichment")
        device = self.get_node_device()
        if not device:
            LOG.warning("node device %s not found in NetBox; skipping node enrichment", self.node_name)
            return

        # Proxmox node network -> interfaces + IPs.
        try:
            netifaces = self.px.get(f"/nodes/{self.node_name}/network")
        except Exception as exc:
            LOG.warning("could not read node network: %s", exc)
            netifaces = []
        node_primary_ip: dict | None = None
        for n in netifaces:
            ntype = n.get("type")
            iface = n.get("iface")
            if not iface or ntype not in ("eth", "bridge", "bond", "vlan"):
                continue
            iftype = "bridge" if ntype == "bridge" else ("lag" if ntype == "bond" else "1000base-t")
            nbiface = self.ensure_node_interface(device, iface, iftype)
            cidr = n.get("cidr")
            if nbiface and cidr:
                ipobj = self.assign_device_ip(cidr, nbiface)
                if ipobj and node_primary_ip is None:
                    node_primary_ip = ipobj
        if node_primary_ip and compare_value(device.get("primary_ip4")) != node_primary_ip.get("id"):
            self.nb.patch(f"dcim/devices/{device['id']}/", {"primary_ip4": node_primary_ip["id"]})

        # Track A: static hardware facts (non-destructive).
        if NODE_STATIC:
            self._apply_node_facts(device, NODE_STATIC)

        # Discovery tags on the node device.
        node_tag_ids = sorted({t["id"] for t in
                               (self.ensure_tag(n) for n in
                                ("proxmox-ve", "discovered", f"proxmox-{self.node_name}")) if t})
        cur_node_tags = sorted({compare_value(t) for t in (device.get("tags") or [])})
        if node_tag_ids != cur_node_tags:
            self.nb.patch(f"dcim/devices/{device['id']}/",
                          {"tags": [{"id": i} for i in node_tag_ids]})
            self.stats.bump("node-tags", "updated")

        # Node CPU/memory custom fields + Proxmox platform, from /nodes/<n>/status.
        try:
            status = self.px.get(f"/nodes/{self.node_name}/status")
        except Exception:
            status = {}
        if status:
            cpus = (status.get("cpuinfo") or {}).get("cpus")
            mem_total = (status.get("memory") or {}).get("total")
            mem_mb = int(mem_total / 1024 / 1024) if mem_total else None
            cf = dict(device.get("custom_fields") or {})
            cf_updates = {}
            if cpus and cf.get("proxmox_cpu_count") != cpus:
                cf_updates["proxmox_cpu_count"] = cpus
            if mem_mb and cf.get("proxmox_memory_mb") != mem_mb:
                cf_updates["proxmox_memory_mb"] = mem_mb
            if cf_updates:
                self.nb.patch(f"dcim/devices/{device['id']}/",
                              {"custom_fields": {**cf, **cf_updates}})
                self.stats.bump("node-facts", "updated")
            # Platform from pveversion (e.g. pve-manager/9.2.3/... -> "Proxmox VE 9.2").
            pve = status.get("pveversion") or ""
            m = re.search(r"pve-manager/(\d+)\.(\d+)", pve)
            if m:
                plat = self.ensure_platform(f"proxmox-ve-{m.group(1)}-{m.group(2)}",
                                            f"Proxmox VE {m.group(1)}.{m.group(2)}")
                if plat and compare_value(device.get("platform")) != plat["id"]:
                    self.nb.patch(f"dcim/devices/{device['id']}/", {"platform": plat["id"]})

        # Track B: live Redfish (optional). Must never abort the sync.
        if idrac:
            try:
                self._idrac_enrich(device, idrac)
            except Exception as exc:
                LOG.warning("iDRAC live enrichment failed (continuing): %s", exc)

    def _apply_node_facts(self, device: dict, facts: dict) -> None:
        diff: dict[str, Any] = {}
        # serial: only fill if empty (unless overwrite).
        if facts.get("serial") and (self.idrac_overwrite or not (device.get("serial") or "").strip()):
            if device.get("serial") != facts["serial"]:
                diff["serial"] = facts["serial"]
        cf = dict(device.get("custom_fields") or {})
        cf_updates = {}
        for k in ("idrac_service_tag", "idrac_ip", "bios_version", "idrac_firmware"):
            if facts.get(k) and (self.idrac_overwrite or not cf.get(k)):
                if cf.get(k) != facts[k]:
                    cf_updates[k] = facts[k]
        if cf_updates:
            diff["custom_fields"] = {**cf, **cf_updates}
        if diff:
            self.nb.patch(f"dcim/devices/{device['id']}/", diff)
            self.stats.bump("node-facts", "updated")
        else:
            self.stats.bump("node-facts", "unchanged")

    def _idrac_enrich(self, device: dict, idrac: IDRACClient) -> None:
        sysinfo = idrac.system()
        if not sysinfo:
            LOG.warning("iDRAC: no system data; skipping live enrichment")
            return
        facts = {
            "serial": sysinfo.get("SKU") or sysinfo.get("SerialNumber"),
            "idrac_service_tag": sysinfo.get("SKU"),
            "bios_version": sysinfo.get("BiosVersion"),
            "idrac_ip": os.environ.get("IDRAC_HOST"),
        }
        self._apply_node_facts(device, {k: v for k, v in facts.items() if v})
        for nic in idrac.ethernet_interfaces():
            name = nic.get("Id") or nic.get("Name")
            mac = nic.get("PermanentMACAddress") or nic.get("MACAddress")
            if not name:
                continue
            # R630 integrated NICs are Intel X710 SFP+ (10G).
            nbiface = self.ensure_node_interface(device, name, "10gbase-x-sfpp")
            if nbiface and mac:
                self.ensure_mac_on_device_iface(nbiface, mac)

    def ensure_mac_on_device_iface(self, interface: dict, mac: str) -> None:
        mac = mac.upper()
        found = self.nb.list_all("dcim/mac-addresses/", mac_address=mac)
        macobj = None
        for m in found:
            aid = compare_value(m.get("assigned_object_id") or
                                (m.get("assigned_object", {}) or {}).get("id"))
            if aid == interface["id"]:
                macobj = m
                break
        if macobj is None and found:
            macobj = found[0]
        if macobj is None:
            macobj = self.nb.post("dcim/mac-addresses/", {
                "mac_address": mac,
                "assigned_object_type": "dcim.interface",
                "assigned_object_id": interface["id"],
            })
            self.stats.bump("node-mac", "created")
        else:
            self.stats.bump("node-mac", "unchanged")
        if macobj and compare_value(interface.get("primary_mac_address")) != macobj.get("id"):
            self.nb.patch(f"dcim/interfaces/{interface['id']}/",
                          {"primary_mac_address": macobj["id"]})

    # -- HA state map ----------------------------------------------------------

    def ha_state_map(self) -> dict[int, str]:
        out: dict[int, str] = {}
        try:
            for r in self.px.get("/cluster/ha/resources"):
                sid = str(r.get("sid", ""))  # e.g. "vm:100" or "ct:103"
                if ":" in sid and sid.split(":", 1)[1].isdigit():
                    out[int(sid.split(":", 1)[1])] = r.get("state", "")
        except Exception:
            pass
        return out

    # -- removal lifecycle (reaper) -------------------------------------------

    def reap_missing_vms(self, live_vmids: set[int]) -> None:
        """Lifecycle for sync_v2-owned VMs no longer present in Proxmox, keyed on
        the proxmox_last_seen timestamp:
            missing > OFFLINE_AFTER  -> status 'offline'
            missing > DECOM_AFTER    -> status 'decommissioning'
            missing > DELETE_AFTER   -> delete the NetBox record
        Only touches VMs with a proxmox_vmid CF (i.e. this sync owns them); never
        the pre-existing manual/v1 orphans."""
        offline_d = float(os.environ.get("REAP_OFFLINE_DAYS", "2"))
        decom_d = float(os.environ.get("REAP_DECOM_DAYS", "14"))
        delete_d = float(os.environ.get("REAP_DELETE_DAYS", "30"))
        now = datetime.now(timezone.utc)

        for vm in self.nb.list_all("virtualization/virtual-machines/"):
            cf = vm.get("custom_fields") or {}
            pvmid = cf.get("proxmox_vmid")
            if pvmid is None:
                continue  # not owned by this sync -> leave alone
            if int(pvmid) in live_vmids:
                continue  # still present in Proxmox
            last_seen = cf.get("proxmox_last_seen")
            if not last_seen:
                continue  # no liveness data yet -> wait for a stamp first
            try:
                seen = datetime.fromisoformat(str(last_seen).replace("Z", "+00:00"))
            except ValueError:
                continue
            age_days = (now - seen).total_seconds() / 86400.0
            cur_status = compare_value(vm.get("status"))
            name = vm.get("name")

            if age_days >= delete_d:
                LOG.info("reap: DELETE %s (vmid %s, missing %.1fd)", name, pvmid, age_days)
                self.nb.delete(f"virtualization/virtual-machines/{vm['id']}/")
                self.stats.bump("vm-reaped", "deleted")
            elif age_days >= decom_d and cur_status != "decommissioning":
                LOG.info("reap: DECOMMISSIONING %s (vmid %s, missing %.1fd)", name, pvmid, age_days)
                self.nb.patch(f"virtualization/virtual-machines/{vm['id']}/",
                              {"status": "decommissioning"})
                self.stats.bump("vm-reaped", "decommissioning")
            elif age_days >= offline_d and cur_status not in ("offline", "decommissioning"):
                LOG.info("reap: OFFLINE %s (vmid %s, missing %.1fd)", name, pvmid, age_days)
                self.nb.patch(f"virtualization/virtual-machines/{vm['id']}/",
                              {"status": "offline"})
                self.stats.bump("vm-reaped", "offline")
            else:
                self.stats.bump("vm-reaped", "waiting")

    # -- top-level run ---------------------------------------------------------

    def run(self, phases: set[str], only_vmid: int | None, idrac: IDRACClient | None,
            reap: bool = True) -> None:
        if "0" in phases:
            self.phase0_custom_fields()

        ha_states = self.ha_state_map() if "0" in phases else {}
        vm_tags = self.vm_tags_map() if "0" in phases else {}

        guest_phases = phases & {"0", "1", "2", "3", "5", "6"}
        if guest_phases:
            for gtype, item in self.px.list_guests():
                if only_vmid is not None and int(item["vmid"]) != only_vmid:
                    continue
                try:
                    self.process_guest(gtype, item, phases, ha_states, vm_tags)
                except Exception as exc:
                    LOG.error("guest %s/%s failed: %s", gtype, item.get("vmid"), exc)

        if "4" in phases and only_vmid is None:
            self.phase4_node(idrac)

        # Removal lifecycle: only on a full run that processed guests (so we have
        # an authoritative live set and fresh last_seen stamps).
        if reap and only_vmid is None and "0" in phases:
            live_vmids = {int(item["vmid"]) for _, item in self.px.list_guests()}
            self.reap_missing_vms(live_vmids)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    parser.add_argument("--dry-run", action="store_true", help="Log intended writes, change nothing")
    parser.add_argument("--phases", default=",".join(ALL_PHASES),
                        help="Comma-separated phases to run (default: all)")
    parser.add_argument("--only-vmid", type=int, default=None, help="Limit to a single VMID")
    parser.add_argument("--idrac", action="store_true",
                        help="Enable Track B live iDRAC Redfish enrichment (off by default)")
    parser.add_argument("--idrac-overwrite", action="store_true",
                        help="Allow iDRAC/static data to overwrite manually-set node fields")
    parser.add_argument("--no-reap", action="store_true",
                        help="Disable the removal lifecycle (offline/decom/delete of missing VMs)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s [%(levelname)s] %(message)s")

    load_env(args.env_file)
    phases = {p.strip() for p in args.phases.split(",") if p.strip()}
    LOG.info("sync_v2 starting: dry_run=%s phases=%s only_vmid=%s idrac=%s",
             args.dry_run, sorted(phases), args.only_vmid, args.idrac)

    # Clients (scaffold; phase logic added in subsequent steps).
    px = ProxmoxClient()
    netbox_url = os.environ.get("NETBOX_URL", "").strip()
    netbox_token = os.environ.get("NETBOX_TOKEN", "").strip()
    if not netbox_url or not netbox_token:
        LOG.error("NETBOX_URL and NETBOX_TOKEN required")
        return 2
    nb = NetBoxClient(netbox_url, netbox_token, dry_run=args.dry_run)
    stats = Stats()

    idrac = None
    if args.idrac:
        idrac = IDRACClient.from_env()

    sync = SyncV2(px, nb, stats, dry_run=args.dry_run, idrac_overwrite=args.idrac_overwrite)
    sync.run(phases, args.only_vmid, idrac, reap=not args.no_reap)

    LOG.info("\n%s", stats.render())
    LOG.info("sync_v2 finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
