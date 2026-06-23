#!/usr/bin/env python3
"""
Populate NetBox from UniFi snapshots.

This draft is intended to be copied into /opt/netbox-sync on LXC 661.
It reads NETBOX_URL and NETBOX_TOKEN from /opt/netbox-sync/.env by default,
supports --dry-run, and is safe to run repeatedly.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import requests


LOG = logging.getLogger("netbox-populate")


DEFAULT_ENV_FILE = "/opt/netbox-sync/.env"
DEFAULT_SITE_ID = 1
DEFAULT_SITE_NAME = os.environ.get("NETBOX_SITE_NAME", "Home Lab")
DEFAULT_SITE_SLUG = os.environ.get("NETBOX_SITE_SLUG", "home-lab")


ROLE_MAP = {
    "accessPoint": ("access-point", "Access Point"),
    "switching": ("switch", "Switch"),
    "firewall": ("firewall", "Firewall"),
    "server": ("server", "Server"),
}


# Your VLAN / subnet map. Override by pointing PREFIXES_FILE at a JSON file with the
# same shape (list of {"prefix","vlan","name"}); otherwise these example defaults
# are used. Edit to match your network.
EXAMPLE_PREFIXES = [
    {"prefix": "10.0.10.0/24", "vlan": 10, "name": "Management"},
    {"prefix": "10.0.20.0/24", "vlan": 20, "name": "Servers"},
    {"prefix": "10.0.30.0/24", "vlan": 30, "name": "Clients"},
    {"prefix": "10.0.40.0/24", "vlan": 40, "name": "IoT"},
]


def _load_prefixes() -> list[dict[str, Any]]:
    path = os.environ.get("PREFIXES_FILE")
    if path and Path(path).exists():
        try:
            return json.loads(Path(path).read_text())
        except (OSError, ValueError):
            LOG.warning("could not read PREFIXES_FILE %s; using example defaults", path)
    return EXAMPLE_PREFIXES


PREFIXES = _load_prefixes()


UPSTREAM_PREFIX = {"prefix": "192.168.1.0/24", "vlan": None, "name": "Upstream/WAN"}


def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "unnamed"


def short_label(value: str, limit: int = 63) -> str:
    value = re.sub(r"[^a-zA-Z0-9.-]+", "-", value).strip("-").lower()
    return value[:limit].strip("-") or "unnamed"


def load_env(path: Path) -> None:
    if not path.exists():
        LOG.warning("env file not found: %s", path)
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ[key] = value


def load_snapshot(path: Path) -> list[dict[str, Any]]:
    if not path:
        return []
    if not path.exists():
        LOG.warning("snapshot missing: %s", path)
        return []
    data = json.loads(path.read_text())
    if isinstance(data, dict):
        items = data.get("items", [])
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def coalesce(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}, ()):
            return value
    return None


def compare_value(value: Any) -> Any:
    if isinstance(value, dict) and "id" in value:
        return value["id"]
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


class NetBoxClient:
    def __init__(self, netbox_url: str, token: str, dry_run: bool = False):
        base = netbox_url.rstrip("/")
        if not base.endswith("/api"):
            base = f"{base}/api"
        self.base = base
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Token {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )
        self.dry_run = dry_run
        self._dry_run_id = -1

    def _url(self, path: str) -> str:
        return f"{self.base}/{path.lstrip('/')}"

    def get(self, path: str, **params: Any) -> Any:
        response = self.session.get(self._url(path), params=params, timeout=30)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def list_all(self, path: str, **params: Any) -> list[dict[str, Any]]:
        params = dict(params)
        params.setdefault("limit", 0)
        payload = self.get(path, **params)
        if not payload:
            return []
        if isinstance(payload, dict) and "results" in payload:
            return payload["results"]
        if isinstance(payload, list):
            return payload
        return []

    def post(self, path: str, payload: dict[str, Any]) -> Any:
        if self.dry_run:
            LOG.info("dry-run POST %s %s", path, self._summarize(payload))
            result = dict(payload)
            result["id"] = self._dry_run_id
            self._dry_run_id -= 1
            return result
        response = self.session.post(self._url(path), json=payload, timeout=30)
        response.raise_for_status()
        return response.json()

    def patch(self, path: str, payload: dict[str, Any]) -> Any:
        if self.dry_run:
            LOG.info("dry-run PATCH %s %s", path, self._summarize(payload))
            return None
        response = self.session.patch(self._url(path), json=payload, timeout=30)
        response.raise_for_status()
        return response.json()

    def delete(self, path: str) -> bool:
        if self.dry_run:
            LOG.info("dry-run DELETE %s", path)
            return True
        response = self.session.delete(self._url(path), timeout=30)
        response.raise_for_status()
        return True

    @staticmethod
    def _summarize(payload: dict[str, Any]) -> str:
        interesting = {}
        for key in ("name", "slug", "prefix", "address", "model", "vid", "status"):
            if key in payload:
                interesting[key] = payload[key]
        return json.dumps(interesting, sort_keys=True)


class NetBoxPopulation:
    def __init__(self, client: NetBoxClient, dry_run: bool = False):
        self.client = client
        self.dry_run = dry_run
        self._sites: dict[int, dict[str, Any]] = {}
        self._manufacturers: dict[str, dict[str, Any]] = {}
        self._device_types: dict[tuple[str, str], dict[str, Any]] = {}
        self._roles: dict[str, dict[str, Any]] = {}
        self._devices: dict[str, dict[str, Any]] = {}
        self._devices_by_name: dict[str, dict[str, Any]] = {}
        self._interfaces: dict[tuple[int, str], dict[str, Any]] = {}
        self._vlans: dict[tuple[int, int], dict[str, Any]] = {}
        self._vlans_by_vid: dict[int, dict[str, Any]] = {}
        self._prefixes: dict[str, dict[str, Any]] = {}
        self._ips: dict[str, dict[str, Any]] = {}
        self.refresh_cache()

    def refresh_cache(self) -> None:
        self._sites = {item["id"]: item for item in self.client.list_all("dcim/sites/")}
        self._manufacturers = {
            item["name"].lower(): item for item in self.client.list_all("dcim/manufacturers/")
        }
        self._device_types = {
            (item["manufacturer"]["name"].lower(), item["model"].lower()): item
            for item in self.client.list_all("dcim/device-types/")
            if item.get("manufacturer") and item.get("model")
        }
        self._roles = {item["slug"]: item for item in self.client.list_all("dcim/device-roles/")}
        self._devices = {}
        self._devices_by_name = {}
        for item in self.client.list_all("dcim/devices/"):
            asset_tag = (item.get("asset_tag") or "").strip()
            if asset_tag:
                self._devices[asset_tag] = item
            name = (item.get("name") or "").strip().lower()
            if name:
                self._devices_by_name[name] = item
        self._interfaces = {
            (item["device"]["id"], item["name"].lower()): item
            for item in self.client.list_all("dcim/interfaces/")
            if item.get("device") and item.get("name")
        }
        vlan_items = self.client.list_all("ipam/vlans/")
        self._vlans = {
            (item["site"]["id"], item["vid"]): item
            for item in vlan_items
            if item.get("site") and item.get("vid") is not None
        }
        self._vlans_by_vid = {
            item["vid"]: item
            for item in vlan_items
            if item.get("vid") is not None
        }
        self._prefixes = {item["prefix"]: item for item in self.client.list_all("ipam/prefixes/")}
        self._ips = {item["address"]: item for item in self.client.list_all("ipam/ip-addresses/")}

    def ensure_site(self, site_id: int, name: str, slug: str) -> dict[str, Any]:
        payload = {
            "name": name,
            "slug": slug,
            "status": "active",
        }
        existing = self.client.get(f"dcim/sites/{site_id}/")
        if existing:
            changed = False
            for key, value in payload.items():
                if compare_value(existing.get(key)) != value:
                    changed = True
                    break
            if changed:
                LOG.info("update site %s", existing.get("name", site_id))
                updated = self.client.patch(f"dcim/sites/{site_id}/", payload)
                if updated:
                    self._sites[site_id] = updated
                    return updated
            self._sites[site_id] = existing
            return existing

        for item in self._sites.values():
            if item.get("name") == name or item.get("slug") == slug:
                changed = {}
                for key, value in payload.items():
                    if item.get(key) != value:
                        changed[key] = value
                if changed:
                    LOG.info("update site %s", item.get("name"))
                    updated = self.client.patch(f"dcim/sites/{item['id']}/", changed)
                    if updated:
                        self._sites[item["id"]] = updated
                        return updated
                return item

        LOG.info("create site %s", name)
        created = self.client.post("dcim/sites/", payload)
        if created:
            self._sites[created["id"]] = created
        return created

    def ensure_manufacturer(self, name: str) -> dict[str, Any]:
        key = name.lower()
        payload = {"name": name, "slug": slugify(name)}
        existing = self._manufacturers.get(key)
        if existing:
            changed = {}
            for field, value in payload.items():
                if existing.get(field) != value:
                    changed[field] = value
            if changed:
                LOG.info("update manufacturer %s", name)
                updated = self.client.patch(f"dcim/manufacturers/{existing['id']}/", changed)
                if updated:
                    self._manufacturers[key] = updated
                    return updated
            return existing
        LOG.info("create manufacturer %s", name)
        created = self.client.post("dcim/manufacturers/", payload)
        if created:
            self._manufacturers[key] = created
        return created

    def ensure_device_type(
        self, manufacturer: dict[str, Any], model: str, role_slug: str | None = None
    ) -> dict[str, Any]:
        key = (manufacturer["name"].lower(), model.lower())
        payload = {
            "manufacturer": manufacturer["id"],
            "model": model,
            "slug": slugify(f"{manufacturer['name']}-{model}"),
            "u_height": 1,
            "is_full_depth": False,
        }
        existing = self._device_types.get(key)
        if existing:
            changed = {}
            for field, value in payload.items():
                existing_value = compare_value(existing.get(field))
                if existing_value != value:
                    changed[field] = value
            if changed:
                LOG.info("update device type %s", model)
                updated = self.client.patch(f"dcim/device-types/{existing['id']}/", changed)
                if updated:
                    self._device_types[key] = updated
                    return updated
            return existing

        LOG.info("create device type %s", model)
        created = self.client.post("dcim/device-types/", payload)
        if created:
            self._device_types[key] = created
        return created

    def ensure_role(self, slug: str, name: str) -> dict[str, Any]:
        payload = {"slug": slug, "name": name}
        existing = self._roles.get(slug)
        if existing:
            changed = {}
            for field, value in payload.items():
                if compare_value(existing.get(field)) != value:
                    changed[field] = value
            if changed:
                LOG.info("update device role %s", name)
                updated = self.client.patch(f"dcim/device-roles/{existing['id']}/", changed)
                if updated:
                    self._roles[slug] = updated
                    return updated
            return existing
        LOG.info("create device role %s", name)
        created = self.client.post("dcim/device-roles/", payload)
        if created:
            self._roles[slug] = created
        return created

    def ensure_vlan(
        self, site: dict[str, Any], vid: int, name: str, description: str | None = None
    ) -> dict[str, Any]:
        key = (site["id"], vid)
        payload = {
            "site": site["id"],
            "vid": vid,
            "name": name,
            "status": "active",
        }
        if description:
            payload["description"] = description
        existing = self._vlans.get(key) or self._vlans_by_vid.get(vid)
        if existing:
            changed = {}
            for field, value in payload.items():
                if compare_value(existing.get(field)) != value:
                    changed[field] = value
            if changed:
                LOG.info("update vlan %s", vid)
                updated = self.client.patch(f"ipam/vlans/{existing['id']}/", changed)
                if updated:
                    self._vlans[key] = updated
                    self._vlans_by_vid[vid] = updated
                    return updated
            return existing
        LOG.info("create vlan %s %s", vid, name)
        created = self.client.post("ipam/vlans/", payload)
        if created:
            self._vlans[key] = created
            self._vlans_by_vid[vid] = created
        return created

    def ensure_custom_field(self, name: str, label: str, object_types: list[str]) -> None:
        """Create a simple text custom field if absent (idempotent, never deletes)."""
        if not hasattr(self, "_cf_names") or self._cf_names is None:
            self._cf_names = {c["name"] for c in self.client.list_all("extras/custom-fields/")}
        if name in self._cf_names:
            return
        LOG.info("create custom field %s", name)
        self.client.post("extras/custom-fields/", {
            "name": name, "label": label, "type": "text",
            "object_types": object_types, "required": False,
        })
        self._cf_names.add(name)

    def ensure_wireless_client_device(
        self,
        site: dict[str, Any],
        device_type: dict[str, Any],
        role: dict[str, Any],
        mac: str,
        name: str,
        wlan: dict[str, Any] | None,
        ip_cidr: str | None,
        extra: str | None = None,
    ) -> dict[str, Any] | None:
        """Model a UniFi wireless client as a lightweight NetBox device with a
        single wireless interface carrying its MAC, WLAN membership, and IP."""
        mac = mac.upper()
        suffix = mac.replace(":", "")[-6:]
        # Disambiguate (many "iPhone"/"SonosZP") and stay <=64 chars.
        dev_name = f"{name}-{suffix}" if name else f"wifi-{suffix}"
        dev_name = dev_name[:64]
        # Use the MAC as asset_tag so re-runs match the same device.
        payload = {
            "name": dev_name,
            "site": site["id"],
            "device_type": device_type["id"],
            "role": role["id"],
            "status": "active",
            "asset_tag": f"wifi:{mac}",
            "comments": extra or "",
        }
        existing = self._devices.get(f"wifi:{mac}")
        if existing:
            device = existing
        else:
            LOG.info("create wireless client device %s", dev_name)
            device = self.client.post("dcim/devices/", payload)
            if device:
                self._devices[f"wifi:{mac}"] = device
        if not device:
            return None

        iface = self.ensure_interface(device, "wlan0")
        # Make the interface wireless and attach the WLAN membership.
        patch: dict[str, Any] = {}
        if compare_value(iface.get("type")) != "ieee802.11ac":
            patch["type"] = "ieee802.11ac"
        if iface.get("mgmt_only"):
            patch["mgmt_only"] = False
        if wlan:
            current = [compare_value(w) for w in (iface.get("wireless_lans") or [])]
            if wlan["id"] not in current:
                patch["wireless_lans"] = current + [wlan["id"]]
        if patch:
            updated = self.client.patch(f"dcim/interfaces/{iface['id']}/", patch)
            if updated:
                iface = updated

        # Attach MAC object to the interface (NetBox 4.x MAC model).
        self._ensure_dcim_mac(iface, mac)

        if ip_cidr:
            self.ensure_ip(ip_cidr, assigned_object=iface,
                           description=f"Wireless client {dev_name} ({mac})")
        return device

    def _ensure_dcim_mac(self, interface: dict[str, Any], mac: str) -> None:
        mac = mac.upper()
        found = self.client.list_all("dcim/mac-addresses/", mac_address=mac)
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
            macobj = self.client.post("dcim/mac-addresses/", {
                "mac_address": mac,
                "assigned_object_type": "dcim.interface",
                "assigned_object_id": interface["id"],
            })
        if macobj and compare_value(interface.get("primary_mac_address")) != macobj.get("id"):
            self.client.patch(f"dcim/interfaces/{interface['id']}/",
                              {"primary_mac_address": macobj["id"]})

    def ensure_wireless_lan(
        self,
        ssid: str,
        auth_type: str,
        auth_cipher: str,
        status: str,
        vlan: dict[str, Any] | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ssid": ssid[:32],
            "auth_type": auth_type,
            "auth_cipher": auth_cipher,
            "status": status,
        }
        if vlan:
            payload["vlan"] = vlan["id"]
        if description:
            payload["description"] = description
        existing = self.client.list_all("wireless/wireless-lans/", ssid=ssid)
        if existing:
            item = existing[0]
            changed = {}
            for field, value in payload.items():
                if compare_value(item.get(field)) != value:
                    changed[field] = value
            if changed:
                LOG.info("update wireless LAN %s", ssid)
                return self.client.patch(f"wireless/wireless-lans/{item['id']}/", changed) or item
            return item
        LOG.info("create wireless LAN %s", ssid)
        return self.client.post("wireless/wireless-lans/", payload)

    def ensure_prefix(
        self, site: dict[str, Any], prefix: str, description: str | None = None
    ) -> dict[str, Any]:
        payload = {
            "prefix": prefix,
            "status": "active",
        }
        if description:
            payload["description"] = description
        existing = self._prefixes.get(prefix)
        if existing:
            changed = {}
            for field, value in payload.items():
                if compare_value(existing.get(field)) != value:
                    changed[field] = value
            if changed:
                LOG.info("update prefix %s", prefix)
                updated = self.client.patch(f"ipam/prefixes/{existing['id']}/", changed)
                if updated:
                    self._prefixes[prefix] = updated
                    return updated
            return existing
        LOG.info("create prefix %s", prefix)
        created = self.client.post("ipam/prefixes/", payload)
        if created:
            self._prefixes[prefix] = created
        return created

    def ensure_device(
        self,
        site: dict[str, Any],
        uni_device: dict[str, Any],
        device_type: dict[str, Any],
        role: dict[str, Any],
    ) -> dict[str, Any]:
        asset_tag = str(uni_device["id"])
        name = coalesce(uni_device.get("name"), uni_device.get("model"), asset_tag)
        payload = {
            "name": name,
            "site": site["id"],
            "device_type": device_type["id"],
            "role": role["id"],
            "status": "active",
            "asset_tag": asset_tag,
            "comments": (
                f"UniFi id={uni_device.get('id')} mac={uni_device.get('macAddress')} "
                f"model={uni_device.get('model')} firmware={uni_device.get('firmwareVersion')} "
                f"state={uni_device.get('state')}"
            ),
        }
        existing = self._devices.get(asset_tag) or self._devices_by_name.get(str(name).lower())
        if existing:
            changed = {}
            for field, value in payload.items():
                if compare_value(existing.get(field)) != value:
                    changed[field] = value
            if changed:
                LOG.info("update device %s", name)
                updated = self.client.patch(f"dcim/devices/{existing['id']}/", changed)
                if updated:
                    self._devices[asset_tag] = updated
                    self._devices_by_name[str(name).lower()] = updated
                    return updated
            return existing
        LOG.info("create device %s", name)
        created = self.client.post("dcim/devices/", payload)
        if created:
            self._devices[asset_tag] = created
            self._devices_by_name[str(name).lower()] = created
        return created

    def ensure_interface(self, device: dict[str, Any], name: str = "mgmt") -> dict[str, Any]:
        key = (device["id"], name.lower())
        payload = {
            "device": device["id"],
            "name": name,
            "type": "1000base-t",
            "enabled": True,
            "mgmt_only": True,
        }
        existing = self._interfaces.get(key)
        if existing:
            changed = {}
            for field, value in payload.items():
                if compare_value(existing.get(field)) != value:
                    changed[field] = value
            if changed:
                LOG.info("update interface %s on %s", name, device.get("name"))
                updated = self.client.patch(f"dcim/interfaces/{existing['id']}/", changed)
                if updated:
                    self._interfaces[key] = updated
                    return updated
            return existing
        LOG.info("create interface %s on %s", name, device.get("name"))
        created = self.client.post("dcim/interfaces/", payload)
        if created:
            self._interfaces[key] = created
        return created

    def ensure_switch_port(
        self, device: dict[str, Any], name: str, media: str | None = None, poe: bool = False
    ) -> dict[str, Any]:
        key = (device["id"], name.lower())
        iftype = "1000base-t"  # UniFi US8/Flex Mini ports are GbE RJ45
        if media and media.upper() in ("SFP", "SFP+"):
            iftype = "10gbase-x-sfpp"
        payload = {
            "device": device["id"],
            "name": name,
            "type": iftype,
            "enabled": True,
            "mgmt_only": False,
        }
        if poe:
            payload["poe_mode"] = "pse"
        existing = self._interfaces.get(key)
        if existing:
            changed = {}
            for field in ("type", "enabled"):
                if compare_value(existing.get(field)) != payload[field]:
                    changed[field] = payload[field]
            if changed:
                LOG.info("update switch port %s on %s", name, device.get("name"))
                updated = self.client.patch(f"dcim/interfaces/{existing['id']}/", changed)
                if updated:
                    self._interfaces[key] = updated
                    return updated
            return existing
        LOG.info("create switch port %s on %s", name, device.get("name"))
        created = self.client.post("dcim/interfaces/", payload)
        if created:
            self._interfaces[key] = created
        return created

    def ensure_cable(self, iface_a: dict[str, Any], iface_b: dict[str, Any]) -> dict[str, Any] | None:
        """Create a point-to-point cable between two DCIM interfaces if neither
        end is already cabled. Idempotent: a connected interface reports a cable."""
        if iface_a.get("cable") or iface_b.get("cable"):
            return None  # already cabled (NetBox interfaces are 1:1)
        payload = {
            "a_terminations": [{"object_type": "dcim.interface", "object_id": iface_a["id"]}],
            "b_terminations": [{"object_type": "dcim.interface", "object_id": iface_b["id"]}],
            "status": "connected",
        }
        LOG.info("create cable %s:%s <-> %s:%s",
                 iface_a.get("device", {}).get("name") if isinstance(iface_a.get("device"), dict) else "?",
                 iface_a.get("name"),
                 iface_b.get("device", {}).get("name") if isinstance(iface_b.get("device"), dict) else "?",
                 iface_b.get("name"))
        return self.client.post("dcim/cables/", payload)

    def ensure_ip(
        self,
        address: str,
        dns_name: str | None = None,
        description: str | None = None,
        assigned_object: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "address": address,
            "status": "active",
        }
        if dns_name:
            payload["dns_name"] = dns_name
        if description:
            payload["description"] = description
        if assigned_object:
            payload["assigned_object_type"] = "dcim.interface"
            payload["assigned_object_id"] = assigned_object["id"]
        existing = self._ips.get(address)
        if existing:
            cur_assigned = compare_value(existing.get("assigned_object")) or existing.get("assigned_object_id")
            changed = {}
            for field, value in payload.items():
                if field == "description" and existing.get("description"):
                    continue
                if field == "dns_name" and existing.get("dns_name"):
                    continue
                if field in ("assigned_object_type", "assigned_object_id"):
                    # Re-link a FLOATING IP to this interface, but never steal an
                    # IP already assigned to a different object, and never re-PATCH
                    # one already assigned to THIS interface (was perpetual churn:
                    # assigned_object_type isn't a readable top-level field, so it
                    # always looked "missing").
                    if assigned_object is None:
                        continue
                    if cur_assigned == assigned_object["id"]:
                        continue  # already correctly assigned -> no-op
                    if cur_assigned and cur_assigned != assigned_object["id"]:
                        continue  # assigned elsewhere -> don't steal
                    changed[field] = value
                    continue
                if compare_value(existing.get(field)) != value:
                    changed[field] = value
            if changed:
                LOG.info("update IP %s", address)
                updated = self.client.patch(f"ipam/ip-addresses/{existing['id']}/", changed)
                if updated:
                    self._ips[address] = updated
                    return updated
            return existing
        LOG.info("create IP %s", address)
        created = self.client.post("ipam/ip-addresses/", payload)
        if created:
            self._ips[address] = created
        return created


def infer_role(uni_device: dict[str, Any]) -> tuple[str, str]:
    features = {str(item) for item in uni_device.get("features", []) if item}
    for feature, role in ROLE_MAP.items():
        if feature in features:
            return role
    model = str(uni_device.get("model") or "").lower()
    name = str(uni_device.get("name") or "").lower()
    if any(token in model for token in ("usw", "switch")):
        return ROLE_MAP["switching"]
    if any(token in model for token in ("ap", "uap")) or "access point" in name:
        return ROLE_MAP["accessPoint"]
    if any(token in model for token in ("uxg", "udm", "usg", "gateway", "firewall")):
        return ROLE_MAP["firewall"]
    if any(token in model for token in ("server", "proxmox", "truenas")):
        return ROLE_MAP["server"]
    return ROLE_MAP["server"]


def normalize_ip(value: str | None) -> str | None:
    if not value:
        return None
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return None
    for item in PREFIXES + [UPSTREAM_PREFIX]:
        network = ipaddress.ip_network(item["prefix"])
        if ip in network:
            return str(ipaddress.ip_interface(f"{ip}/{network.prefixlen}"))
    return str(ipaddress.ip_interface(f"{ip}/32"))


def build_dns_name(name: str | None, mac: str | None, fallback_prefix: str) -> str:
    suffix = mac.replace(":", "") if mac else None
    if name:
        base = short_label(name)
        return short_label(f"{base}-{suffix}") if suffix else base
    if suffix:
        return short_label(f"{fallback_prefix}-{suffix}")
    return short_label(fallback_prefix)


def build_description(
    kind: str,
    name: str | None,
    mac: str | None,
    client_type: str | None = None,
    uplink: str | None = None,
    extra: str | None = None,
) -> str:
    parts = [kind]
    if name:
        parts.append(f"name={name}")
    if mac:
        parts.append(f"mac={mac}")
    if client_type:
        parts.append(f"type={client_type}")
    if uplink:
        parts.append(f"uplink={uplink}")
    if extra:
        parts.append(extra)
    return "; ".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Populate NetBox from UniFi snapshots")
    parser.add_argument(
        "--env-file",
        default=DEFAULT_ENV_FILE,
        help="Path to .env file containing NETBOX_URL and NETBOX_TOKEN",
    )
    parser.add_argument(
        "--devices-json",
        default=None,
        help="Path to unifi_devices.json snapshot",
    )
    parser.add_argument(
        "--clients-json",
        default=None,
        help="Path to unifi_clients.json snapshot",
    )
    parser.add_argument(
        "--wlans-json",
        default=None,
        help="Path to unifi_wlans.json snapshot",
    )
    parser.add_argument(
        "--sta-json",
        default=None,
        help="Path to unifi_sta.json snapshot (per-MAC connection facts)",
    )
    parser.add_argument(
        "--topology-json",
        default=None,
        help="Path to unifi_topology.json snapshot (switches + uplinks)",
    )
    parser.add_argument(
        "--include-upstream-prefix",
        action="store_true",
        help="Also ensure the optional 192.168.1.0/24 Upstream/WAN prefix",
    )
    parser.add_argument("--dry-run", action="store_true", help="Log changes without writing")
    parser.add_argument("--site-id", type=int, default=DEFAULT_SITE_ID)
    parser.add_argument("--site-name", default=DEFAULT_SITE_NAME)
    parser.add_argument("--site-slug", default=DEFAULT_SITE_SLUG)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(levelname)s %(message)s")

    load_env(Path(args.env_file))
    netbox_url = os.environ.get("NETBOX_URL", "").strip()
    netbox_token = os.environ.get("NETBOX_TOKEN", "").strip()
    if not netbox_url or not netbox_token:
        LOG.error("NETBOX_URL and NETBOX_TOKEN must be set via %s or the environment", args.env_file)
        return 2

    script_dir = Path(__file__).resolve().parent
    devices_path = Path(args.devices_json) if args.devices_json else script_dir / "unifi_devices.json"
    clients_path = Path(args.clients_json) if args.clients_json else script_dir / "unifi_clients.json"
    wlans_path = Path(args.wlans_json) if args.wlans_json else script_dir / "unifi_wlans.json"
    sta_path = Path(args.sta_json) if args.sta_json else script_dir / "unifi_sta.json"
    topology_path = Path(args.topology_json) if args.topology_json else script_dir / "unifi_topology.json"

    client = NetBoxClient(netbox_url, netbox_token, dry_run=args.dry_run)
    nb = NetBoxPopulation(client, dry_run=args.dry_run)

    site = nb.ensure_site(args.site_id, args.site_name, args.site_slug)
    manufacturer = nb.ensure_manufacturer("Ubiquiti")

    devices = load_snapshot(devices_path)
    clients = load_snapshot(clients_path)
    wlans = load_snapshot(wlans_path)
    sta = load_snapshot(sta_path)
    topology = json.loads(topology_path.read_text()) if topology_path.exists() else {}

    LOG.info("loaded %d UniFi devices, %d clients, %d wlans, %d sta, %d switches",
             len(devices), len(clients), len(wlans), len(sta),
             len(topology.get("switches", [])))

    device_by_unifi_id: dict[str, dict[str, Any]] = {}
    for uni_device in devices:
        model = str(uni_device.get("model") or "Unknown model")
        device_role_slug, device_role_name = infer_role(uni_device)
        role = nb.ensure_role(device_role_slug, device_role_name)
        device_type = nb.ensure_device_type(manufacturer, model, role_slug=device_role_slug)
        nb_device = nb.ensure_device(site, uni_device, device_type, role)
        if not nb_device:
            continue
        device_by_unifi_id[str(uni_device.get("id"))] = nb_device
        mgmt_interface = nb.ensure_interface(nb_device, "mgmt")
        mgmt_ip = normalize_ip(uni_device.get("ipAddress"))
        if mgmt_ip:
            existing_ip = nb._ips.get(mgmt_ip)
            ip = nb.ensure_ip(
                mgmt_ip,
                dns_name=build_dns_name(uni_device.get("name"), uni_device.get("macAddress"), "unifi"),
                description=build_description(
                    "UniFi device",
                    uni_device.get("name"),
                    uni_device.get("macAddress"),
                    extra=f"model={uni_device.get('model')} firmware={uni_device.get('firmwareVersion')}",
                ),
                assigned_object=mgmt_interface,
            )
            current_primary = compare_value(nb_device.get("primary_ip4"))
            if args.dry_run:
                target_id = compare_value(existing_ip.get("id")) if existing_ip else mgmt_ip
                if current_primary != target_id:
                    LOG.info("dry-run would set primary IP for %s to %s", nb_device.get("name"), mgmt_ip)
            elif ip and current_primary != ip.get("id"):
                LOG.info("update primary IP for %s", nb_device.get("name"))
                client.patch(f"dcim/devices/{nb_device['id']}/", {"primary_ip4": ip["id"]})

    for item in PREFIXES + ([UPSTREAM_PREFIX] if args.include_upstream_prefix else []):
        prefix = item["prefix"]
        vlan_vid = item["vlan"]
        vlan_name = item["name"]
        if vlan_vid is not None:
            nb.ensure_vlan(site, vlan_vid, vlan_name, description=f"{vlan_vid} {vlan_name}")
        nb.ensure_prefix(site, prefix, description=f"{vlan_vid} {vlan_name}" if vlan_vid is not None else vlan_name)

    # MACs modeled as wireless-client devices own their IP assignment (to wlan0);
    # skip them in this flat-IP loop so the two passes don't fight over the IP.
    wireless_macs = {
        (c.get("mac") or "").upper()
        for c in sta if not c.get("is_wired") and c.get("essid") and c.get("mac")
    }

    for client_item in clients:
        if (client_item.get("macAddress") or "").upper() in wireless_macs:
            continue
        ip_address = normalize_ip(client_item.get("ipAddress"))
        if not ip_address:
            continue
        client_type = client_item.get("type")
        uplink_id = client_item.get("uplinkDeviceId")
        uplink_device = device_by_unifi_id.get(str(uplink_id)) if uplink_id else None
        uplink_desc = uplink_device.get("name") if uplink_device else None
        if uplink_device and uplink_device.get("primary_ip4"):
            uplink_desc = f"{uplink_desc} ({uplink_device['primary_ip4']})" if uplink_desc else uplink_device["primary_ip4"]
        dns_name = build_dns_name(client_item.get("name"), client_item.get("macAddress"), "client")
        description = build_description(
            "UniFi client",
            client_item.get("name"),
            client_item.get("macAddress"),
            client_type=client_type,
            uplink=uplink_desc,
            extra=f"id={client_item.get('id')}",
        )
        try:
            nb.ensure_ip(ip_address, dns_name=dns_name, description=description)
        except requests.HTTPError as exc:
            LOG.warning("skip client IP %s: %s", ip_address, exc)

    for wlan in wlans:
        ssid = wlan.get("name")
        if not ssid:
            continue
        security = (wlan.get("security") or "").lower()
        if security in ("wpapsk", "wpa2", "wpa3"):
            auth_type = "wpa-personal"
        elif security in ("wpaeap", "wpa-eap", "radius"):
            auth_type = "wpa-enterprise"
        elif security in ("open", "none", ""):
            auth_type = "open"
        elif security == "wep":
            auth_type = "wep"
        else:
            auth_type = "wpa-personal"
        auth_cipher = "aes" if auth_type.startswith("wpa") else "auto"
        status = "active" if wlan.get("enabled", True) else "disabled"
        vlan = None
        vlan_id = wlan.get("vlan_id")
        if vlan_id:
            vlan = nb._vlans_by_vid.get(int(vlan_id))
        desc_bits = [f"wpa_mode={wlan.get('wpa_mode')}"]
        if wlan.get("network_name"):
            desc_bits.append(f"network={wlan['network_name']}")
        if wlan.get("is_guest"):
            desc_bits.append("guest")
        try:
            nb.ensure_wireless_lan(
                ssid, auth_type, auth_cipher, status, vlan=vlan,
                description="; ".join(b for b in desc_bits if b and "None" not in b),
            )
        except requests.HTTPError as exc:
            LOG.warning("skip wireless LAN %s: %s", ssid, exc)

    # Model UniFi wireless clients (phones/IoT/etc.) as lightweight NetBox devices
    # with a wireless interface, so each can be a real member of its SSID's
    # WirelessLAN. Reuses existing role/manufacturer/device-type scaffolding.
    if sta:
        wireless_clients = [c for c in sta if not c.get("is_wired") and c.get("essid")]
        if wireless_clients:
            wc_role = nb.ensure_role("wireless-client", "Wireless Client")
            generic_mfr = nb.ensure_manufacturer("Generic")
            wc_type = nb.ensure_device_type(generic_mfr, "Wireless Client")
            wlan_by_ssid = {w["ssid"]: w for w in client.list_all("wireless/wireless-lans/")}
            created_wc = 0
            processed_wc = 0
            for c in wireless_clients:
                mac = (c.get("mac") or "").upper()
                if not mac:
                    continue
                was_new = f"wifi:{mac}" not in nb._devices
                ip = c.get("ip")
                ip_cidr = normalize_ip(ip) if ip else None
                extra = "; ".join(filter(None, [
                    f"UniFi wireless client on SSID {c.get('essid')}",
                    f"vlan={c.get('vlan')}" if c.get("vlan") else None,
                    f"network={c.get('network')}" if c.get("network") else None,
                    f"ap={c.get('last_uplink_name')}" if c.get("last_uplink_name") else None,
                ]))
                try:
                    if nb.ensure_wireless_client_device(
                        site, wc_type, wc_role, mac,
                        c.get("hostname") or c.get("name") or "",
                        wlan_by_ssid.get(c.get("essid")), ip_cidr, extra=extra,
                    ):
                        processed_wc += 1
                        if was_new:
                            created_wc += 1
                except requests.HTTPError as exc:
                    LOG.warning("skip wireless client %s: %s", mac, exc)
            LOG.info("wireless clients: %d processed, %d newly created", processed_wc, created_wc)

    # Correlate UniFi connection facts to NetBox interfaces by MAC. Writes the
    # connection facts into STRUCTURED CUSTOM FIELDS (unifi_*) on the assigned
    # interface -- NOT the description -- so the Proxmox sync (which owns
    # `description = "Proxmox bridge: ..."`) and this UniFi sync never write the
    # same field. Also links the matching WirelessLAN on wireless DCIM interfaces.
    if sta:
        # Structured fields the UniFi sync OWNS on interfaces (Proxmox owns desc).
        IFACE_CTS = ["dcim.interface", "virtualization.vminterface"]
        for cf, label in (("unifi_connection", "UniFi Connection"),
                          ("unifi_switch_port", "UniFi Switch Port"),
                          ("unifi_ssid", "UniFi SSID"),
                          ("unifi_uplink", "UniFi Uplink")):
            nb.ensure_custom_field(cf, label, IFACE_CTS)
        sta_by_mac = {r["mac"].upper(): r for r in sta if r.get("mac")}
        all_macs = client.list_all("dcim/mac-addresses/")
        wlan_id_by_ssid = {
            w["ssid"]: w["id"] for w in client.list_all("wireless/wireless-lans/")
        }
        correlated = 0
        members = 0
        for macobj in all_macs:
            rec = sta_by_mac.get((macobj.get("mac_address") or "").upper())
            ao = macobj.get("assigned_object")
            ao_type = macobj.get("assigned_object_type")
            if not rec or not ao or not ao_type:
                continue
            wired = rec.get("is_wired")
            desired_cf = {
                "unifi_connection": "wired" if wired else "wireless",
                "unifi_switch_port": str(rec.get("sw_port")) if (wired and rec.get("sw_port")) else None,
                "unifi_ssid": None if wired else rec.get("essid"),
                "unifi_uplink": rec.get("uplink_name") or None,
            }
            endpoint = "dcim/interfaces/" if ao_type == "dcim.interface" else "virtualization/interfaces/"
            # The MAC list's nested assigned_object does NOT include custom_fields,
            # so fetch the interface fresh to compare (else it patches every run).
            iface_full = client.get(f"{endpoint}{ao['id']}/") or {}
            cur_cf = iface_full.get("custom_fields") or {}
            patch: dict[str, Any] = {}
            if any(compare_value(cur_cf.get(k)) != v for k, v in desired_cf.items()):
                patch["custom_fields"] = desired_cf
            # One-time migration: strip the old "| UniFi: ..." text annotations
            # that earlier versions wrote into description (now owned by Proxmox).
            existing_desc = iface_full.get("description") or ""
            cleaned = re.split(r"\s*\|?\s*UniFi:", existing_desc)[0].rstrip()
            if cleaned != existing_desc:
                patch["description"] = cleaned
            if patch:
                try:
                    client.patch(f"{endpoint}{ao['id']}/", patch)
                    correlated += 1
                except requests.HTTPError as exc:
                    LOG.warning("skip MAC correlation %s: %s", macobj.get("mac_address"), exc)

            # WirelessLAN membership: only DCIM interfaces carry wireless_lans in
            # NetBox; VMInterfaces do not. Set the interface wireless and attach
            # the WLAN. Idempotent: skip if already a member.
            if not wired and ao_type == "dcim.interface":
                wlan_id = wlan_id_by_ssid.get(rec.get("essid"))
                if wlan_id:
                    iface = client.get(f"dcim/interfaces/{ao['id']}/") or {}
                    current = [compare_value(w) for w in (iface.get("wireless_lans") or [])]
                    if wlan_id not in current:
                        patch = {"wireless_lans": current + [wlan_id]}
                        cur_type = compare_value(iface.get("type"))
                        if not str(cur_type).startswith("ieee802.11"):
                            patch["type"] = "ieee802.11ac"  # AC LR APs / client radios
                        try:
                            client.patch(f"dcim/interfaces/{ao['id']}/", patch)
                            members += 1
                        except requests.HTTPError as exc:
                            LOG.warning("skip WLAN membership %s: %s",
                                        macobj.get("mac_address"), exc)
        LOG.info("correlated %d interfaces, linked %d wireless members", correlated, members)

    # Switch ports + physical cabling. Create ports on the UniFi switch devices
    # from their port_table, then draw point-to-point cables for genuine 1:1
    # physical links only (DCIM interfaces): the host NIC uplink, AP/switch
    # uplinks. VM/LXC interfaces ride a trunk port and stay annotation-only.
    if topology.get("switches"):
        # Map UniFi switch MAC -> NetBox switch device (matched by name).
        sw_dev_by_mac: dict[str, dict[str, Any]] = {}
        sw_ports_by_mac: dict[str, dict[int, dict[str, Any]]] = {}
        for sw in topology["switches"]:
            dev = nb._devices_by_name.get(str(sw.get("name") or "").lower())
            if not dev:
                LOG.warning("switch %s not found in NetBox; skipping its ports", sw.get("name"))
                continue
            sw_dev_by_mac[sw["mac"]] = dev
            ports = {}
            for p in sw.get("ports", []):
                port_iface = nb.ensure_switch_port(
                    dev, p.get("name") or f"Port {p['idx']}",
                    media=p.get("media"), poe=p.get("poe", False),
                )
                if port_iface:
                    ports[p["idx"]] = port_iface
            sw_ports_by_mac[sw["mac"]] = ports

        cables = 0

        def cable_to_port(dev_iface, sw_mac, sw_port):
            nonlocal cables
            ports = sw_ports_by_mac.get((sw_mac or "").upper())
            if not ports or sw_port not in ports:
                return
            # Re-fetch both interfaces fresh so .cable status is current.
            a = client.get(f"dcim/interfaces/{dev_iface['id']}/")
            b = client.get(f"dcim/interfaces/{ports[sw_port]['id']}/")
            if a and b and nb.ensure_cable(a, b):
                cables += 1

        # (a) host NIC + any other DCIM interface with a wired UniFi uplink.
        if sta:
            for macobj in client.list_all("dcim/mac-addresses/"):
                if macobj.get("assigned_object_type") != "dcim.interface":
                    continue
                rec = sta_by_mac.get((macobj.get("mac_address") or "").upper())
                if not rec or not rec.get("is_wired") or not rec.get("uplink_mac"):
                    continue
                ao = macobj.get("assigned_object")
                if ao:
                    try:
                        cable_to_port(ao, rec["uplink_mac"], rec.get("sw_port"))
                    except requests.HTTPError as exc:
                        LOG.warning("skip cable for %s: %s", macobj.get("mac_address"), exc)

        # (b) AP / downstream-switch wired uplinks (device -> switch port).
        for up in topology.get("uplinks", []):
            dev = nb._devices_by_name.get(str(up.get("device_name") or "").lower())
            if not dev or not up.get("uplink_sw_mac"):
                continue
            # Use/create a generic uplink interface on the AP/switch device.
            dev_iface = nb.ensure_switch_port(dev, "uplink", media="GE")
            try:
                cable_to_port(dev_iface, up["uplink_sw_mac"], up.get("uplink_port"))
            except requests.HTTPError as exc:
                LOG.warning("skip uplink cable for %s: %s", up.get("device_name"), exc)

        LOG.info("created %d cables", cables)

    LOG.info("finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
