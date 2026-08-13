#!/usr/bin/env python3

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

try:
    import yaml
except ImportError:
    yaml = None


MAAS_COLUMNS = [
    "hostname",
    "pxe_mac",
    "bmc_ip",
    "bmc_user",
    "bmc_pass",
    "node_id",
    "sn",
    "25g",
    "25g_mode",
    "tag",
    "type",
    "power_driver",
    "power_driver_fallback",
    "boot_mode",
    "network_mode",
    "25g_apply",
    "boot_vd_name",
    "single_disk_raid_level",
    "multi_disk_raid_level",
    "boot_disk_count",
    "data_disk_raid_layout",
]

STATUS_COLUMNS = [
    "hostname",
    "sn",
    "status",
    "bmc_ip",
    "bmc_user",
    "bmc_user_id",
    "bmc_user_readback_ok",
    "bmc_access_readback_ok",
    "bmc_network_readback_ok",
    "bmc_verify_method",
    "pxe_mac",
    "iface_name",
    "pci_bus",
    "error_code",
    "message",
    "updated_at",
]

ERROR_COLUMNS = [
    "hostname",
    "sn",
    "error_code",
    "message",
    "bmc_ip",
    "pxe_mac",
    "updated_at",
]

MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_yaml(path):
    if not path:
        return {}
    if yaml is None:
        return read_simple_defaults_yaml(path)
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def clean_scalar(value):
    text = str(value or "").strip()
    if (
        (text.startswith('"') and text.endswith('"'))
        or (text.startswith("'") and text.endswith("'"))
    ):
        return text[1:-1]
    return text


def clean_optional_text(value):
    text = str(value or "").strip()
    if text in {"-", "--", "null", "None", "NONE"}:
        return ""
    return text


def primary_ip(value):
    return clean_optional_text(str(value or "").split(",", 1)[0].split("/", 1)[0])


def read_simple_defaults_yaml(path):
    data = {}
    current_section = None
    current_key = None
    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.rstrip()
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if not line.startswith(" ") and line.endswith(":"):
                current_section = line[:-1].strip()
                data[current_section] = {}
                current_key = None
                continue
            if current_section is None:
                continue
            stripped = line.strip()
            section = data[current_section]
            if stripped.startswith("- ") and current_key:
                section.setdefault(current_key, []).append(clean_scalar(stripped[2:]))
                continue
            if ":" in stripped:
                key, value = stripped.split(":", 1)
                key = key.strip()
                value = value.strip()
                current_key = key
                section[key] = [] if not value else clean_scalar(value)
    return data


def load_defaults(path):
    data = read_yaml(path)
    defaults = data.get("defaults") if isinstance(data, dict) else {}
    return defaults or {}


def load_inventory(path):
    rows = []
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            row = {key: str(value or "").strip() for key, value in raw.items()}
            if not any(row.values()):
                continue
            rows.append(row)
    return rows


def normalize_mac(value):
    text = str(value or "").strip().lower().replace("-", ":")
    if not text:
        return ""
    if ":" not in text and len(text) == 12:
        text = ":".join(text[index : index + 2] for index in range(0, 12, 2))
    parts = [part.zfill(2) for part in text.split(":") if part]
    return ":".join(parts)


def first_non_empty(*values):
    for value in values:
        if str(value or "").strip():
            return str(value).strip()
    return ""


def normalize_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on", "ok"}


def verify_remote_ipmi(ip, user, password, timeout=8, attempts=4, retry_delay=5):
    if not shutil.which("ipmitool"):
        return False, "", "ipmitool is not installed on the collector"
    if not (ip and user and password):
        return False, "", "BMC IP/user/password is incomplete"
    env = os.environ.copy()
    env["IPMI_PASSWORD"] = password
    failures = []
    for attempt in range(1, attempts + 1):
        attempt_failures = []
        for interface in ("lanplus", "lan"):
            command = [
                "ipmitool", "-I", interface, "-H", ip, "-U", user, "-E", "mc", "info",
            ]
            try:
                result = subprocess.run(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=timeout,
                    check=False,
                    env=env,
                )
            except subprocess.TimeoutExpired:
                attempt_failures.append(f"{interface}: timeout")
                continue
            except Exception as exc:
                attempt_failures.append(f"{interface}: {exc}")
                continue
            output = result.stdout.strip()
            if result.returncode == 0:
                return True, f"ipmi-{interface}", output
            attempt_failures.append(f"{interface}: {output or f'exit {result.returncode}'}")
        failures.append(f"attempt {attempt}/{attempts}: " + "; ".join(attempt_failures))
        if attempt < attempts:
            time.sleep(retry_delay)
    return False, "", "; ".join(failures)


