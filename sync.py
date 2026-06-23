#!/usr/bin/env python3
"""Proxmox → NetBox sync script."""
import ipaddress
import logging
import os
from typing import Dict, List

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def load_env(path: str = "/opt/netbox-sync/.env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def cfg(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def normalize_ip(ip: str) -> str:
    ip = ip.strip()
    if "/" in ip:
        return ip
    return f"{ip}/128" if ":" in ip else f"{ip}/32"


class ProxmoxClient:
    def __init__(self) -> None:
        self.host = cfg("PROXMOX_HOST", "https://proxmox.example.com:8006")
        self.node = cfg("PROXMOX_NODE", "pve")
        self.token_id = cfg("PROXMOX_TOKEN_ID", "root@pam!netbox-sync")
        self.token_secret = cfg("PROXMOX_TOKEN_SECRET")
        if not self.token_secret:
            raise RuntimeError("PROXMOX_TOKEN_SECRET is required")

        self.base = self.host.rstrip("/") + "/api2/json"
        self.session = requests.Session()
        self.session.verify = False
        self.session.headers.update(
            {"Authorization": f"PVEAPIToken={self.token_id}={self.token_secret}"}
        )

    def get(self, path: str):
        r = self.session.get(self.base + path, timeout=20)
        r.raise_for_status()
        return r.json().get("data", [])

    def list_guests(self):
        guests = []
        guests += [("qemu", g) for g in self.get(f"/nodes/{self.node}/qemu")]
        guests += [("lxc", g) for g in self.get(f"/nodes/{self.node}/lxc")]
        return sorted(guests, key=lambda x: int(x[1]["vmid"]))

    def get_config(self, guest_type: str, vmid: int):
        return self.get(f"/nodes/{self.node}/{guest_type}/{vmid}/config")

    def guest_ips(self, guest_type: str, vmid: int, cfg_data: Dict) -> List[str]:
        ips = set()
        if guest_type == "qemu":
            try:
                data = self.get(f"/nodes/{self.node}/qemu/{vmid}/agent/network-get-interfaces")
                for iface in data.get("result", []):
                    for addr in iface.get("ip-addresses", []):
                        ip = addr.get("ip-address")
                        pfx = addr.get("prefix")
                        if not ip or ip.startswith("127.") or ip == "::1" or ip.startswith("fe80:"):
                            continue
                        ips.add(f"{ip}/{pfx}" if pfx else normalize_ip(ip))
            except Exception:
                pass
            for k, v in cfg_data.items():
                if k.startswith("ipconfig") and "ip=" in str(v):
                    for part in str(v).split(","):
                        if part.startswith("ip="):
                            raw = part.split("=", 1)[1]
                            if raw.lower() != "dhcp":
                                ips.add(normalize_ip(raw))
        else:
            for k, v in cfg_data.items():
                if k.startswith("net"):
                    for part in str(v).split(","):
                        if part.startswith("ip="):
                            raw = part.split("=", 1)[1]
                            if raw.lower() != "dhcp":
                                ips.add(normalize_ip(raw))
        return sorted(ips)


class NetBoxClient:
    def __init__(self) -> None:
        self.base = cfg("NETBOX_URL", "http://localhost:8000/api").rstrip("/")
        token = cfg("NETBOX_TOKEN")
        if not token:
            raise RuntimeError("NETBOX_TOKEN is required")
        self.cluster_id = int(cfg("CLUSTER_ID", "1"))
        self.site_id = int(cfg("SITE_ID", "1"))
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Token {token}", "Content-Type": "application/json"})

    def get(self, path: str, **params):
        r = self.session.get(f"{self.base}/{path}", params=params, timeout=20)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, payload: Dict):
        r = self.session.post(f"{self.base}/{path}", json=payload, timeout=20)
        if not r.ok:
            raise RuntimeError(f"POST {path} failed: {r.status_code} {r.text}")
        return r.json()

    def patch(self, path: str, payload: Dict):
        r = self.session.patch(f"{self.base}/{path}", json=payload, timeout=20)
        if not r.ok:
            raise RuntimeError(f"PATCH {path} failed: {r.status_code} {r.text}")
        return r.json()

    def upsert_vm(self, payload: Dict) -> str:
        search = self.get("virtualization/virtual-machines/", name=payload["name"])
        full = {**payload, "cluster": self.cluster_id, "site": self.site_id}
        if search.get("count", 0) == 0:
            self.post("virtualization/virtual-machines/", full)
            return "created"

        vm = search["results"][0]
        diff = {}
        for field in ["status", "vcpus", "memory", "disk", "comments", "description"]:
            if str(vm.get(field)) != str(full[field]):
                diff[field] = full[field]
        if vm.get("cluster", {}).get("id") != self.cluster_id:
            diff["cluster"] = self.cluster_id
        if vm.get("site", {}).get("id") != self.site_id:
            diff["site"] = self.site_id

        if diff:
            self.patch(f"virtualization/virtual-machines/{vm['id']}/", diff)
            return "updated"
        return "unchanged"

    def ensure_ip(self, address: str, vm_name: str) -> str:
        found = self.get("ipam/ip-addresses/", address=address)
        if found.get("count", 0) > 0:
            return "unchanged"
        self.post("ipam/ip-addresses/", {"address": address, "description": f"Proxmox guest: {vm_name}"})
        return "created"


def guest_payload(item: Dict, cfg_data: Dict, gtype: str) -> Dict:
    vmid = int(item["vmid"])
    name = item.get("name") or f"{gtype}-{vmid}"
    status = "active" if item.get("status") == "running" else "offline"
    vcpus = int(item.get("cpus") or item.get("cores") or cfg_data.get("cores") or 1)
    mem = int((item.get("maxmem") or 0) / 1024 / 1024) or int(cfg_data.get("memory") or 0)
    disk = int(round(float((item.get("maxdisk") or 0) / 1024 / 1024 / 1024)))
    description = str(cfg_data.get("description", "") or "")[:200]
    return {
        "name": name,
        "status": status,
        "vcpus": vcpus,
        "memory": mem,
        "disk": disk,
        "description": description,
        "comments": f"VMID: {vmid}\nType: {gtype}",
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    load_env()
    px = ProxmoxClient()
    nb = NetBoxClient()

    stats = {"created": 0, "updated": 0, "unchanged": 0, "ip_created": 0, "ip_unchanged": 0}

    for gtype, item in px.list_guests():
        vmid = int(item["vmid"])
        cfg_data = px.get_config(gtype, vmid)
        payload = guest_payload(item, cfg_data, gtype)

        result = nb.upsert_vm(payload)
        stats[result] += 1

        ips = px.guest_ips(gtype, vmid, cfg_data)
        for ip in ips:
            try:
                ipaddress.ip_interface(ip)
            except ValueError:
                continue
            ip_res = nb.ensure_ip(ip, payload["name"])
            stats[f"ip_{ip_res}"] += 1

        logging.info("%s (%s/%s): %s, ips=%s", payload["name"], gtype, vmid, result, len(ips))

    logging.info(
        "Summary: vm created=%s updated=%s unchanged=%s | ip created=%s unchanged=%s",
        stats["created"], stats["updated"], stats["unchanged"], stats["ip_created"], stats["ip_unchanged"],
    )


if __name__ == "__main__":
    main()
