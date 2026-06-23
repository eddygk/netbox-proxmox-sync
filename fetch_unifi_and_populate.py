#!/usr/bin/env python3
"""Refresh UniFi snapshots and run the NetBox UniFi importer."""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

LOG = logging.getLogger("netbox-unifi-sync")


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def read_api_key(path: Path) -> str:
    key = os.environ.get("UNIFI_API_KEY", "").strip()
    if key:
        return key
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    raise RuntimeError(f"UniFi API key not found in UNIFI_API_KEY or {path}")


class UniFi:
    def __init__(self, host: str, port: int, api_key: str, verify_ssl: bool) -> None:
        self.base = f"https://{host}:{port}/proxy/network/integration"
        # The Integration API does not expose WLAN/SSID configs; the legacy
        # private REST API does (and accepts the same X-API-Key here).
        self.legacy_base = f"https://{host}:{port}/proxy/network/api"
        self.session = requests.Session()
        self.session.verify = verify_ssl
        self.session.headers.update({"X-API-Key": api_key, "Accept": "application/json"})

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        response = self.session.get(f"{self.base}/{path.lstrip('/')}", params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def legacy_get(self, path: str) -> dict[str, Any]:
        """Query the legacy private REST API (e.g. /s/default/rest/wlanconf).
        Returns {} on failure so a missing/blocked endpoint never aborts the run."""
        try:
            r = self.session.get(f"{self.legacy_base}/{path.lstrip('/')}", timeout=30)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            LOG.warning("legacy UniFi GET %s failed: %s", path, exc)
            return {}


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    path.chmod(0o600)


def snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    items = payload.get("items")
    if items is None:
        items = payload.get("data", [])
    return {
        "total": payload.get("total", payload.get("totalCount", len(items))),
        "count": payload.get("count", len(items)),
        "offset": payload.get("offset", 0),
        "items": items,
        "has_more": payload.get("has_more", False),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default="/opt/netbox-sync/.env")
    parser.add_argument("--api-key-file", default="/opt/netbox-sync/.unifi-api-key")
    parser.add_argument("--out-dir", default="/opt/netbox-sync/import-data")
    parser.add_argument("--populate-script", default="/opt/netbox-sync/populate_network.py")
    parser.add_argument("--site-id", default=os.environ.get("UNIFI_SITE_ID", ""))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(levelname)s %(message)s")
    load_env(Path(args.env_file))

    host = os.environ.get("UNIFI_HOST", "unifi.example.com").strip()
    port = int(os.environ.get("UNIFI_PORT", "11443"))
    verify_ssl = os.environ.get("UNIFI_VERIFY_SSL", "false").lower() not in {"0", "false", "no"}
    api_key = read_api_key(Path(args.api_key_file))

    unifi = UniFi(host, port, api_key, verify_ssl)
    sites = unifi.get("/v1/sites", limit=200, offset=0)
    site_id = args.site_id or os.environ.get("UNIFI_SITE_ID", "").strip()
    if not site_id:
        items = sites.get("data") or sites.get("items") or []
        if not items:
            raise RuntimeError("No UniFi sites returned")
        site_id = items[0]["id"]

    devices = unifi.get(f"/v1/sites/{site_id}/devices", limit=200, offset=0)
    clients = unifi.get(f"/v1/sites/{site_id}/clients", limit=200, offset=0)

    # WLANs/SSIDs come from the legacy REST API. Join wlanconf -> networkconf to
    # resolve each SSID's VLAN id. UniFi maps wpa_mode/security -> NetBox auth.
    legacy_site = os.environ.get("UNIFI_LEGACY_SITE", "default").strip() or "default"
    wlanconf = unifi.legacy_get(f"/s/{legacy_site}/rest/wlanconf").get("data", [])
    networkconf = unifi.legacy_get(f"/s/{legacy_site}/rest/networkconf").get("data", [])
    net_vlan = {n.get("_id"): n.get("vlan") for n in networkconf}
    net_name = {n.get("_id"): n.get("name") for n in networkconf}
    # Rich device stats (legacy): switch port_table + device uplink topology.
    dev_stat = unifi.legacy_get(f"/s/{legacy_site}/stat/device").get("data", [])
    switches = []
    dev_uplinks = []
    for d in dev_stat:
        dmac = (d.get("mac") or "").upper()
        if d.get("type") == "usw" and d.get("port_table"):
            switches.append({
                "name": d.get("name"),
                "mac": dmac,
                "model": d.get("model"),
                "ports": [
                    {"idx": p.get("port_idx"), "name": p.get("name"),
                     "media": p.get("media"), "poe": bool(p.get("port_poe"))}
                    for p in d.get("port_table", []) if p.get("port_idx")
                ],
            })
        up = d.get("uplink") or {}
        if up.get("type") == "wire" and up.get("uplink_mac"):
            dev_uplinks.append({
                "device_name": d.get("name"),
                "device_mac": dmac,
                "uplink_sw_mac": (up.get("uplink_mac") or "").upper(),
                "uplink_port": up.get("uplink_remote_port"),
            })

    # Active-client connection facts, keyed by MAC (wired/wireless, AP/switch
    # port, observed SSID + VLAN). Used to enrich NetBox interfaces by MAC.
    sta = unifi.legacy_get(f"/s/{legacy_site}/stat/sta").get("data", [])
    dev_name_by_mac = {
        (d.get("macAddress") or "").upper(): d.get("name")
        for d in (devices.get("data") or devices.get("items") or [])
    }
    sta_records = []
    for c in sta:
        mac = (c.get("mac") or "").upper()
        if not mac:
            continue
        wired = bool(c.get("is_wired"))
        uplink_mac = (c.get("sw_mac") if wired else c.get("ap_mac")) or ""
        sta_records.append({
            "mac": mac,
            "hostname": c.get("hostname") or c.get("name"),
            "is_wired": wired,
            "ip": c.get("ip") or c.get("last_ip") or c.get("fixed_ip"),
            "essid": c.get("essid"),
            "vlan": c.get("vlan"),
            "network": c.get("network"),
            "radio": c.get("radio"),
            "sw_port": c.get("sw_port"),
            "uplink_mac": uplink_mac.upper(),
            "uplink_name": dev_name_by_mac.get(uplink_mac.upper())
                or next((sw["name"] for sw in switches if sw["mac"] == uplink_mac.upper()), None),
        })

    wlans = []
    for w in wlanconf:
        ncid = w.get("networkconf_id")
        wlans.append({
            "id": w.get("_id"),
            "name": w.get("name"),
            "enabled": w.get("enabled", True),
            "security": w.get("security"),       # wpapsk / open / ...
            "wpa_mode": w.get("wpa_mode"),        # wpa2 / wpa3 / ...
            "is_guest": w.get("is_guest", False),
            "hide_ssid": w.get("hide_ssid", False),
            "vlan_id": net_vlan.get(ncid),        # None == untagged/VLAN 1
            "network_name": net_name.get(ncid),
        })

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    site_snapshot = snapshot(sites)
    device_snapshot = snapshot(devices)
    client_snapshot = snapshot(clients)
    atomic_json(out_dir / "unifi_sites.json", site_snapshot)
    atomic_json(out_dir / "unifi_devices.json", device_snapshot)
    atomic_json(out_dir / "unifi_clients.json", client_snapshot)
    atomic_json(out_dir / "unifi_wlans.json", {"items": wlans, "count": len(wlans)})
    atomic_json(out_dir / "unifi_sta.json", {"items": sta_records, "count": len(sta_records)})
    atomic_json(out_dir / "unifi_topology.json",
                {"switches": switches, "uplinks": dev_uplinks})
    LOG.info(
        "refreshed UniFi snapshots: site=%s devices=%s clients=%s wlans=%s sta=%s switches=%s",
        site_id, len(device_snapshot["items"]), len(client_snapshot["items"]),
        len(wlans), len(sta_records), len(switches),
    )

    command = [
        sys.executable,
        args.populate_script,
        "--devices-json",
        str(out_dir / "unifi_devices.json"),
        "--clients-json",
        str(out_dir / "unifi_clients.json"),
        "--wlans-json",
        str(out_dir / "unifi_wlans.json"),
        "--sta-json",
        str(out_dir / "unifi_sta.json"),
        "--topology-json",
        str(out_dir / "unifi_topology.json"),
    ]
    if args.dry_run:
        command.append("--dry-run")
    return subprocess.call(command)


if __name__ == "__main__":
    raise SystemExit(main())