def remote_ipmi_error(message):
    text = str(message or "").lower()
    auth_markers = ("unauthorized", "invalid user", "password", "authentication type")
    if any(marker in text for marker in auth_markers):
        return "BMC_AUTH_FAILED"
    session_markers = ("timeout", "unable to establish", "no route", "network is unreachable")
    if any(marker in text for marker in session_markers):
        return "BMC_IPMI_LAN_UNAVAILABLE"
    return "BMC_REMOTE_IPMI_FAILED"


def inventory_by_sn(rows):
    by_sn = {}
    duplicates = set()
    for row in rows:
        sn = row.get("sn", "")
        if not sn:
            continue
        if sn in by_sn:
            duplicates.add(sn)
        by_sn.setdefault(sn, row)
    return by_sn, duplicates


def load_state(path):
    state_path = Path(path)
    if not state_path.exists():
        return {"reports": {}}
    with state_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if "reports" not in data:
        data["reports"] = {}
    return data


def save_state(path, state):
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    tmp_path.replace(state_path)


def merged_config(row, defaults):
    merged = dict(defaults)
    for key, value in row.items():
        cleaned = clean_optional_text(value)
        if cleaned:
            merged[key] = cleaned
    merged.setdefault("node_id", clean_optional_text(merged.get("sn")) or "System.Embedded.1")
    return merged


def report_value(report, *keys):
    for key in keys:
        if "." not in key:
            value = report.get(key)
        else:
            current = report
            for part in key.split("."):
                if not isinstance(current, dict):
                    current = None
                    break
                current = current.get(part)
            value = current
        if value is not None and str(value).strip() != "":
            return value
    return ""


def normalize_report(report):
    hardware = report_value(report, "hardware")
    if not isinstance(hardware, dict):
        hardware = {}
    normalized = {
        "sn": str(report_value(report, "sn", "serial_number")).strip(),
        "bmc_ip": str(report_value(report, "bmc_ip", "bmc.ip")).strip(),
        "bmc_user": str(report_value(report, "bmc_user", "bmc.user")).strip(),
        "bmc_pass": str(report_value(report, "bmc_pass", "bmc.pass", "bmc.password")).strip(),
        "bmc_reachable": normalize_bool(report_value(report, "bmc_reachable", "bmc.reachable")),
        "bmc_auth_ok": normalize_bool(report_value(report, "bmc_auth_ok", "bmc.auth_ok")),
        "bmc_configured": normalize_bool(report_value(report, "bmc_configured", "bmc.configured")),
        "bmc_reused": normalize_bool(report_value(report, "bmc_reused", "bmc.reused")),
        "bmc_readback_ok": normalize_bool(report_value(report, "bmc_readback_ok", "bmc.readback_ok")),
        "bmc_user_readback_ok": normalize_bool(report_value(report, "bmc_user_readback_ok", "bmc.user_readback_ok")),
        "bmc_access_readback_ok": normalize_bool(report_value(report, "bmc_access_readback_ok", "bmc.access_readback_ok")),
        "bmc_network_readback_ok": normalize_bool(report_value(report, "bmc_network_readback_ok", "bmc.network_readback_ok")),
        "bmc_lan_access_enabled": normalize_bool(report_value(report, "bmc_lan_access_enabled", "bmc.lan_access_enabled")),
        "bmc_channel": str(report_value(report, "bmc_channel", "bmc.channel")).strip(),
        "bmc_user_id": str(report_value(report, "bmc_user_id", "bmc.user_id")).strip(),
        "bmc_access": report_value(report, "bmc_access", "bmc.access"),
        "bmc_verify_pending": normalize_bool(report_value(report, "bmc_verify_pending", "bmc.verify_pending")),
        "bmc_verify_method": str(report_value(report, "bmc_verify_method", "bmc.verify_method")).strip(),
        "bmc_verify_message": str(report_value(report, "bmc_verify_message", "bmc.verify_message")).strip(),
        "bmc_method": str(report_value(report, "bmc_method", "bmc.method")).strip(),
        "pxe_mac": normalize_mac(report_value(report, "pxe_mac", "pxe.mac")),
        "iface_name": str(report_value(report, "iface_name", "pxe.iface", "pxe.iface_name")).strip(),
        "pci_bus": str(report_value(report, "pci_bus", "pxe.pci_bus")).strip(),
        "error_code": str(report_value(report, "error_code", "error.code")).strip(),
        "message": str(report_value(report, "message", "error.message")).strip(),
        "stage1_status": str(report_value(report, "stage1_status", "status")).strip().lower(),
        "hardware": hardware,
        "updated_at": utc_now(),
    }
    return normalized


def validate_inventory(rows, defaults=None):
    defaults = defaults or {}
    errors = {}
    seen = {}
    for field, code in (
        ("sn", "SN_DUPLICATED"),
        ("hostname", "HOSTNAME_DUPLICATED"),
        ("bmc_ip", "BMC_IP_DUPLICATED"),
    ):
        seen.clear()
        for row in rows:
            value = row.get(field, "")
            if field == "bmc_ip":
                value = primary_ip(value)
            if not value:
                continue
            if value in seen:
                for duplicate in (row.get("sn", ""), seen[value].get("sn", "")):
                    if duplicate:
                        errors.setdefault(duplicate, []).append(
                            (code, f"{field} duplicated: {value}")
                        )
            else:
                seen[value] = row
    for row in rows:
        sn = row.get("sn", "")
        effective = merged_config(row, defaults)
        if not sn:
            continue
        if not effective.get("hostname"):
            errors.setdefault(sn, []).append(("HOSTNAME_REQUIRED", "hostname is required"))
        if not effective.get("bmc_ip"):
            errors.setdefault(sn, []).append(("BMC_IP_REQUIRED", "bmc_ip is required"))
        if not effective.get("bmc_user"):
            errors.setdefault(sn, []).append(("BMC_USER_REQUIRED", "bmc_user is required"))
        if not effective.get("bmc_pass"):
            errors.setdefault(sn, []).append(("BMC_PASS_REQUIRED", "bmc_pass is required"))
        if not effective.get("25g"):
            errors.setdefault(sn, []).append(("NODE_IP_REQUIRED", "25g is required"))
    return errors


def evaluate_nodes(rows, defaults, state):
    by_sn, _duplicates = inventory_by_sn(rows)
    inventory_errors = validate_inventory(rows, defaults)
    reports = state.get("reports") or {}
    nodes = []
    used_reports = set()

    pxe_owner = {}
    for sn, report in reports.items():
        mac = report.get("pxe_mac", "")
        if mac:
            pxe_owner.setdefault(mac, []).append(sn)

    for row in rows:
        inventory_sn = row.get("sn", "")
        config = merged_config(row, defaults)
        report = reports.get(inventory_sn, {}) if inventory_sn else {}
        report_key = inventory_sn if report else ""
        if not report and not inventory_sn and row.get("bmc_ip"):
            for candidate_sn, candidate_report in reports.items():
                if candidate_sn in used_reports:
                    continue
                if primary_ip(candidate_report.get("bmc_ip", "")) == primary_ip(row.get("bmc_ip", "")):
                    report = candidate_report
                    report_key = candidate_sn
                    break
        if report_key:
            used_reports.add(report_key)
        sn = inventory_sn or report_key
        errors = list(inventory_errors.get(inventory_sn, []))
        config_bmc_ip = config.get("bmc_ip", "")
        reported_bmc_ip = report.get("bmc_ip", "")
        bmc_ip = first_non_empty(reported_bmc_ip, config_bmc_ip)
        pxe_mac = report.get("pxe_mac", "")

        if not report:
            status = "inventory_pending"
        else:
            reported_status = report.get("stage1_status", "")
            if reported_status in {"bmc_configuring", "bmc_configured", "stage1_capturing"} and not report.get("error_code"):
                status = reported_status
                first_error = ("", "")
                node = {
                    "hostname": config.get("hostname", ""),
                    "sn": sn,
                    "status": status,
                    "bmc_ip": first_non_empty(report.get("bmc_ip"), config.get("bmc_ip")),
                    "bmc_user": first_non_empty(report.get("bmc_user"), config.get("bmc_user")),
                    "bmc_pass": first_non_empty(report.get("bmc_pass"), config.get("bmc_pass")),
                    "bmc_configured": report.get("bmc_configured", False),
                    "bmc_reused": report.get("bmc_reused", False),
                    "bmc_reachable": report.get("bmc_reachable", False),
                    "bmc_auth_ok": report.get("bmc_auth_ok", False),
                    "bmc_readback_ok": report.get("bmc_readback_ok", False),
                    "bmc_user_readback_ok": report.get("bmc_user_readback_ok", False),
                    "bmc_access_readback_ok": report.get("bmc_access_readback_ok", False),
                    "bmc_network_readback_ok": report.get("bmc_network_readback_ok", False),
                    "bmc_lan_access_enabled": report.get("bmc_lan_access_enabled", False),
                    "bmc_channel": report.get("bmc_channel", ""),
                    "bmc_user_id": report.get("bmc_user_id", ""),
                    "bmc_access": report.get("bmc_access", {}),
                    "bmc_verify_method": report.get("bmc_verify_method", ""),
                    "bmc_verify_message": report.get("bmc_verify_message", ""),
                    "bmc_method": report.get("bmc_method", ""),
                    "hardware": report.get("hardware", {}),
                    "node_id": config.get("node_id") or config.get("sn") or "System.Embedded.1",
                    "pxe_mac": report.get("pxe_mac", ""),
                    "iface_name": report.get("iface_name", ""),
                    "pci_bus": report.get("pci_bus", ""),
                    "25g": config.get("25g", ""),
                    "25g_mode": config.get("25g_mode", ""),
                    "tag": first_non_empty(config.get("tag"), config.get("tags")),
                    "type": config.get("type", ""),
                    "error_code": "",
                    "message": "",
                    "updated_at": report.get("updated_at", ""),
                    "errors": [],
                }
                for key in (
                    "power_driver", "power_driver_fallback", "boot_mode", "network_mode", "25g_apply",
                    "boot_vd_name", "single_disk_raid_level", "multi_disk_raid_level", "boot_disk_count",
                    "data_disk_raid_layout",
                ):
                    node[key] = config.get(key, "")
                nodes.append(node)
                continue
            if primary_ip(reported_bmc_ip) and primary_ip(reported_bmc_ip) != primary_ip(config_bmc_ip):
                errors.append(
                    (
                        "BMC_IP_MISMATCH",
                        f"reported bmc_ip={reported_bmc_ip}, expected={config_bmc_ip}",
                    )
                )
            if not report.get("bmc_reachable"):
                errors.append(("BMC_UNREACHABLE", "BMC IP is not verified as reachable"))
            if not report.get("bmc_auth_ok"):
                errors.append(("BMC_AUTH_FAILED", "BMC account is not verified"))
            if not (report.get("bmc_configured") or report.get("bmc_reused")):
                errors.append(("BMC_CONFIG_NOT_APPLIED", "BMC target configuration was not applied or reused by local IPMI/KCS"))
            if not report.get("bmc_readback_ok"):
                errors.append(("BMC_READBACK_FAILED", "BMC user/access/network local readback did not pass"))
            if not report.get("bmc_user_readback_ok"):
                errors.append(("BMC_USER_READBACK_FAILED", "BMC username was not confirmed through local IPMI/KCS"))
            if not report.get("bmc_access_readback_ok"):
                errors.append(("BMC_ACCESS_READBACK_FAILED", "BMC IPMI channel access is not administrator-enabled"))
            if not report.get("bmc_network_readback_ok"):
                errors.append(("BMC_NETWORK_READBACK_FAILED", "BMC network settings do not match the target plan"))
            if not report.get("bmc_lan_access_enabled"):
                errors.append(("BMC_IPMI_LAN_DISABLED", "BMC LAN channel was not enabled for remote IPMI"))
            if not pxe_mac:
                errors.append(("PXE_MAC_NOT_FOUND", "pxe_mac is required"))
            elif not MAC_RE.match(pxe_mac):
                errors.append(("PXE_MAC_INVALID", f"invalid pxe_mac: {pxe_mac}"))
            elif len(pxe_owner.get(pxe_mac, [])) > 1:
                errors.append(("PXE_MAC_DUPLICATED", f"pxe_mac duplicated: {pxe_mac}"))
            if report.get("error_code"):
                errors.append((report["error_code"], report.get("message", "")))

            status = "failed" if errors else "stage1_ready"

        first_error = errors[0] if errors else ("", "")
        node = {
            "hostname": config.get("hostname", ""),
            "sn": sn,
            "status": status,
            "bmc_ip": bmc_ip,
            "bmc_user": first_non_empty(report.get("bmc_user"), config.get("bmc_user")),
            "bmc_pass": first_non_empty(report.get("bmc_pass"), config.get("bmc_pass")),
            "bmc_configured": report.get("bmc_configured", False),
            "bmc_reused": report.get("bmc_reused", False),
            "bmc_reachable": report.get("bmc_reachable", False),
            "bmc_auth_ok": report.get("bmc_auth_ok", False),
            "bmc_readback_ok": report.get("bmc_readback_ok", False),
            "bmc_user_readback_ok": report.get("bmc_user_readback_ok", False),
            "bmc_access_readback_ok": report.get("bmc_access_readback_ok", False),
            "bmc_network_readback_ok": report.get("bmc_network_readback_ok", False),
            "bmc_lan_access_enabled": report.get("bmc_lan_access_enabled", False),
            "bmc_channel": report.get("bmc_channel", ""),
            "bmc_user_id": report.get("bmc_user_id", ""),
            "bmc_access": report.get("bmc_access", {}),
            "bmc_verify_method": report.get("bmc_verify_method", ""),
            "bmc_verify_message": report.get("bmc_verify_message", ""),
            "bmc_method": report.get("bmc_method", ""),
            "hardware": report.get("hardware", {}),
            "node_id": config.get("node_id") or config.get("sn") or "System.Embedded.1",
            "pxe_mac": pxe_mac,
            "iface_name": report.get("iface_name", ""),
            "pci_bus": report.get("pci_bus", ""),
            "25g": config.get("25g", ""),
            "25g_mode": config.get("25g_mode", ""),
            "tag": first_non_empty(config.get("tag"), config.get("tags")),
            "type": config.get("type", ""),
            "power_driver": config.get("power_driver", ""),
            "power_driver_fallback": config.get("power_driver_fallback", ""),
            "boot_mode": config.get("boot_mode", ""),
            "network_mode": config.get("network_mode", ""),
            "25g_apply": config.get("25g_apply", ""),
            "boot_vd_name": config.get("boot_vd_name", ""),
            "single_disk_raid_level": config.get("single_disk_raid_level", ""),
            "multi_disk_raid_level": config.get("multi_disk_raid_level", ""),
            "boot_disk_count": config.get("boot_disk_count", ""),
            "data_disk_raid_layout": config.get("data_disk_raid_layout", ""),
            "error_code": first_error[0],
            "message": first_error[1],
            "updated_at": report.get("updated_at", ""),
            "errors": errors,
        }
        nodes.append(node)

    for sn, report in reports.items():
        if sn in by_sn or sn in used_reports:
            continue
        nodes.append(
            {
                "hostname": "",
                "sn": sn,
                "status": "failed",
                "bmc_ip": report.get("bmc_ip", ""),
                "bmc_user": report.get("bmc_user", ""),
                "bmc_pass": report.get("bmc_pass", ""),
                "node_id": "",
                "pxe_mac": report.get("pxe_mac", ""),
                "iface_name": report.get("iface_name", ""),
                "pci_bus": report.get("pci_bus", ""),
                "25g": "",
                "25g_mode": "",
                "tag": "",
                "type": "",
                "power_driver": "",
                "power_driver_fallback": "",
                "boot_mode": "",
                "network_mode": "",
                "25g_apply": "",
                "boot_vd_name": "",
                "single_disk_raid_level": "",
                "multi_disk_raid_level": "",
                "boot_disk_count": "",
                "data_disk_raid_layout": "",
                "error_code": "SN_NOT_FOUND",
                "message": "reported SN is not in inventory",
                "updated_at": report.get("updated_at", ""),
                "errors": [("SN_NOT_FOUND", "reported SN is not in inventory")],
            }
        )
    return nodes


def export_files(rows, defaults, state, output_dir):
    nodes = evaluate_nodes(rows, defaults, state)
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)

    with (target / "maas.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MAAS_COLUMNS)
        writer.writeheader()
        for node in nodes:
            if node["status"] != "stage1_ready":
                continue
            writer.writerow({column: node.get(column, "") for column in MAAS_COLUMNS})

    with (target / "stage1-status.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=STATUS_COLUMNS)
        writer.writeheader()
        for node in nodes:
            writer.writerow({column: node.get(column, "") for column in STATUS_COLUMNS})

    with (target / "stage1-errors.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ERROR_COLUMNS)
        writer.writeheader()
        for node in nodes:
            for code, message in node.get("errors", []):
                writer.writerow(
                    {
                        "hostname": node.get("hostname", ""),
                        "sn": node.get("sn", ""),
                        "error_code": code,
                        "message": message,
                        "bmc_ip": node.get("bmc_ip", ""),
                        "pxe_mac": node.get("pxe_mac", ""),
                        "updated_at": node.get("updated_at", ""),
                    }
                )
    return nodes


class Stage1App:
    def __init__(self, inventory_path, defaults_path, state_path):
        self.inventory_path = inventory_path
        self.defaults_path = defaults_path
        self.state_path = state_path

    def rows(self):
        return load_inventory(self.inventory_path)

    def defaults(self):
        return load_defaults(self.defaults_path)

    def state(self):
        return load_state(self.state_path)

    def save_report(self, raw_report):
        report = normalize_report(raw_report)
        if not report["sn"]:
            return 400, {"error": "SN_REQUIRED", "message": "sn is required"}
        status, config = self.config_for_sn(report["sn"])
        is_final = not report.get("stage1_status")
        if is_final and status == 200:
            report["bmc_ip"] = primary_ip(config.get("bmc_ip"))
            report["bmc_user"] = config.get("bmc_user", "")
            report["bmc_pass"] = config.get("bmc_pass", "")
            report["bmc_reachable"] = False
            report["bmc_auth_ok"] = False
            report["bmc_verify_pending"] = False
            local_ready = (
                not report.get("error_code")
                and report.get("bmc_readback_ok")
                and report.get("bmc_user_readback_ok")
                and report.get("bmc_access_readback_ok")
                and report.get("bmc_network_readback_ok")
                and report.get("bmc_lan_access_enabled")
            )
            if local_ready:
                verified, method, verify_message = verify_remote_ipmi(
                    report["bmc_ip"], report["bmc_user"], report["bmc_pass"]
                )
                report["bmc_reachable"] = verified
                report["bmc_auth_ok"] = verified
                report["bmc_verify_method"] = method
                report["bmc_verify_message"] = verify_message
                if not verified:
                    report["error_code"] = remote_ipmi_error(verify_message)
                    report["message"] = f"collector IPMI verification failed: {verify_message}"
                else:
                    report["message"] = f"local readback and collector {method} verification passed"
        state = self.state()
        state.setdefault("reports", {})[report["sn"]] = report
        save_state(self.state_path, state)
        nodes = evaluate_nodes(self.rows(), self.defaults(), state)
        node = next((item for item in nodes if item["sn"] == report["sn"]), None)
        return 200, {"ok": True, "node": node}

    def config_for_sn(self, sn):
        rows = self.rows()
        defaults = self.defaults()
        by_sn, duplicates = inventory_by_sn(rows)
        if sn in duplicates:
            return 409, {"error": "SN_DUPLICATED", "message": f"SN duplicated: {sn}"}
        row = by_sn.get(sn)
        if not row:
            return 404, {"error": "SN_NOT_FOUND", "message": f"SN not found: {sn}"}
        return 200, merged_config(row, defaults)

    def nodes(self):
        return evaluate_nodes(self.rows(), self.defaults(), self.state())

    def reverify_bmc(self, sn):
        state = self.state()
        report = (state.get("reports") or {}).get(sn)
        if not isinstance(report, dict):
            return 404, {"error": "REPORT_NOT_FOUND", "message": f"Stage1 report not found: {sn}"}
        status, config = self.config_for_sn(sn)
        if status != 200:
            return status, config
        local_ready = all(
            normalize_bool(report.get(field))
            for field in (
                "bmc_readback_ok",
                "bmc_user_readback_ok",
                "bmc_access_readback_ok",
                "bmc_network_readback_ok",
                "bmc_lan_access_enabled",
            )
        )
        if not local_ready:
            return 409, {
                "error": "BMC_LOCAL_READBACK_REQUIRED",
                "message": "local IPMI/KCS readback must pass before remote verification",
            }
        ip = primary_ip(config.get("bmc_ip"))
        user = config.get("bmc_user", "")
        password = config.get("bmc_pass", "")
        verified, method, message = verify_remote_ipmi(ip, user, password)
        report.update(
            bmc_ip=ip,
            bmc_user=user,
            bmc_pass=password,
            bmc_reachable=verified,
            bmc_auth_ok=verified,
            bmc_verify_pending=False,
            bmc_verify_method=method,
            bmc_verify_message=message,
            updated_at=utc_now(),
        )
        if verified:
            if report.get("error_code") in {
                "BMC_UNREACHABLE", "BMC_AUTH_FAILED", "BMC_IPMI_LAN_UNAVAILABLE", "BMC_REMOTE_IPMI_FAILED",
            }:
                report["error_code"] = ""
            report["message"] = f"local readback and collector {method} verification passed"
        else:
            report["error_code"] = remote_ipmi_error(message)
            report["message"] = f"collector IPMI verification failed: {message}"
        state.setdefault("reports", {})[sn] = report
        save_state(self.state_path, state)
        node = next((item for item in self.nodes() if item.get("sn") == sn), None)
        return 200, {"ok": verified, "node": node, "method": method, "message": message}


def json_response(handler, status, payload):
    encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    handler.wfile.write(encoded)


def text_response(handler, status, text, content_type="text/plain; charset=utf-8"):
    encoded = text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    handler.wfile.write(encoded)


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/health":
                json_response(self, 200, {"ok": True, "time": utc_now()})
                return
            if path.startswith("/api/v1/config/"):
                sn = unquote(path.rsplit("/", 1)[-1])
                status, payload = app.config_for_sn(sn)
                json_response(self, status, payload)
                return
            if path == "/api/v1/nodes":
                json_response(self, 200, {"nodes": app.nodes()})
                return
            if path == "/api/v1/export/maas.csv":
                text_response(self, 200, render_csv_text(app.nodes(), MAAS_COLUMNS, ready_only=True), "text/csv; charset=utf-8")
                return
            if path == "/api/v1/export/status.csv":
                text_response(self, 200, render_csv_text(app.nodes(), STATUS_COLUMNS), "text/csv; charset=utf-8")
                return
            if path == "/api/v1/export/errors.csv":
                text_response(self, 200, render_errors_csv_text(app.nodes()), "text/csv; charset=utf-8")
                return
            json_response(self, 404, {"error": "NOT_FOUND"})

        def do_POST(self):
            if urlparse(self.path).path != "/api/v1/report":
                json_response(self, 404, {"error": "NOT_FOUND"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8")
                payload = json.loads(raw or "{}")
            except Exception as exc:
                json_response(self, 400, {"error": "BAD_JSON", "message": str(exc)})
                return
            status, response = app.save_report(payload)
            json_response(self, status, response)

    return Handler


def render_csv_text(nodes, columns, ready_only=False):
    import io

    handle = io.StringIO()
    writer = csv.DictWriter(handle, fieldnames=columns)
    writer.writeheader()
    for node in nodes:
        if ready_only and node["status"] != "stage1_ready":
            continue
        writer.writerow({column: node.get(column, "") for column in columns})
    return handle.getvalue()


def render_errors_csv_text(nodes):
    import io

    handle = io.StringIO()
    writer = csv.DictWriter(handle, fieldnames=ERROR_COLUMNS)
    writer.writeheader()
    for node in nodes:
        for code, message in node.get("errors", []):
            writer.writerow(
                {
                    "hostname": node.get("hostname", ""),
                    "sn": node.get("sn", ""),
                    "error_code": code,
                    "message": message,
                    "bmc_ip": node.get("bmc_ip", ""),
                    "pxe_mac": node.get("pxe_mac", ""),
                    "updated_at": node.get("updated_at", ""),
                }
            )
    return handle.getvalue()


def add_common_args(parser):
    parser.add_argument("--inventory", required=True, help="Stage1 inventory CSV")
    parser.add_argument("--defaults", required=True, help="Stage1 defaults YAML")
    parser.add_argument("--state", required=True, help="Persistent Stage1 state JSON")


def parse_args():
    parser = argparse.ArgumentParser(description="Stage1 diskless collector")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Run HTTP collector")
    add_common_args(serve)
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8091)

    report = subparsers.add_parser("report", help="Apply one report JSON without HTTP")
    add_common_args(report)
    report.add_argument("--report", required=True, help="Report JSON file")

    export = subparsers.add_parser("export", help="Export maas/status/errors CSV")
    add_common_args(export)
    export.add_argument("--output-dir", required=True)

    inspect = subparsers.add_parser("inspect", help="Print evaluated node states")
    add_common_args(inspect)

    reverify = subparsers.add_parser("reverify-bmc", help="Retry remote IPMI verification for one Stage1 report")
    add_common_args(reverify)
    reverify.add_argument("--sn", required=True)

    return parser.parse_args()


def main():
    args = parse_args()
    app = Stage1App(args.inventory, args.defaults, args.state)

    if args.command == "serve":
        server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
        print(f"stage1 collector listening on http://{args.host}:{args.port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nstage1 collector stopped", flush=True)
        finally:
            server.server_close()

    if args.command == "report":
        with open(args.report, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        status, response = app.save_report(payload)
        print(json.dumps(response, ensure_ascii=False, indent=2))
        raise SystemExit(0 if status < 400 else 1)

    if args.command == "export":
        nodes = export_files(app.rows(), app.defaults(), app.state(), args.output_dir)
        ready = sum(1 for item in nodes if item["status"] == "stage1_ready")
        failed = sum(1 for item in nodes if item["status"] == "failed")
        print(f"exported output_dir={args.output_dir} total={len(nodes)} ready={ready} failed={failed}")
        return

    if args.command == "inspect":
        print(json.dumps({"nodes": app.nodes()}, ensure_ascii=False, indent=2))

    if args.command == "reverify-bmc":
        status, response = app.reverify_bmc(args.sn)
        node = response.get("node") or {}
        summary = {
            "ok": response.get("ok", False),
            "sn": args.sn,
            "status": node.get("status", ""),
            "bmc_ip": node.get("bmc_ip", ""),
            "bmc_user": node.get("bmc_user", ""),
            "method": response.get("method", ""),
            "error_code": node.get("error_code") or response.get("error", ""),
            "message": node.get("message") or response.get("message", ""),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        raise SystemExit(0 if status < 400 and response.get("ok") else 1)


if __name__ == "__main__":
    main()
