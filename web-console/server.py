#!/usr/bin/env python3
import csv
import base64
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import tarfile
import threading
import time
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / "static"
SOURCES = Path(os.environ.get("MAAS_SOURCES", ROOT.parent / "MAAS-sources"))
LAB_CONFIG = Path(os.environ.get("MAAS_LAB_CONFIG", ROOT / "docs/lab/two-node-physical.local.json"))
ALLOW_MUTATION = os.environ.get("MAAS_CONSOLE_ALLOW_MUTATION") == "1"
MAAS_PROFILE = os.environ.get("MAAS_PROFILE", "admin")
DEPLOY_SERIES = os.environ.get("DEPLOY_SERIES", "jammy")
DEPLOY_OSYSTEM = os.environ.get("DEPLOY_OSYSTEM", "ubuntu")
DEPLOY_POLICY = os.environ.get("DEPLOY_POLICY", "")
WIPE_SCRIPT_NAME = os.environ.get("MAAS_WIPE_SCRIPT", "wipe-raid-and-disks-test")
FLOW_TAG = os.environ.get("MAAS_FLOW_TAG", "physical-test")
POLICY_FILES = {
    "deploy_policy": ROOT / "docs/cloud-init/deploy-policy.yaml",
    "default_user_data": ROOT / "docs/cloud-init/default-user-data.yaml",
}
AUTOMATION_ROOT = SOURCES / "ansible"
ANSIBLE_BUNDLES = AUTOMATION_ROOT / "bundles"
AUTOMATION_JOBS = ROOT / ".tmp" / "automation-jobs"
COMPLIANCE_STATE = ROOT / ".tmp" / "compliance-state.json"
CONNECTIVITY_STATE = ROOT / ".tmp" / "connectivity-state.json"
JOB_LOCK = threading.Lock()
CONNECTIVITY_LOCK = threading.RLock()
CONNECTIVITY_ACTIVE = set()


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def deep_merge(base, override):
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            merged[key] = deep_merge(merged.get(key), value)
        return merged
    if override is None:
        return base
    return override


def config_defaults():
    return {
        "lab_name": "two-node-physical",
        "inventory": {
            "uploaded_csv": "",
        },
        "defaults": {
            "bmc_user": "",
            "bmc_pass": "",
            "bmc_prefix": 24,
            "bmc_gateway": "192.168.2.254",
            "node_prefix": 24,
            "node_gateway": "",
            "power_driver": "ipmi",
            "power_driver_fallback": "",
            "boot_mode": "uefi",
        },
        "server": {
            "hostname": "maas-server-1",
            "role": "server",
            "os_user": "ubuntu",
            "os_password": "",
            "external_ip": "192.168.2.200",
            "stage1_server_ip": "",
            "stage1_uefi_ipxe_source": "ipxe.efi",
            "maas_url": "http://192.168.2.200:5240/MAAS",
            "offline_root": "/srv/maas-offline",
            "http_port": 8083,
            "stage1_port": 8091,
            "admin_user": "admin",
            "admin_password": "",
            "dhcp_interface": "",
            "dhcp_range": "",
            "dhcp_router": "",
            "dhcp_dns": "",
        },
        "clients": [],
        "known_bmcs": [],
        "console": {
            "flow_tag": FLOW_TAG,
            "deploy_policy": DEPLOY_POLICY,
            "wipe_script_name": WIPE_SCRIPT_NAME,
            "default_node_id": "",
            "default_client_tag": "physical-test,no-gpu",
            "deploy_osystem": DEPLOY_OSYSTEM,
            "deploy_series": DEPLOY_SERIES,
        },
        "raid": {
            "tools_base_url": "",
            "boot_vd_name": "ssd01",
            "single_disk_raid_level": "r0",
            "multi_disk_raid_level": "r1",
            "boot_disk_count": 2,
            "data_disk_raid_layout": [],
        },
        "node_types": {
            "default": {
                "match_tags": [],
                "networking": {
                    "mode": "bond25g",
                    "apply_on_first_boot": True,
                    "bond25g": {
                        "bond_name": "bond0",
                    },
                },
                "raid": {
                    "boot_vd_name": "ssd01",
                    "single_disk_raid_level": "r0",
                    "multi_disk_raid_level": "r1",
                    "boot_disk_count": 2,
                    "data_disk_raid_layout": [],
                },
            },
            "gpu": {
                "match_tags": ["gpu", "a800", "h100"],
            },
            "cpu": {
                "match_tags": ["cpu"],
                "networking": {
                    "mode": "single_nic",
                    "single_nic": {
                        "interface_name": "eno4",
                    },
                },
                "raid": {
                    "data_disk_raid_layout": [
                        {"name": "data01", "raid_level": "r1", "disk_count": 2},
                        {"name": "data02", "raid_level": "r1", "disk_count": 2},
                    ],
                },
            },
        },
    }


def normalize_config(config):
    merged = deep_merge(config_defaults(), config or {})
    inventory = merged.get("inventory") or {}
    defaults = merged.get("defaults") or {}
    server = merged.get("server") or {}
    raid = merged.get("raid") or {}
    console = deep_merge(config_defaults()["console"], merged.get("console") or {})
    node_types = deep_merge(config_defaults()["node_types"], merged.get("node_types") or {})
    client_template = {
        "hostname": "",
        "role": "client",
        "os_user": "ubuntu",
        "os_password": "",
        "external_ip": "",
        "bmc_ip": "",
        "bmc_user": defaults.get("bmc_user") or config_defaults()["defaults"]["bmc_user"],
        "bmc_pass": defaults.get("bmc_pass") or config_defaults()["defaults"]["bmc_pass"],
        "node_id": console.get("default_node_id") or config_defaults()["console"]["default_node_id"],
        "sn": "",
        "pxe_mac": "",
        "type": "",
        "power_driver": defaults.get("power_driver") or config_defaults()["defaults"]["power_driver"],
        "power_driver_fallback": defaults.get("power_driver_fallback") or config_defaults()["defaults"]["power_driver_fallback"],
        "boot_mode": defaults.get("boot_mode") or config_defaults()["defaults"]["boot_mode"],
        "tag": console.get("default_client_tag") or config_defaults()["console"]["default_client_tag"],
    }
    for key in ("http_port", "stage1_port"):
        try:
            server[key] = int(server.get(key) or config_defaults()["server"][key])
        except Exception:
            server[key] = config_defaults()["server"][key]
    for key in ("bmc_prefix", "node_prefix"):
        try:
            defaults[key] = int(defaults.get(key) or config_defaults()["defaults"][key])
        except Exception:
            defaults[key] = config_defaults()["defaults"][key]
    try:
        raid["boot_disk_count"] = max(1, int(raid.get("boot_disk_count") or 2))
    except Exception:
        raid["boot_disk_count"] = 2
    if not isinstance(raid.get("data_disk_raid_layout"), list):
        raid["data_disk_raid_layout"] = list(config_defaults()["raid"]["data_disk_raid_layout"])
    if not isinstance(inventory.get("uploaded_csv"), str):
        inventory["uploaded_csv"] = str(inventory.get("uploaded_csv") or "")
    merged["server"] = server
    merged["inventory"] = inventory
    merged["defaults"] = defaults
    merged["raid"] = raid
    merged["console"] = console
    merged["clients"] = [deep_merge(client_template, item or {}) for item in list(merged.get("clients") or [])]
    merged["known_bmcs"] = [dict(item or {}) for item in list(merged.get("known_bmcs") or [])]
    merged["node_types"] = node_types
    return merged


def derive_tools_base_url(config):
    raid = config.get("raid") or {}
    if raid.get("tools_base_url"):
        return str(raid.get("tools_base_url")).rstrip("/")
    server = config.get("server") or {}
    http_port = server.get("http_port") or 8083
    for host in (
        server.get("stage1_server_ip"),
        server.get("diskless_server_ip"),
        server.get("external_ip"),
    ):
        host = primary_ip(host)
        if host:
            return f"http://{host}:{http_port}/tools"
    return f"http://127.0.0.1:{http_port}/tools"


def clean_optional_text(value):
    text = str(value or "").strip()
    if text in {"-", "--", "null", "None", "NONE"}:
        return ""
    return text


def normalize_tag_text(value):
    tags = []
    seen = set()
    for item in str(value or "").replace(";", ",").split(","):
        tag = clean_optional_text(item)
        lowered = tag.lower()
        if not tag or lowered in seen:
            continue
        seen.add(lowered)
        tags.append(tag)
    return ",".join(tags)


def normalize_boot_mode(value):
    text = clean_optional_text(value).lower()
    if text in {"bios", "legacy", "legacy boot"}:
        return "bios"
    if text in {"uefi", "efi", "efi boot"}:
        return "uefi"
    return "uefi"


def normalize_power_driver(value, default="ipmi"):
    text = clean_optional_text(value).lower()
    if text in {"", "auto"}:
        return default
    if text in {"ipmi", "redfish"}:
        return text
    return default


def normalize_network_mode(value, default="bond25g"):
    text = clean_optional_text(value).lower().replace("-", "_")
    if text in {"bond25g", "bond_25g", "bond"}:
        return "bond25g"
    if text in {"single_nic", "single", "nobond", "no_bond"}:
        return "single_nic"
    return default


def normalize_bool_text(value, default=True):
    text = clean_optional_text(value).lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "on", "ok"}


def normalize_ip_with_defaults(value, default_prefix="", default_gateway=""):
    text = clean_optional_text(value)
    if not text:
        return ""
    parts = [part.strip() for part in text.split(",")]
    address = parts[0] if parts else ""
    gateway = parts[1] if len(parts) > 1 else ""
    if address and "/" not in address and default_prefix:
        address = f"{address}/{default_prefix}"
    if not gateway and default_gateway:
        gateway = str(default_gateway).strip()
    return ",".join(part for part in (address, gateway) if part)


def primary_ip(value):
    return clean_optional_text(str(value or "").split(",", 1)[0].split("/", 1)[0])


def parse_uploaded_csv(text):
    content = str(text or "").strip()
    if not content:
        return []
    import io

    handle = io.StringIO(content)
    reader = csv.DictReader(handle)
    rows = []
    for raw in reader:
        row = {}
        for key, value in (raw or {}).items():
            if key is None:
                continue
            row[str(key).strip()] = clean_optional_text(value)
        if any(row.values()):
            rows.append(row)
    return rows


def csv_from_rows(rows, fieldnames=None):
    rows = [dict(row or {}) for row in list(rows or [])]
    if not rows:
        return ""
    import io

    headers = []
    for name in list(fieldnames or []):
        if name and name not in headers:
            headers.append(name)
    for row in rows:
        for name in row.keys():
            if name and name not in headers:
                headers.append(name)
    handle = io.StringIO()
    writer = csv.DictWriter(handle, fieldnames=headers, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in headers})
    return handle.getvalue().strip()


def client_match_key(item):
    for key in ("sn", "hostname", "bmc_ip", "external_ip"):
        value = clean_optional_text(item.get(key))
        if value:
            return f"{key}:{value.lower()}"
    return ""


def node_identity_values(item):
    values = set()
    for key in ("node_key", "hostname", "sn", "system_id", "maas_system_id", "node_id", "pxe_mac"):
        value = clean_optional_text((item or {}).get(key))
        if value:
            values.add(value.lower())
    for key in ("bmc_ip", "external_ip", "25g"):
        value = clean_optional_text((item or {}).get(key))
        if value:
            values.add(value.lower())
            primary = primary_ip(value)
            if primary:
                values.add(primary.lower())
    return values


def config_item_matches_node(item, node):
    node_values = node_identity_values(node)
    item_values = node_identity_values(item)
    return bool(node_values & item_values)


def remove_nodes_from_lab_config(nodes, dry=False):
    config = raw_lab_config()
    selected = list(nodes or [])
    removed = {node.get("node_key"): {"clients": 0, "uploaded_csv": 0} for node in selected}

    kept_clients = []
    for client in list(config.get("clients") or []):
        matched_key = ""
        for node in selected:
            if config_item_matches_node(client, node):
                matched_key = node.get("node_key")
                break
        if matched_key:
            removed.setdefault(matched_key, {"clients": 0, "uploaded_csv": 0})["clients"] += 1
        else:
            kept_clients.append(client)

    inventory = dict(config.get("inventory") or {})
    uploaded_text = inventory.get("uploaded_csv") or ""
    uploaded_rows = parse_uploaded_csv(uploaded_text)
    fieldnames = []
    if clean_optional_text(uploaded_text):
        import io

        reader = csv.DictReader(io.StringIO(str(uploaded_text).strip()))
        fieldnames = list(reader.fieldnames or [])

    kept_rows = []
    for row in uploaded_rows:
        matched_key = ""
        for node in selected:
            if config_item_matches_node(row, node):
                matched_key = node.get("node_key")
                break
        if matched_key:
            removed.setdefault(matched_key, {"clients": 0, "uploaded_csv": 0})["uploaded_csv"] += 1
        else:
            kept_rows.append(row)

    total_removed = sum(item["clients"] + item["uploaded_csv"] for item in removed.values())
    if total_removed and not dry:
        config["clients"] = kept_clients
        inventory["uploaded_csv"] = csv_from_rows(kept_rows, fieldnames)
        config["inventory"] = inventory
        write_lab_config(config)
    return removed


def remove_nodes_from_stage1_runtime(nodes, dry=False):
    selected = list(nodes or [])
    result = {"inventory": 0, "reports": 0, "exports": 0}
    inventory_path = SOURCES / "stage1/inventory.csv"
    if inventory_path.exists():
        with inventory_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = list(reader.fieldnames or [])
            rows = list(reader)
        kept = [row for row in rows if not any(config_item_matches_node(row, node) for node in selected)]
        result["inventory"] = len(rows) - len(kept)
        if result["inventory"] and not dry:
            import io

            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(kept)
            write_text_atomic(inventory_path, output.getvalue())

    state_path = SOURCES / "stage1/state.json"
    state = read_json(state_path, {"reports": {}})
    reports = dict(state.get("reports") or {})
    kept_reports = {}
    for sn, report in reports.items():
        candidate = {**dict(report or {}), "sn": sn}
        if any(config_item_matches_node(candidate, node) for node in selected):
            result["reports"] += 1
        else:
            kept_reports[sn] = report
    if result["reports"] and not dry:
        state["reports"] = kept_reports
        write_text_atomic(state_path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")

    output_dir = SOURCES / "stage1/output"
    for name in ("maas.csv", "stage1-status.csv", "stage1-errors.csv"):
        path = output_dir / name
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = list(reader.fieldnames or [])
            rows = list(reader)
        kept = [row for row in rows if not any(config_item_matches_node(row, node) for node in selected)]
        removed = len(rows) - len(kept)
        result["exports"] += removed
        if removed and not dry:
            import io

            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(kept)
            write_text_atomic(path, output.getvalue())
    return result


def is_legacy_placeholder_client(item):
    client = dict(item or {})
    return (
        clean_optional_text(client.get("hostname")) == "node-physical-2"
        and not clean_optional_text(client.get("sn"))
        and not clean_optional_text(client.get("25g"))
        and not clean_optional_text(client.get("pxe_mac"))
    )


def merge_client_sources(config):
    merged = {}
    ordered = []
    uploaded_rows = parse_uploaded_csv((config.get("inventory") or {}).get("uploaded_csv"))
    client_sources = list(config.get("clients") or [])
    if uploaded_rows:
        client_sources = [item for item in client_sources if not is_legacy_placeholder_client(item)]
    for source in client_sources + uploaded_rows:
        item = {str(key): value for key, value in dict(source or {}).items()}
        key = client_match_key(item) or f"row:{len(ordered)}"
        if key not in merged:
            merged[key] = {}
            ordered.append(key)
        merged[key] = deep_merge(merged[key], item)
    return [merged[key] for key in ordered]


def determine_node_type(client, node_types):
    explicit = clean_optional_text(client.get("type"))
    if explicit and explicit in node_types:
        return explicit
    tags = {item.strip().lower() for item in normalize_tag_text(client.get("tag")).split(",") if item.strip()}
    for name, policy in node_types.items():
        if name == "default":
            continue
        match_tags = {str(item).strip().lower() for item in list((policy or {}).get("match_tags") or []) if str(item).strip()}
        if tags & match_tags:
            return name
    return "default"


def parse_data_raid_layout(value):
    if isinstance(value, list):
        return [dict(item or {}) for item in value]
    text = clean_optional_text(value)
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except Exception:
        return []
    return [dict(item or {}) for item in parsed] if isinstance(parsed, list) else []


def resolved_node_id(raw_value, sn="", fallback=""):
    value = clean_optional_text(raw_value)
    if value:
        return value
    sn_value = clean_optional_text(sn)
    if sn_value:
        return sn_value
    return clean_optional_text(fallback) or "System.Embedded.1"


def compose_clients(config):
    defaults = config.get("defaults") or {}
    node_types = config.get("node_types") or {}
    global_raid = config.get("raid") or {}
    console = config.get("console") or {}
    results = []
    for raw in merge_client_sources(config):
        node_type = determine_node_type(raw, node_types)
        base_policy = deep_merge(node_types.get("default") or {}, node_types.get(node_type) or {})
        client = deep_merge(
            {
                "node_id": console.get("default_node_id") or "",
                "tag": normalize_tag_text(raw.get("tag") or console.get("default_client_tag") or ""),
                "bmc_user": defaults.get("bmc_user") or "",
                "bmc_pass": defaults.get("bmc_pass") or "",
                "power_driver": defaults.get("power_driver") or "ipmi",
                "power_driver_fallback": defaults.get("power_driver_fallback") or "",
                "boot_mode": defaults.get("boot_mode") or "uefi",
                "type": node_type,
            },
            raw,
        )
        client["tag"] = normalize_tag_text(client.get("tag"))
        if node_type != "default" and node_type.lower() not in {item.strip().lower() for item in client["tag"].split(",") if item.strip()}:
            client["tag"] = normalize_tag_text(",".join([client["tag"], node_type]))
        client["type"] = node_type
        client["bmc_ip"] = normalize_ip_with_defaults(client.get("bmc_ip"), defaults.get("bmc_prefix"), defaults.get("bmc_gateway"))
        client["25g"] = normalize_ip_with_defaults(client.get("25g"), defaults.get("node_prefix"), defaults.get("node_gateway"))
        client["bmc_user"] = clean_optional_text(client.get("bmc_user")) or defaults.get("bmc_user") or ""
        client["bmc_pass"] = clean_optional_text(client.get("bmc_pass")) or defaults.get("bmc_pass") or ""
        client["node_id"] = resolved_node_id(client.get("node_id"), client.get("sn"), console.get("default_node_id"))
        client["power_driver"] = normalize_power_driver(client.get("power_driver"), defaults.get("power_driver") or "ipmi")
        client["power_driver_fallback"] = normalize_power_driver(client.get("power_driver_fallback"), defaults.get("power_driver_fallback") or "")
        client["boot_mode"] = normalize_boot_mode(client.get("boot_mode") or defaults.get("boot_mode"))
        client["networking"] = deep_merge((node_types.get("default") or {}).get("networking") or {}, (base_policy.get("networking") or {}))
        explicit_network_mode = clean_optional_text(client.get("network_mode"))
        if explicit_network_mode:
            client["networking"]["mode"] = normalize_network_mode(
                explicit_network_mode,
                (client.get("networking") or {}).get("mode") or "bond25g",
            )
        if clean_optional_text(client.get("25g_apply")):
            client["networking"]["apply_on_first_boot"] = normalize_bool_text(client.get("25g_apply"), default=True)
        client["raid"] = deep_merge(global_raid, base_policy.get("raid") or {})
        if clean_optional_text(client.get("boot_vd_name")):
            client["raid"]["boot_vd_name"] = client.get("boot_vd_name")
        if clean_optional_text(client.get("single_disk_raid_level")):
            client["raid"]["single_disk_raid_level"] = client.get("single_disk_raid_level")
        if clean_optional_text(client.get("multi_disk_raid_level")):
            client["raid"]["multi_disk_raid_level"] = client.get("multi_disk_raid_level")
        if clean_optional_text(client.get("boot_disk_count")):
            try:
                client["raid"]["boot_disk_count"] = max(1, int(client.get("boot_disk_count")))
            except Exception:
                pass
        if clean_optional_text(client.get("data_disk_raid_layout")):
            client["raid"]["data_disk_raid_layout"] = parse_data_raid_layout(client.get("data_disk_raid_layout"))
        results.append(client)
    return results


def validate_clients(clients):
    errors = []
    if not clients:
        return ["未配置任何节点，请先上传节点清单或填写客户端配置。"]
    for client in clients:
        name = client.get("hostname") or client.get("sn") or "未命名节点"
        missing = []
        for field in ("hostname", "sn", "bmc_ip", "bmc_user", "bmc_pass"):
            if not clean_optional_text(client.get(field)):
                missing.append(field)
        if not clean_optional_text(client.get("25g")):
            missing.append("25g")
        if missing:
            errors.append(f"{name}: 缺少 {', '.join(missing)}")
    return errors


def validate_lab_config(config, clients):
    errors = []
    uploaded_csv = clean_optional_text(((config.get("inventory") or {}).get("uploaded_csv")))
    if not uploaded_csv:
        errors.append("未上传节点清单 CSV，请先在配置中心粘贴节点清单；节点 IP、BMC、tag 等信息不能再依赖示例配置。")
    errors.extend(validate_clients(clients))
    return errors


def read_inventory():
    path = SOURCES / "stage1" / "inventory.csv"
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def read_stage1_state():
    return read_json(SOURCES / "stage1" / "state.json", {"reports": {}})


def storage_state_path():
    return ROOT / ".tmp" / "storage-state.json"


def read_storage_state():
    return read_json(storage_state_path(), {"nodes": {}})


def workflow_state_path():
    return ROOT / ".tmp" / "workflow-state.json"


def read_workflow_state():
    return read_json(workflow_state_path(), {"checks": {}})


def workflow_check_ok(name):
    return bool(((read_workflow_state().get("checks") or {}).get(name) or {}).get("ok"))


def record_workflow_check(name, result):
    state = read_workflow_state()
    checks = state.setdefault("checks", {})
    checks[name] = {
        "ok": bool(result.get("ok")),
        "updated_at": int(time.time()),
        "output": sanitize_text((result.get("output") or result.get("error") or "")[-2000:]),
    }
    write_text_atomic(workflow_state_path(), json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def invalidate_workflow_check(name):
    state = read_workflow_state()
    checks = state.setdefault("checks", {})
    if name in checks:
        checks.pop(name, None)
        write_text_atomic(workflow_state_path(), json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def write_storage_state(state):
    write_text_atomic(storage_state_path(), json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def storage_state_for_node(node):
    system_id = str(node.get("system_id") or node.get("maas_system_id") or "").strip()
    if not system_id:
        return {}
    return ((read_storage_state().get("nodes") or {}).get(system_id) or {})


def clear_storage_state(node):
    system_id = str(node.get("system_id") or node.get("maas_system_id") or "").strip()
    if not system_id:
        return
    state = read_storage_state()
    nodes = state.setdefault("nodes", {})
    if system_id in nodes:
        nodes.pop(system_id, None)
        write_storage_state(state)


def update_storage_state(node, ok, output="", policy=""):
    system_id = str(node.get("system_id") or node.get("maas_system_id") or "").strip()
    if not system_id:
        return
    state = read_storage_state()
    nodes = state.setdefault("nodes", {})
    nodes[system_id] = {
        "ok": bool(ok),
        "policy": policy,
        "updated_at": int(time.time()),
        "output": sanitize_text(output[-2000:]),
    }
    write_storage_state(state)


def clear_storage_state_by_system_id(system_id):
    target = str(system_id or "").strip()
    if not target:
        return
    state = read_storage_state()
    nodes = state.setdefault("nodes", {})
    if target in nodes:
        nodes.pop(target, None)
        write_storage_state(state)


def clear_node_runtime_cache(node):
    """Remove console-only state after a node is deleted."""
    keys = {
        str(value or "").strip()
        for value in (
            node.get("node_key"),
            node.get("system_id"),
            node.get("maas_system_id"),
        )
        if str(value or "").strip()
    }
    for path in (CONNECTIVITY_STATE, COMPLIANCE_STATE):
        state = read_json(path, {"nodes": {}})
        nodes = state.get("nodes") or {}
        changed = False
        for key in keys:
            if key in nodes:
                nodes.pop(key, None)
                changed = True
        if changed:
            state["nodes"] = nodes
            write_text_atomic(path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def read_lab_config():
    return normalize_config(read_json(LAB_CONFIG, {}))


def raw_lab_config():
    return read_json(LAB_CONFIG, {})


def write_lab_config(config):
    write_text_atomic(LAB_CONFIG, json.dumps(normalize_config(config), ensure_ascii=False, indent=2) + "\n")
    invalidate_workflow_check("network-check")


def lab_console():
    return read_lab_config().get("console") or {}


def lab_raid():
    config = read_lab_config()
    raid = dict(config.get("raid") or {})
    raid["tools_base_url"] = derive_tools_base_url(config)
    return raid


def effective_lab_config():
    config = read_lab_config()
    merged = deep_merge({}, config)
    merged["raid"] = lab_raid()
    merged["clients"] = compose_clients(config)
    merged["validation_errors"] = validate_lab_config(config, merged["clients"])
    return merged


def config_payload():
    effective = effective_lab_config()
    return {
        "ok": True,
        "path": str(LAB_CONFIG),
        "defaults": config_defaults(),
        "current": read_lab_config(),
        "effective": effective,
        "validation_errors": effective.get("validation_errors", []),
        "policy_files": {
            name: {"path": str(path), "content": path.read_text(encoding="utf-8") if path.exists() else ""}
            for name, path in POLICY_FILES.items()
        },
    }


def validate_and_write_policy_files(policy_files):
    if not isinstance(policy_files, dict):
        return
    try:
        import yaml
    except ImportError as exc:
        raise ValueError("缺少 python3-yaml，无法安全校验策略 YAML") from exc
    parsed = {}
    for name, path in POLICY_FILES.items():
        if name not in policy_files:
            continue
        item = policy_files.get(name)
        content = item.get("content", "") if isinstance(item, dict) else str(item or "")
        try:
            document = yaml.safe_load(content)
        except Exception as exc:
            raise ValueError(f"{path.name} YAML 格式错误: {exc}") from exc
        if not isinstance(document, dict):
            raise ValueError(f"{path.name} 必须是 YAML 对象")
        if name == "deploy_policy" and not document.get("policies"):
            raise ValueError("deploy-policy.yaml 缺少 policies")
        parsed[path] = content
    for path, content in parsed.items():
        write_text_atomic(path, content.rstrip() + "\n")


def is_sensitive_key(key):
    lowered = str(key or "").lower()
    return (
        "password" in lowered
        or lowered in {"pass", "secret", "token", "apikey", "api_key"}
        or lowered.endswith(("_pass", "_secret", "_token", "_apikey", "_api_key"))
    )


def redact_config(value):
    if isinstance(value, list):
        return [redact_config(item) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if is_sensitive_key(key):
                result[key] = "******" if str(item or "") else ""
            else:
                result[key] = redact_config(item)
        return result
    return value


def sensitive_values():
    values = []

    def collect(item):
        if isinstance(item, dict):
            for key, value in item.items():
                if is_sensitive_key(key):
                    if value:
                        values.append(str(value))
                else:
                    collect(value)
        elif isinstance(item, list):
            for value in item:
                collect(value)

    collect(read_lab_config())
    return sorted(set(values), key=len, reverse=True)


def sanitize_text(text):
    sanitized = str(text or "")
    for value in sensitive_values():
        sanitized = sanitized.replace(value, "******")
    return sanitized


def systemctl_active(unit):
    try:
        result = subprocess.run(
            ["systemctl", "is-active", unit],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.stdout.strip() or "unknown"
    except FileNotFoundError:
        return "unavailable"


def service_status():
    units = [
        "maas-offline-http.service",
        "stage1-collector.service",
        "diskless-stage1-dnsmasq.service",
        "maas-http.service",
        "maas-regiond.service",
        "maas-rackd.service",
        "maas-dhcpd.service",
        "maas-dhcpd6.service",
    ]
    return {unit: systemctl_active(unit) for unit in units}


def source_status():
    checks = {
        "boot_stream": SOURCES / "mirror/ephemeral-v3/stable/streams/v1/index.sjson",
        "ubuntu_jammy_repo": SOURCES / "iso/dists/jammy/Release",
        "lldpd_repo": SOURCES / "tools/lldpd-mini-repo/dists/jammy/Release",
        "maas_control_repo": SOURCES / "tools/maas-control-repo/dists/jammy/main/binary-amd64/Packages.gz",
        "stage1_inventory": SOURCES / "stage1/inventory.csv",
        "stage1_defaults": SOURCES / "stage1/defaults.yaml",
        "stage1_state": SOURCES / "stage1/state.json",
        "ansible_checksums": SOURCES / "ansible/runtime/debs/SHA256SUMS",
    }
    status = {name: path.exists() for name, path in checks.items()}
    status["ansible_runtime"] = len(list((SOURCES / "ansible/runtime/debs").glob("*.deb"))) >= 4
    return status


def required_sources_ok(sources, capability="core"):
    required = {
        "core": ("boot_stream", "ubuntu_jammy_repo", "lldpd_repo", "maas_control_repo"),
        "maas": ("boot_stream", "ubuntu_jammy_repo", "lldpd_repo", "maas_control_repo"),
        "stage1": ("ubuntu_jammy_repo", "stage1_inventory", "stage1_defaults", "stage1_state"),
    }
    names = required.get(capability, required["core"])
    return all(bool(sources.get(name)) for name in names)


def normalize_mac(value):
    return str(value or "").strip().lower().replace("-", ":")


def maas_status_value(machine):
    return str(machine.get("status_name") or machine.get("status") or "").strip()


def maas_status_key(machine):
    return maas_status_value(machine).lower()


PIPELINE_STAGES = [
    {
        "id": "sources",
        "label": "离线资源",
        "description": "校验 MAAS、Ubuntu 22.04、Stage1、工具仓库资源",
        "actions": ["validate-sources"],
    },
    {
        "id": "network",
        "label": "网络校验",
        "description": "检查管理网、装机网、DHCP/TFTP 监听和关键地址冲突",
        "actions": ["network-check"],
    },
    {
        "id": "control_plane",
        "label": "服务部署",
        "description": "离线部署 MAAS 服务端和 Stage1 无盘服务",
        "actions": ["install-maas", "install-diskless"],
    },
    {
        "id": "node_plan",
        "label": "节点规划",
        "description": "上传 SN、目标 BMC 配置、目标业务网络和节点类型",
        "actions": ["sync-lab-stage1"],
    },
    {
        "id": "bmc_config",
        "label": "BMC 配置",
        "description": "无盘环境通过本机 IPMI/KCS 写入或复用目标 BMC 配置并回读验证",
        "actions": ["pxe-diskless"],
    },
    {
        "id": "stage1_capture",
        "label": "抓配合并",
        "description": "采集 PXE MAC 和硬件，与人工规划合并生成 MAAS 导入数据",
        "actions": ["export-stage1"],
    },
    {
        "id": "maas_import",
        "label": "导入 MAAS",
        "description": "生成导入表，注册 BMC 电源与节点标签",
        "actions": ["pxe-maas", "import-nodes"],
    },
    {
        "id": "commissioning",
        "label": "硬件盘点",
        "description": "MAAS Commissioning 发现网卡、磁盘、硬件信息",
        "actions": ["recommission"],
    },
    {
        "id": "storage_prepare",
        "label": "存储准备",
        "description": "清盘、清理旧 RAID，并按节点策略创建 RAID、启动盘和分区",
        "actions": ["register-wipe-script", "wipe", "apply-storage"],
    },
    {
        "id": "deploy",
        "label": "系统部署",
        "description": "部署 Ubuntu 22.04，注入 cloud-init 和首启策略",
        "actions": ["deploy"],
    },
    {
        "id": "verify",
        "label": "网络与测试",
        "description": "控制节点自动验证业务 IP、SSH 端口和系统账号登录",
        "actions": ["connectivity-check"],
    },
    {
        "id": "automation",
        "label": "Ansible 配置",
        "description": "对所有通过网络门禁的节点执行项目自定义 Ansible 剧本",
        "actions": ["ansible"],
    },
    {
        "id": "acceptance",
        "label": "节点验收",
        "description": "Ansible 配置完成，展示项目自定义检查结果，节点流程结束",
        "actions": ["project-checks"],
    },
]

STAGE_INDEX = {stage["id"]: index for index, stage in enumerate(PIPELINE_STAGES)}


def maas_cli(*args):
    return ["maas", MAAS_PROFILE, *args]


def machine_block_devices(system_id):
    if not system_id:
        return []
    try:
        data = json.loads(
            subprocess.run(
                maas_cli("block-devices", "read", system_id),
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
            ).stdout
            or "[]"
        )
    except Exception:
        return []
    return data if isinstance(data, list) else []


def deployable_block_devices(system_id):
    items = []
    for dev in machine_block_devices(system_id):
        dev_type = str(dev.get("type") or "").strip().lower()
        size = dev.get("size") or 0
        if dev_type in {"physical", "virtual"} and size:
            items.append(dev)
    return items


def node_status_key(node):
    return str(node.get("status") or "").strip().lower()


def action_policy(node):
    status = node_status_key(node)
    block_device_count = int(node.get("block_device_count") or 0)
    has_storage_inventory = block_device_count > 0
    storage_status = node.get("storage_status") or {}
    storage_ready = bool(storage_status.get("ok"))
    storage_failed = storage_status.get("ok") is False and bool(storage_status)
    has_maas_record = bool(str(node.get("system_id") or node.get("maas_system_id") or "").strip())
    verify_bmc_allowed = (
        not has_maas_record
        and bool(node.get("sn"))
        and bool(node.get("bmc_readback_ok"))
        and bool(node.get("bmc_user_readback_ok"))
        and bool(node.get("bmc_access_readback_ok"))
        and bool(node.get("bmc_network_readback_ok"))
    )
    delete_allowed = status not in {"bmc_configuring", "stage1_capturing", "commissioning", "deploying"}

    wipe_allowed = status in {"ready", "failed testing"}
    recommission_allowed = status in {"ready", "failed testing", "failed commissioning"}
    storage_allowed = status in {"ready", "failed deployment"} and has_storage_inventory
    deploy_allowed = status in {"ready", "failed deployment"} and has_storage_inventory and storage_ready

    return {
        "reboot": {"allowed": True, "reason": ""},
        "verify_bmc": {
            "allowed": verify_bmc_allowed,
            "reason": "" if verify_bmc_allowed else "BMC 本机 KCS 回读通过后才能执行远程 IPMI 重验",
        },
        "recommission": {
            "allowed": recommission_allowed,
            "reason": "" if recommission_allowed else "仅 Ready、Failed testing 或 Failed commissioning 节点可重新 Commissioning",
        },
        "wipe": {
            "allowed": wipe_allowed,
            "reason": (
                ""
                if wipe_allowed
                else "当前是 Failed commissioning：先修复 commissioning 依赖并重扫，节点 Ready 后才能清盘/清 RAID"
                if status == "failed commissioning"
                else "仅 Ready 或 Failed testing 节点可执行清盘/清 RAID"
            ),
        },
        "apply_storage": {
            "allowed": storage_allowed,
            "reason": (
                ""
                if storage_allowed
                else "MAAS 尚未发现可用块设备，请先重新 Commissioning 刷新硬件"
                if status in {"ready", "failed deployment"} and not has_storage_inventory
                else "仅 Ready 或 Failed deployment 节点可套存储策略"
            ),
        },
        "deploy": {
            "allowed": deploy_allowed,
            "reason": (
                ""
                if deploy_allowed
                else "套存储策略失败，请先调整策略后重新执行"
                if storage_failed
                else "请先成功执行套存储策略"
                if status in {"ready", "failed deployment"} and has_storage_inventory and not storage_ready
                else "MAAS 尚未发现可用块设备，请先重新 Commissioning 刷新硬件"
                if status in {"ready", "failed deployment"} and not has_storage_inventory
                else "仅 Ready 或 Failed deployment 节点可部署"
            ),
        },
        "delete": {
            "allowed": delete_allowed,
            "reason": "" if delete_allowed else "节点正在执行任务，完成或停止后才能删除",
        },
    }


def flow_nodes(nodes, statuses=None):
    wanted = {item.lower() for item in (statuses or [])}
    flow_tag = str(console_value("flow_tag", FLOW_TAG) or "").strip().lower()
    return [
        node for node in nodes
        if (not wanted or node_status_key(node) in wanted)
        and (not flow_tag or flow_tag in node_tags(node))
    ]


MODE_INFO = {
    "maintenance_locked": {
        "label": "维护锁定",
        "description": "MAAS 与无盘 DHCP/TFTP 均停止，不接受 PXE 节点。用于配置和维护。",
    },
    "diskless_stage1": {
        "label": "无盘抓配",
        "description": "Stage1 独占 DHCP/TFTP。只允许 BMC 配置、硬件采集和抓配导出。",
    },
    "maas_provision": {
        "label": "MAAS PXE",
        "description": "MAAS 独占 DHCP/TFTP。允许纳管、Commissioning、存储和系统部署。",
    },
    "conflict": {
        "label": "服务冲突",
        "description": "两套 PXE 控制面同时活动，已禁止所有推进操作，请立即切到维护锁定。",
    },
}


def active_service_mode(services=None):
    services = services or service_status()
    diskless = services.get("diskless-stage1-dnsmasq.service") == "active"
    maas = any(
        services.get(unit) == "active"
        for unit in ("maas-rackd.service", "maas-dhcpd.service", "maas-dhcpd6.service")
    )
    if diskless and maas:
        return "conflict"
    if diskless:
        return "diskless_stage1"
    if maas:
        return "maas_provision"
    return "maintenance_locked"


def gate(allowed, reason=""):
    return {"allowed": bool(allowed), "reason": "" if allowed else reason}


def control_status(nodes, services, sources):
    mode = active_service_mode(services)
    source_ok = required_sources_ok(sources, "core")
    stage1_source_ok = required_sources_ok(sources, "stage1")
    maas_source_ok = required_sources_ok(sources, "maas")
    config_errors = effective_lab_config().get("validation_errors") or []
    planned = [item for item in nodes if not clean_optional_text(item.get("system_id") or item.get("maas_system_id"))]
    stage1_ready = [item for item in planned if node_status_key(item) == "stage1_ready"]
    stage1_incomplete = [item for item in planned if node_status_key(item) != "stage1_ready"]
    has_plan = bool(planned)
    all_stage1_ready = has_plan and not stage1_incomplete
    maintenance = mode == "maintenance_locked"
    diskless = mode == "diskless_stage1"
    maas = mode == "maas_provision"
    healthy = mode != "conflict"
    sources_validated = workflow_check_ok("validate-sources")
    network_validated = workflow_check_ok("network-check")
    wipe_candidates = [
        item for item in flow_nodes(nodes, {"ready", "failed testing"})
        if not (item.get("storage_status") or {}).get("ok")
    ]
    storage_candidates = [
        item for item in flow_nodes(nodes, {"ready", "failed deployment"})
        if int(item.get("block_device_count") or 0) > 0
        and not (item.get("storage_status") or {}).get("ok")
    ]
    deploy_candidates = [
        item for item in flow_nodes(nodes, {"ready", "failed deployment"})
        if ((item.get("actions") or action_policy(item)).get("deploy") or {}).get("allowed")
    ]

    action_gates = {
        "validate-sources": gate(healthy, "PXE 服务冲突时只能先进入维护锁定"),
        "network-check": gate(
            healthy and sources_validated,
            "请先完成离线资源校验" if healthy else "PXE 服务冲突时只能先进入维护锁定",
        ),
        # Repair actions are recovery paths: they normalize services into
    # maintenance mode themselves, so they must remain available even
        # when both PXE control planes are accidentally active.
        "install-maas": gate(True),
        "install-diskless": gate(True),
        "sync-lab-stage1": gate(maintenance or diskless, "节点规划只能在维护锁定或无盘抓配模式同步"),
        "reset-stage1-state": gate(maintenance, "清空抓配状态必须进入维护锁定"),
        "export-stage1": gate(diskless and bool(stage1_ready), "仅无盘抓配模式且至少一个节点完成 BMC 回读和抓配后可导出"),
        "import-nodes": gate(
            maas and bool(stage1_ready),
            (
                "当前处于维护锁定：请先恢复/启用硬盘并切换到 MAAS PXE"
                if maintenance
                else "当前仍是无盘抓配模式：请先恢复/启用硬盘并切换到 MAAS PXE"
                if diskless
                else "当前服务模式异常：请先切换到维护锁定再处理"
            )
            if bool(stage1_ready) and not maas
            else "没有 Stage1 就绪节点可导入",
        ),
        "register-wipe-script": gate(maas, "注册清盘脚本必须在 MAAS PXE 模式"),
        "wipe-ready": gate(
            maas and bool(wipe_candidates),
            "没有等待清盘和创建 RAID 的 Ready 节点" if maas else "清盘/创建 RAID 必须在 MAAS PXE 模式",
        ),
        "apply-storage-ready": gate(
            maas and bool(storage_candidates),
            "没有已完成 RAID 且等待套存储策略的节点" if maas else "存储策略必须在 MAAS PXE 模式",
        ),
        "deploy-ready": gate(
            maas and sources_validated and network_validated and bool(deploy_candidates),
            "请先依次通过离线资源和网络校验"
            if maas and not (sources_validated and network_validated)
            else "没有已完成存储策略且可部署的节点"
            if maas
            else "系统部署必须在 MAAS PXE 模式",
        ),
        "reboot-nodes": gate(diskless or maas, "维护锁定模式不允许通过控制台重启交付节点"),
        "reverify-bmc-nodes": gate(
            maintenance or diskless or maas,
            "当前 PXE 服务状态异常，不能重新校验 BMC",
        ),
        "recommission-nodes": gate(maas, "Commissioning 必须在 MAAS PXE 模式"),
        "delete-nodes": gate(maintenance or diskless or maas, "当前 PXE 服务状态异常，不能删除节点"),
        "wipe-nodes": gate(maas, "清盘/清 RAID 必须在 MAAS PXE 模式"),
        "apply-storage-nodes": gate(maas, "存储策略必须在 MAAS PXE 模式"),
        "deploy-nodes": gate(
            maas and sources_validated and network_validated,
            "请先依次通过离线资源和网络校验"
            if maas
            else "系统部署必须在 MAAS PXE 模式",
        ),
    }
    checks_validated = sources_validated and network_validated
    diskless_allowed = stage1_source_ok and checks_validated and not config_errors and healthy
    maas_allowed = maas_source_ok and checks_validated and healthy
    if not healthy:
        diskless_reason = "PXE 服务状态异常，请先执行安装/修复无盘服务"
    elif not stage1_source_ok or not checks_validated:
        diskless_reason = "请先完成离线资源和网络校验"
    elif config_errors:
        diskless_reason = "；".join(str(item) for item in config_errors)
    else:
        diskless_reason = ""
    mode_gates = {
        "maintenance_locked": gate(True),
        "diskless_stage1": gate(diskless_allowed, diskless_reason),
        "maas_provision": gate(maas_allowed, "请先完成 MAAS 必需离线资源和网络校验"),
    }

    configured_clients = effective_lab_config().get("clients") or []
    if not sources_validated:
        next_action = {"action": "validate-sources", "label": "校验离线资源", "reason": ""}
    elif not network_validated:
        next_action = {"action": "network-check", "label": "检查网络联通", "reason": ""}
    elif not nodes and configured_clients:
        next_action = {"action": "sync-lab-stage1", "label": "同步节点规划", "reason": ""}
    elif stage1_incomplete:
        next_action = {
            "action": "pxe-diskless" if not diskless else "wait-stage1",
            "label": "切换到无盘抓配并重启节点" if not diskless else "重启节点并等待 Stage1 抓配",
            "reason": "",
        }
    elif stage1_ready and not maas:
        next_action = {
            "action": "export-stage1" if diskless else "pxe-maas",
            "label": "生成 MAAS 导入表" if diskless else "恢复硬盘并切换到 MAAS PXE",
            "reason": "",
        }
    elif action_gates["import-nodes"]["allowed"]:
        next_action = {"action": "import-nodes", "label": "导入 Stage1 就绪节点", "reason": ""}
    elif action_gates["wipe-ready"]["allowed"]:
        next_action = {"action": "wipe-ready", "label": "批量清盘并创建 RAID", "reason": ""}
    elif action_gates["apply-storage-ready"]["allowed"]:
        next_action = {"action": "apply-storage-ready", "label": "批量套存储策略", "reason": ""}
    elif action_gates["deploy-ready"]["allowed"]:
        next_action = {"action": "deploy-ready", "label": "批量部署 Ubuntu", "reason": ""}
    else:
        next_action = {}

    expected_units = {
        "maintenance_locked": {
            "maas-offline-http.service": False,
            "stage1-collector.service": False,
            "diskless-stage1-dnsmasq.service": False,
            "maas-regiond.service": False,
            "maas-rackd.service": False,
            "maas-dhcpd.service": False,
            "maas-dhcpd6.service": False,
        },
        "diskless_stage1": {
            "maas-offline-http.service": True,
            "stage1-collector.service": True,
            "diskless-stage1-dnsmasq.service": True,
            "maas-regiond.service": False,
            "maas-rackd.service": False,
            "maas-dhcpd.service": False,
            "maas-dhcpd6.service": False,
        },
        "maas_provision": {
            "maas-offline-http.service": True,
            "stage1-collector.service": False,
            "diskless-stage1-dnsmasq.service": False,
            "maas-regiond.service": True,
            "maas-rackd.service": True,
        },
    }
    checks = []
    for unit, expected_active in expected_units.get(mode, {}).items():
        actual_active = services.get(unit) == "active"
        checks.append({
            "label": f"{unit}: {'active' if actual_active else services.get(unit, 'unknown')} / 期望 {'active' if expected_active else 'inactive'}",
            "ok": actual_active == expected_active,
        })

    return {
        "mode": mode,
        **MODE_INFO.get(mode, MODE_INFO["conflict"]),
        "healthy": healthy and all(item["ok"] for item in checks),
        "checks": checks,
        "mode_gates": mode_gates,
        "action_gates": action_gates,
        "next_action": next_action,
        "stage1": {
            "planned": len(planned),
            "ready": len(stage1_ready),
            "incomplete": len(stage1_incomplete),
        },
        "workflow_checks": read_workflow_state().get("checks") or {},
    }


def require_service_mode(expected, action):
    actual = active_service_mode()
    if actual == expected:
        return None
    return {
        "ok": False,
        "error": f"{action} 被服务模式门禁阻止：要求 {MODE_INFO[expected]['label']}，当前为 {MODE_INFO.get(actual, MODE_INFO['conflict'])['label']}",
        "required_mode": expected,
        "actual_mode": actual,
    }


def require_service_modes(expected_modes, action):
    actual = active_service_mode()
    if actual in expected_modes:
        return None
    labels = " / ".join(MODE_INFO[item]["label"] for item in expected_modes)
    return {
        "ok": False,
        "error": f"{action} 被服务模式门禁阻止：要求 {labels}，当前为 {MODE_INFO.get(actual, MODE_INFO['conflict'])['label']}",
        "required_modes": list(expected_modes),
        "actual_mode": actual,
    }


def node_tags(node):
    raw = str(node.get("tag") or node.get("tags") or "").strip()
    return {item.strip().lower() for item in raw.replace(";", ",").split(",") if item.strip()}


def suggest_next_step(node):
    status = node_status_key(node)
    block_device_count = int(node.get("block_device_count") or 0)
    storage_status = node.get("storage_status") or {}
    if status == "bmc_plan_required":
        return "先补齐 SN、目标 BMC 地址/账号和目标业务网络"
    if status in {"inventory_pending", "pending", "planned", "new", "unknown"} and not node.get("pxe_mac"):
        return "切到无盘抓配并重启节点；Stage1 将配置 BMC、回读验证并采集硬件"
    if status in {"bmc_configuring", "bmc_configured", "stage1_capturing"}:
        return "等待 BMC 回读、远程认证和 PXE/硬件信息上报完成"
    if status in {"ready", "failed deployment"} and storage_status.get("ok") is False:
        return "套存储策略失败，先调整存储策略并重新执行，暂不能继续部署"
    if status in {"ready", "failed deployment"} and storage_status.get("ok") is True:
        return "存储策略已就绪，可以部署 Ubuntu"
    if status in {"ready", "failed deployment"} and block_device_count > 0 and not storage_status.get("ok"):
        return "先执行套存储策略，成功后再部署"
    if status == "ready" and block_device_count <= 0:
        return "MAAS 尚未发现 RAID 盘，先重新 Commissioning 刷新硬件，再套存储策略"
    if status == "ready":
        return "先清盘/清RAID，再套存储策略，最后部署"
    if status == "testing":
        return "等待清盘测试完成"
    if status == "failed testing":
        return "检查 Testing 日志，修复后可重新执行清盘"
    if status == "commissioning":
        return "等待 Commissioning 完成"
    if status == "failed commissioning":
        return "检查 Commissioning 日志并修复后重试"
    if status == "failed deployment":
        return "先重新套存储策略，再重新部署"
    if status == "deploying":
        return "等待部署完成"
    if status == "deployed":
        if node.get("connectivity_status") == "failed":
            return "修复业务网络或 SSH 登录后重新检测"
        if node.get("connectivity_status") != "passed":
            return "等待控制节点自动完成网络与 SSH 登录检测"
        if node.get("ansible_status") != "succeeded":
            return "网络与 SSH 已通过，执行项目 Ansible 剧本"
        return "节点验收 Ready，可查看项目自定义检查结果"
    if status == "stage1_ready":
        return "导出 maas.csv 并导入 MAAS"
    return "按状态继续处理"


def node_has_stage1_report(node):
    if node.get("last_report"):
        return True
    status = str(node.get("stage1_status") or node.get("status") or "").strip().lower()
    return status in {"stage1_ready", "failed"}


def node_blocker(node):
    message = clean_optional_text(node.get("message")) or clean_optional_text(node.get("status_message"))
    code = clean_optional_text(node.get("error_code"))
    errors = node.get("errors") or []
    if errors and not message:
        first = errors[0]
        if isinstance(first, (list, tuple)) and len(first) >= 2:
            code = code or clean_optional_text(first[0])
            message = clean_optional_text(first[1])
        else:
            message = clean_optional_text(first)
    if code or message:
        return " / ".join(part for part in (code, message) if part)
    status = node_status_key(node)
    storage_status = node.get("storage_status") or {}
    if status in {"ready", "failed deployment"} and storage_status.get("ok") is False:
        return clean_optional_text(storage_status.get("output")) or "存储策略执行失败"
    return ""


def node_state_key(node):
    return str(node.get("system_id") or node.get("maas_system_id") or node.get("node_key") or "").strip()


def connectivity_for_node(node):
    return ((read_json(CONNECTIVITY_STATE, {"nodes": {}}).get("nodes") or {}).get(node_state_key(node)) or {})


def latest_ansible_job_for_node(node):
    node_key = str(node.get("node_key") or "")
    if not node_key or not AUTOMATION_JOBS.exists():
        return {}
    latest = {}
    for path in AUTOMATION_JOBS.iterdir():
        if not path.is_dir():
            continue
        job = read_json(path / "job.json", {})
        if (
            job.get("kind") == "ansible"
            and job.get("status") == "succeeded"
            and not job.get("check_mode")
            and node_key in (job.get("node_keys") or [])
            and int(job.get("finished_at") or 0) >= int(latest.get("finished_at") or 0)
        ):
            latest = job
    return latest


def infer_node_phase(node):
    status = node_status_key(node)
    status_source = str(node.get("status_source") or "").strip().lower()
    has_maas_record = bool(clean_optional_text(node.get("system_id") or node.get("maas_system_id")))
    block_device_count = int(node.get("block_device_count") or 0)
    storage_status = node.get("storage_status") or {}
    blocker = node_blocker(node)

    if status == "bmc_plan_required":
        return "node_plan", "blocked", blocker or "目标 BMC 或业务网络规划不完整"
    if status in {"failed", "error", "abnormal", "broken"} and status_source != "maas":
        error_code = clean_optional_text(node.get("error_code")).upper()
        phase = "bmc_config" if error_code.startswith("BMC_") or error_code.startswith("IPMI_") else "stage1_capture"
        return phase, "blocked", blocker or "Stage1 抓配失败"
    if status in {"inventory_pending", "pending", "planned", "new", "unknown", ""} and not node_has_stage1_report(node):
        return "bmc_config", "active", "等待节点 PXE 进入 Stage1 并通过本机 IPMI/KCS 配置 BMC"
    if status in {"bmc_configuring", "bmc_configured"}:
        return "bmc_config", "active", blocker or "正在配置并回读 BMC"
    if status == "stage1_capturing":
        return "stage1_capture", "active", blocker or "正在采集 PXE MAC 和硬件信息"
    if status == "stage1_ready" and not has_maas_record:
        return "maas_import", "ready", "已完成抓配，等待导入 MAAS"
    if not has_maas_record:
        return "stage1_inventory", "active", blocker

    if status == "commissioning":
        return "commissioning", "active", "MAAS 正在盘点硬件"
    if status == "failed commissioning":
        return "commissioning", "blocked", blocker or "Commissioning 失败"
    if status == "testing":
        return "storage_prepare", "active", "清盘/清 RAID 脚本运行中"
    if status == "failed testing":
        return "storage_prepare", "blocked", blocker or "清盘/清 RAID 测试失败"
    if status in {"ready", "failed deployment"}:
        if storage_status.get("ok") is False:
            return "storage_prepare", "blocked", blocker or "存储策略失败"
        if storage_status.get("ok") is True:
            return "deploy", "ready", "存储策略已就绪，等待部署"
        if block_device_count <= 0:
            return "commissioning", "blocked", "MAAS 尚未发现可部署块设备"
        return "storage_prepare", "ready", "RAID 盘已发现，等待套存储策略"
    if status == "deploying":
        return "deploy", "active", "部署进行中"
    if status == "failed deployment":
        return "deploy", "blocked", blocker or "部署失败"
    if status == "deployed":
        connectivity = connectivity_for_node(node)
        if connectivity.get("status") == "failed":
            return "verify", "blocked", connectivity.get("error") or "网络或 SSH 登录检测失败"
        if connectivity.get("status") != "passed":
            return "verify", "active", "正在从控制节点自动检测业务网络和 SSH 登录"
        ansible_job = latest_ansible_job_for_node(node)
        if not ansible_job:
            return "automation", "ready", "网络与 SSH 已通过，等待执行项目 Ansible 剧本"
        return "acceptance", "ready", "Ansible 已完成，节点验收 Ready"

    return "stage1_capture", "active", blocker or "按节点状态继续处理"


def stage_timeline_for_node(node, phase_id, phase_state):
    current_index = STAGE_INDEX.get(phase_id, 0)
    timeline = []
    for index, stage in enumerate(PIPELINE_STAGES):
        if index < current_index:
            state = "done"
        elif index == current_index:
            state = "blocked" if phase_state == "blocked" else "current"
        else:
            state = "pending"
        timeline.append({
            "id": stage["id"],
            "label": stage["label"],
            "state": state,
        })
    return timeline


def enrich_node_flow(node):
    enriched = dict(node)
    phase_id, phase_state, blocker = infer_node_phase(enriched)
    phase_index = STAGE_INDEX.get(phase_id, 0)
    progress = int((phase_index / max(len(PIPELINE_STAGES) - 1, 1)) * 100)
    if phase_state == "ready":
        progress = min(progress + 5, 100)
    if phase_id == "acceptance" and phase_state == "ready":
        progress = 100
    enriched["phase"] = phase_id
    enriched["phase_label"] = PIPELINE_STAGES[phase_index]["label"]
    enriched["phase_state"] = phase_state
    enriched["progress"] = progress
    enriched["blocker"] = blocker
    enriched["stage_timeline"] = stage_timeline_for_node(enriched, phase_id, phase_state)
    system_id = str(enriched.get("system_id") or enriched.get("maas_system_id") or "").strip()
    compliance = ((read_json(COMPLIANCE_STATE, {"nodes": {}}).get("nodes") or {}).get(system_id) or {})
    enriched["compliance"] = compliance
    enriched["compliance_status"] = compliance.get("status") or "not_checked"
    connectivity = connectivity_for_node(enriched)
    enriched["connectivity"] = connectivity
    enriched["connectivity_status"] = connectivity.get("status") or "not_checked"
    ansible_job = latest_ansible_job_for_node(enriched)
    enriched["ansible_job"] = ansible_job
    enriched["ansible_status"] = "succeeded" if ansible_job else "not_run"
    enriched["next_step"] = suggest_next_step(enriched)
    return enriched


def read_maas_machines():
    try:
        result = subprocess.run(
            maas_cli("machines", "read"),
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError:
        return []
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def maas_machine_lookups():
    machines = read_maas_machines()
    by_hostname = {}
    by_mac = {}
    by_system_id = {}
    for machine in machines:
        hostname = str(machine.get("hostname") or "").strip().lower()
        if hostname:
            by_hostname[hostname] = machine
        system_id = str(machine.get("system_id") or "").strip()
        if system_id:
            by_system_id[system_id] = machine
        for iface in machine.get("interface_set") or []:
            for link in iface.get("links") or []:
                mac = normalize_mac(link.get("mac_address"))
                if mac:
                    by_mac[mac] = machine
            mac = normalize_mac(iface.get("mac_address"))
            if mac:
                by_mac[mac] = machine
    return {"machines": machines, "by_hostname": by_hostname, "by_mac": by_mac, "by_system_id": by_system_id}


def merge_maas_node(node, lookups):
    hostname = str(node.get("hostname") or "").strip().lower()
    pxe_mac = normalize_mac(node.get("pxe_mac"))
    system_id = str(node.get("system_id") or node.get("maas_system_id") or "").strip()
    machine = (
        (lookups["by_system_id"].get(system_id) if system_id else None)
        or (lookups["by_hostname"].get(hostname) if hostname else None)
        or (lookups["by_mac"].get(pxe_mac) if pxe_mac else None)
    )
    merged = dict(node)
    merged["stage1_status"] = node.get("stage1_status") or node.get("status") or ""
    if not machine:
        merged.setdefault("status_source", "stage1")
        merged["actions"] = action_policy(merged)
        return merged

    merged["status"] = maas_status_value(machine) or merged["stage1_status"]
    merged["status_source"] = "maas"
    merged["maas_status"] = maas_status_value(machine)
    merged["maas_system_id"] = machine.get("system_id", "")
    merged["system_id"] = machine.get("system_id", "")
    merged["power_state"] = machine.get("power_state", "")
    merged["status_message"] = machine.get("status_message", "")
    merged["maas_fqdn"] = machine.get("fqdn", "")
    planned_tags = node_tags(merged)
    maas_tags = {
        str(item.get("name") if isinstance(item, dict) else item).strip().lower()
        for item in (machine.get("tag_names") or [])
        if str(item.get("name") if isinstance(item, dict) else item).strip()
    }
    merged["tag"] = ",".join(sorted(planned_tags | maas_tags))
    merged["maas_tags"] = ",".join(sorted(maas_tags))
    merged["updated_at"] = machine.get("updated") or merged.get("updated_at", "")
    merged["block_device_count"] = len(deployable_block_devices(merged["system_id"]))
    merged["storage_status"] = storage_state_for_node(merged)
    merged["next_step"] = suggest_next_step(merged)
    merged["actions"] = action_policy(merged)
    return merged


def build_nodes():
    inventory_path = SOURCES / "stage1/inventory.csv"
    defaults_path = SOURCES / "stage1/defaults.yaml"
    state_path = SOURCES / "stage1/state.json"
    stage1_script = ROOT / "docs/scripts/stage1_collector.py"
    if inventory_path.exists() and defaults_path.exists() and state_path.exists() and stage1_script.exists():
        result = subprocess.run(
            [
                str(stage1_script),
                "inspect",
                "--inventory", str(inventory_path),
                "--defaults", str(defaults_path),
                "--state", str(state_path),
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            try:
                nodes = json.loads(result.stdout).get("nodes", [])
                for node in nodes:
                    node.setdefault("node_key", node_identity(node))
                lookups = maas_machine_lookups()
                merged = [merge_maas_node(node, lookups) for node in nodes]
                known_ids = {item.get("system_id") for item in merged if item.get("system_id")}
                known_hosts = {str(item.get("hostname") or "").strip().lower() for item in merged if item.get("hostname")}
                known_macs = {normalize_mac(item.get("pxe_mac")) for item in merged if item.get("pxe_mac")}
                for machine in lookups["machines"]:
                    system_id = str(machine.get("system_id") or "").strip()
                    hostname = str(machine.get("hostname") or "").strip()
                    hostname_key = hostname.lower()
                    iface_macs = {
                        normalize_mac(iface.get("mac_address"))
                        for iface in (machine.get("interface_set") or [])
                        if iface.get("mac_address")
                    }
                    if system_id in known_ids or hostname_key in known_hosts or (iface_macs & known_macs):
                        continue
                    merged.append({
                        "hostname": hostname,
                        "sn": "",
                        "status": maas_status_value(machine),
                        "stage1_status": "",
                        "maas_status": maas_status_value(machine),
                        "status_source": "maas",
                        "pxe_mac": next(iter(iface_macs), ""),
                        "bmc_ip": "",
                        "node_id": "",
                        "tag": ",".join(item.get("name", "") for item in (machine.get("tag_names") or []) if item),
                        "status_message": machine.get("status_message", ""),
                        "updated_at": machine.get("updated", ""),
                        "system_id": system_id,
                        "maas_system_id": system_id,
                        "power_state": machine.get("power_state", ""),
                        "block_device_count": len(deployable_block_devices(system_id)),
                        "storage_status": storage_state_for_node({"system_id": system_id}),
                        "next_step": suggest_next_step({
                            "status": maas_status_value(machine),
                            "block_device_count": len(deployable_block_devices(system_id)),
                            "storage_status": storage_state_for_node({"system_id": system_id}),
                        }),
                        "actions": action_policy({
                            "status": maas_status_value(machine),
                            "block_device_count": len(deployable_block_devices(system_id)),
                            "storage_status": storage_state_for_node({"system_id": system_id}),
                        }),
                        "node_key": system_id or hostname or node_identity(machine),
                    })
                return merged
            except Exception:
                pass

    inventory = read_inventory()
    reports = read_stage1_state().get("reports", {})
    nodes = []
    for row in inventory:
        key = row.get("sn") or row.get("serial") or row.get("hostname") or row.get("mac") or ""
        report = reports.get(key, {}) if isinstance(reports, dict) else {}
        status = report.get("status") or row.get("status") or "pending"
        node = {**row, "status": status, "stage1_status": status, "status_source": "stage1", "last_report": report}
        node["block_device_count"] = 0
        node["storage_status"] = {}
        node["next_step"] = suggest_next_step(node)
        node["actions"] = action_policy(node)
        node["node_key"] = node_identity(node)
        nodes.append(node)
    lookups = maas_machine_lookups()
    return [merge_maas_node(node, lookups) for node in nodes]


def node_identity(node):
    for key in ("node_key", "sn", "hostname", "bmc_ip", "pxe_mac"):
        value = str(node.get(key) or "").strip()
        if value:
            return value
    return f"node-{abs(hash(json.dumps(node, sort_keys=True, ensure_ascii=False)))}"


def pipeline_summary(nodes, services, sources):
    total = len(nodes)
    by_phase = {stage["id"]: {"total": 0, "blocked": 0, "active": 0, "ready": 0} for stage in PIPELINE_STAGES}
    for node in nodes:
        phase = node.get("phase") or "node_plan"
        state = node.get("phase_state") or "active"
        bucket = by_phase.setdefault(phase, {"total": 0, "blocked": 0, "active": 0, "ready": 0})
        bucket["total"] += 1
        if state in bucket:
            bucket[state] += 1

    source_ok = required_sources_ok(sources, "core")
    stage1_services_ok = all(
        services.get(unit) == "active"
        for unit in ("maas-offline-http.service", "stage1-collector.service", "diskless-stage1-dnsmasq.service")
        if unit in services
    )
    maas_services_ok = any(
        services.get(unit) == "active"
        for unit in ("maas-regiond.service", "maas-rackd.service", "maas-http.service")
    )

    items = []
    for stage in PIPELINE_STAGES:
        stage_id = stage["id"]
        counts = by_phase.get(stage_id, {})
        if stage_id == "sources":
            state = "done" if source_ok and workflow_check_ok("validate-sources") else "blocked"
            reason = "" if state == "done" else "离线资源不完整或尚未完成实际校验"
        elif stage_id == "network":
            state = "done" if workflow_check_ok("network-check") else "ready"
            reason = "" if state == "done" else "执行网络联通检查确认管理网和装机网"
        elif stage_id == "control_plane":
            state = "done" if stage1_services_ok or maas_services_ok else "blocked"
            reason = "" if state == "done" else "控制面服务未全部就绪"
        elif total == 0:
            state = "pending"
            reason = "尚未生成节点清单"
        elif counts.get("blocked"):
            state = "blocked"
            reason = f"{counts.get('blocked')} 个节点阻塞在此阶段"
        elif counts.get("active"):
            state = "active"
            reason = f"{counts.get('active')} 个节点正在此阶段"
        elif stage_id == "acceptance" and counts.get("ready"):
            state = "ready"
            reason = f"{counts.get('ready')} 个节点流程已完成"
        elif counts.get("ready"):
            state = "ready"
            reason = f"{counts.get('ready')} 个节点等待下一步"
        elif sum((by_phase.get(item["id"], {}).get("total", 0) for item in PIPELINE_STAGES[STAGE_INDEX[stage_id] + 1:]), 0):
            state = "done"
            reason = ""
        else:
            state = "pending"
            reason = ""
        items.append({
            **stage,
            "state": state,
            "reason": reason,
            "counts": counts,
        })
    return items


def summary():
    raw_nodes = build_nodes()
    ensure_connectivity_checks(raw_nodes)
    nodes = [enrich_node_flow(item) for item in raw_nodes]
    total = len(nodes)
    ready_states = {"ready", "ok", "validated", "deployed", "stage1_ready"}
    failed_states = {"failed", "error", "abnormal", "failed testing", "failed commissioning", "failed deployment", "broken"}
    ready = sum(1 for item in nodes if str(item.get("status") or "").strip().lower() in ready_states)
    failed = sum(1 for item in nodes if str(item.get("status") or "").strip().lower() in failed_states)
    pending = max(total - ready - failed, 0)
    blocked = sum(1 for item in nodes if item.get("phase_state") == "blocked")
    deployed = sum(1 for item in nodes if node_status_key(item) == "deployed")
    stage_ready = sum(1 for item in nodes if node_status_key(item) == "stage1_ready")
    services = service_status()
    sources = source_status()
    return {
        "sources_root": str(SOURCES),
        "lab_config_path": str(LAB_CONFIG),
        "allow_mutation": ALLOW_MUTATION,
        "stats": {
            "total": total,
            "ready": ready,
            "failed": failed,
            "pending": pending,
            "blocked": blocked,
            "deployed": deployed,
            "stage1_ready": stage_ready,
        },
        "pipeline": pipeline_summary(nodes, services, sources),
        "control": control_status(nodes, services, sources),
        "services": services,
        "sources": sources,
        "lab": redact_config(effective_lab_config()),
        "nodes": redact_config(nodes),
    }


def shell_quote(value):
    text = str(value)
    if not text:
        return "''"
    safe = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_+-=.,/:@%"
    if all(ch in safe for ch in text):
        return text
    return "'" + text.replace("'", "'\"'\"'") + "'"


def lab_client_lookup():
    lookup = {}
    for client in lab_clients():
        for key in ("sn", "hostname", "bmc_ip", "pxe_mac"):
            value = str(client.get(key) or "").strip()
            if value:
                lookup[value] = client
    return lookup


def merge_node_secrets(node):
    merged = dict(node)
    client = lab_client_lookup().get(node_identity(node), {})
    for field in ("bmc_ip", "bmc_user", "bmc_pass", "hostname", "sn", "pxe_mac"):
        if not merged.get(field) and client.get(field):
            merged[field] = client.get(field)
    merged["actions"] = action_policy(merged)
    merged["node_key"] = node_identity(merged)
    return merged


def reboot_nodes(node_keys, dry=False):
    if not node_keys:
        return {"ok": False, "error": "no nodes selected"}
    all_nodes = [merge_node_secrets(node) for node in build_nodes()]
    selected = [node for node in all_nodes if node.get("node_key") in set(node_keys)]
    if not selected:
        return {"ok": False, "error": "selected nodes not found"}
    if not dry and not ALLOW_MUTATION:
        return {"ok": False, "error": "mutation disabled; set MAAS_CONSOLE_ALLOW_MUTATION=1"}

    results = []
    overall_ok = True
    for node in selected:
        hostname = node.get("hostname") or node.get("sn") or node.get("node_key")
        system_id = str(node.get("system_id") or node.get("maas_system_id") or "").strip()
        bmc_ip = node.get("bmc_ip") or ""
        bmc_user = node.get("bmc_user") or ""
        bmc_pass = node.get("bmc_pass") or ""
        command = [
            "sshpass", "-p", bmc_pass,
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=8",
            f"{bmc_user}@{bmc_ip}",
            "racadm serveraction powercycle",
        ]
        if not (bmc_ip and bmc_user and bmc_pass):
            overall_ok = False
            results.append({
                "node_key": node.get("node_key"),
                "hostname": hostname,
                "ok": False,
                "error": "missing bmc credentials",
            })
            continue
        if dry:
            results.append({
                "node_key": node.get("node_key"),
                "hostname": hostname,
                "ok": True,
                "dry_run": True,
                "command": sanitize_text(" ".join(shell_quote(part) for part in command)),
            })
            continue
        result = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        ok = result.returncode == 0
        summarized_output = sanitize_text(result.stdout[-4000:])
        if ok:
            try:
                payload = json.loads(result.stdout or "{}")
                summarized_output = sanitize_text(
                    f"started recommission system_id={system_id} "
                    f"status={payload.get('status_name') or payload.get('status') or 'unknown'}"
                )
            except Exception:
                pass
        if not ok:
            overall_ok = False
        results.append({
            "node_key": node.get("node_key"),
            "hostname": hostname,
            "ok": ok,
            "code": result.returncode,
            "output": summarized_output,
        })

    output_lines = []
    for item in results:
        prefix = "[ok]" if item.get("ok") else "[failed]"
        output_lines.append(f"{prefix} {item.get('hostname')} ({item.get('node_key')})")
        if item.get("command"):
            output_lines.append(f"  {item['command']}")
        if item.get("error"):
            output_lines.append(f"  error: {item['error']}")
        if item.get("output"):
            output_lines.append(item["output"].rstrip())
    return {
        "ok": overall_ok,
        "code": 0 if overall_ok else 1,
        "action": "reboot-nodes",
        "results": results,
        "output": "\n".join(output_lines).strip(),
    }


def reverify_bmc_nodes(node_keys, dry=False):
    if not node_keys:
        return {"ok": False, "error": "no nodes selected"}
    if not dry and not ALLOW_MUTATION:
        return {"ok": False, "error": "mutation disabled; set MAAS_CONSOLE_ALLOW_MUTATION=1"}
    selected_keys = set(node_keys)
    selected = [node for node in build_nodes() if node.get("node_key") in selected_keys]
    if not selected:
        return {"ok": False, "error": "selected nodes not found"}

    script = ROOT / "docs/scripts/stage1_collector.py"
    common = [
        "--inventory", str(SOURCES / "stage1/inventory.csv"),
        "--defaults", str(SOURCES / "stage1/defaults.yaml"),
        "--state", str(SOURCES / "stage1/state.json"),
    ]
    results = []
    overall_ok = True
    for node in selected:
        sn = str(node.get("sn") or "").strip()
        policy = (node.get("actions") or action_policy(node)).get("verify_bmc") or {}
        if not sn or not policy.get("allowed"):
            overall_ok = False
            results.append({
                "node_key": node.get("node_key"),
                "hostname": node.get("hostname") or sn,
                "ok": False,
                "error": policy.get("reason") or "missing SN",
            })
            continue
        command = [str(script), "reverify-bmc", *common, "--sn", sn]
        if dry:
            results.append({
                "node_key": node.get("node_key"),
                "hostname": node.get("hostname") or sn,
                "ok": True,
                "dry_run": True,
                "command": sanitize_text(" ".join(shell_quote(part) for part in command)),
            })
            continue
        result = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        ok = result.returncode == 0
        overall_ok = overall_ok and ok
        results.append({
            "node_key": node.get("node_key"),
            "hostname": node.get("hostname") or sn,
            "ok": ok,
            "code": result.returncode,
            "output": sanitize_text(result.stdout[-4000:]),
        })
    return {
        "ok": overall_ok,
        "code": 0 if overall_ok else 1,
        "action": "reverify-bmc-nodes",
        "results": results,
        "output": "\n".join(
            f"[{'ok' if item.get('ok') else 'failed'}] {item.get('hostname')}: "
            f"{item.get('output') or item.get('error') or item.get('command') or ''}"
            for item in results
        ).strip(),
    }


def recommission_nodes(node_keys, dry=False):
    if not node_keys:
        return {"ok": False, "error": "no nodes selected"}
    if not dry and not ALLOW_MUTATION:
        return {"ok": False, "error": "mutation disabled; set MAAS_CONSOLE_ALLOW_MUTATION=1"}

    selected_keys = set(node_keys)
    all_nodes = [merge_node_secrets(node) for node in build_nodes()]
    selected = [node for node in all_nodes if node.get("node_key") in selected_keys]
    if not selected:
        return {"ok": False, "error": "selected nodes not found"}

    results = []
    overall_ok = True
    for node in selected:
        hostname = node.get("hostname") or node.get("sn") or node.get("node_key")
        system_id = str(node.get("system_id") or node.get("maas_system_id") or "").strip()
        if not system_id:
            overall_ok = False
            results.append({"node_key": node.get("node_key"), "hostname": hostname, "ok": False, "error": "missing system_id"})
            continue

        recommission_policy = (node.get("actions") or {}).get("recommission") or {}
        if not recommission_policy.get("allowed"):
            overall_ok = False
            results.append({
                "node_key": node.get("node_key"),
                "hostname": hostname,
                "ok": False,
                "error": recommission_policy.get("reason") or f"status {node.get('status')} cannot recommission",
            })
            continue

        command = maas_cli(
            "machine",
            "commission",
            system_id,
            "enable_ssh=1",
            "skip_bmc_config=1",
            "skip_networking=1",
            "testing_scripts=none",
        )
        if dry:
            results.append({
                "node_key": node.get("node_key"),
                "hostname": hostname,
                "ok": True,
                "dry_run": True,
                "command": sanitize_text(" ".join(shell_quote(part) for part in command)),
            })
            continue

        result = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        ok = result.returncode == 0
        if ok:
            clear_storage_state(node)
        if not ok:
            overall_ok = False
        results.append({
            "node_key": node.get("node_key"),
            "hostname": hostname,
            "ok": ok,
            "code": result.returncode,
            "output": sanitize_text(result.stdout[-4000:]),
        })

    output_lines = []
    for item in results:
        prefix = "[ok]" if item.get("ok") else "[failed]"
        output_lines.append(f"{prefix} {item.get('hostname')} ({item.get('node_key')})")
        if item.get("command"):
            output_lines.append(f"  {item['command']}")
        if item.get("error"):
            output_lines.append(f"  error: {item['error']}")
        if item.get("output"):
            output_lines.append(item["output"].rstrip())
    return {
        "ok": overall_ok,
        "code": 0 if overall_ok else 1,
        "action": "recommission-nodes",
        "results": results,
        "output": "\n".join(output_lines).strip(),
    }


def deploy_nodes(node_keys, dry=False):
    if not node_keys:
        return {"ok": False, "error": "no nodes selected"}
    if not dry and not ALLOW_MUTATION:
        return {"ok": False, "error": "mutation disabled; set MAAS_CONSOLE_ALLOW_MUTATION=1"}
    if not (workflow_check_ok("validate-sources") and workflow_check_ok("network-check")):
        return {"ok": False, "error": "部署门禁未通过：请先依次完成离线资源校验和网络联通检查"}

    selected_keys = set(node_keys)
    all_nodes = build_nodes()
    selected = [node for node in all_nodes if node.get("node_key") in selected_keys]
    if not selected:
        return {"ok": False, "error": "selected nodes not found"}

    deploy_csv = str(SOURCES / "stage1/export/maas.csv")
    deploy_policy = str(console_value("deploy_policy", DEPLOY_POLICY) or "").strip()
    deploy_osystem = str(console_value("deploy_osystem", DEPLOY_OSYSTEM) or DEPLOY_OSYSTEM).strip()
    deploy_series = str(console_value("deploy_series", DEPLOY_SERIES) or DEPLOY_SERIES).strip()
    template_output = ""
    if not dry:
        template_command = [
            "sudo",
            "-n",
            str(ROOT / "docs/scripts/maas_install_curtin_login_template.py"),
            "--config",
            str(POLICY_FILES["deploy_policy"]),
            "--user-data",
            str(POLICY_FILES["default_user_data"]),
            "--csv",
            deploy_csv,
            "--output",
            "/etc/maas/preseeds",
            "--osystem",
            deploy_osystem,
            "--series",
            deploy_series,
        ]
        template_result = run_action("sync-curtin-login-template", template_command, mutate=True)
        if not template_result.get("ok"):
            return {
                "ok": False,
                "error": "部署前同步默认账户的 curtin 模板失败",
                "output": template_result.get("output") or template_result.get("error") or "",
            }
        template_output = template_result.get("output") or ""
    results = []
    overall_ok = True

    for node in selected:
        hostname = node.get("hostname") or node.get("sn") or node.get("node_key")
        system_id = str(node.get("system_id") or node.get("maas_system_id") or "").strip()
        if not system_id:
            overall_ok = False
            results.append({
                "node_key": node.get("node_key"),
                "hostname": hostname,
                "ok": False,
                "error": "missing system_id",
            })
            continue
        deploy_policy_info = (node.get("actions") or {}).get("deploy") or {}
        if not deploy_policy_info.get("allowed"):
            overall_ok = False
            results.append({
                "node_key": node.get("node_key"),
                "hostname": hostname,
                "ok": False,
                "error": deploy_policy_info.get("reason") or f"status {node.get('status')} is not deployable",
            })
            continue

        command = [str(ROOT / "docs/scripts/maas_deploy_one.sh"), system_id]
        env = os.environ.copy()
        env["PROFILE"] = MAAS_PROFILE
        env["OSYSTEM"] = deploy_osystem
        env["SERIES"] = deploy_series
        env["DEPLOY_CSV"] = deploy_csv
        if deploy_policy:
            env["DEPLOY_POLICY"] = deploy_policy

        if dry:
            env_prefix = [f"PROFILE={MAAS_PROFILE}", f"OSYSTEM={deploy_osystem}", f"SERIES={deploy_series}", f"DEPLOY_CSV={deploy_csv}"]
            if deploy_policy:
                env_prefix.append(f"DEPLOY_POLICY={deploy_policy}")
            results.append({
                "node_key": node.get("node_key"),
                "hostname": hostname,
                "ok": True,
                "dry_run": True,
                "command": sanitize_text(" ".join(env_prefix + [shell_quote(part) for part in command])),
            })
            continue

        result = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        ok = result.returncode == 0
        if not ok:
            overall_ok = False
        results.append({
            "node_key": node.get("node_key"),
            "hostname": hostname,
            "ok": ok,
            "code": result.returncode,
            "output": sanitize_text(result.stdout[-4000:]),
        })

    output_lines = []
    for item in results:
        prefix = "[ok]" if item.get("ok") else "[failed]"
        output_lines.append(f"{prefix} {item.get('hostname')} ({item.get('node_key')})")
        if item.get("command"):
            output_lines.append(f"  {item['command']}")
        if item.get("error"):
            output_lines.append(f"  error: {item['error']}")
        if item.get("output"):
            output_lines.append(item["output"].rstrip())
    return {
        "ok": overall_ok,
        "code": 0 if overall_ok else 1,
        "action": "deploy-nodes",
        "results": results,
        "output": "\n".join(
            [
                "[dry-run] 实际部署前将按 default-user-data.yaml 同步 curtin 登录模板"
                if dry
                else "[ok] 已按 default-user-data.yaml 同步 curtin 登录模板",
                template_output,
                *output_lines,
            ]
        ).strip(),
    }


def delete_nodes(node_keys, dry=False):
    if not node_keys:
        return {"ok": False, "error": "no nodes selected"}
    if not dry and not ALLOW_MUTATION:
        return {"ok": False, "error": "mutation disabled; set MAAS_CONSOLE_ALLOW_MUTATION=1"}

    selected_keys = set(node_keys)
    all_nodes = build_nodes()
    selected = [node for node in all_nodes if node.get("node_key") in selected_keys]
    if not selected:
        return {"ok": False, "error": "selected nodes not found"}

    results = []
    overall_ok = True
    config_removals = {
        node.get("node_key"): remove_nodes_from_lab_config([node], dry=True).get(node.get("node_key"), {})
        for node in selected
    }
    runtime_removals = {
        node.get("node_key"): remove_nodes_from_stage1_runtime([node], dry=True)
        for node in selected
    }
    removable_node_keys = set()

    for node in selected:
        hostname = node.get("hostname") or node.get("sn") or node.get("node_key")
        system_id = str(node.get("system_id") or node.get("maas_system_id") or "").strip()
        delete_policy = (node.get("actions") or {}).get("delete") or {}
        config_removed_count = sum((config_removals.get(node.get("node_key")) or {}).values())
        runtime_removed_count = sum((runtime_removals.get(node.get("node_key")) or {}).values())
        if system_id and not delete_policy.get("allowed"):
            overall_ok = False
            results.append({
                "node_key": node.get("node_key"),
                "hostname": hostname,
                "ok": False,
                "error": delete_policy.get("reason") or "node is not deletable",
            })
            continue
        if not system_id:
            removed_count = config_removed_count + runtime_removed_count
            if not removed_count:
                overall_ok = False
            results.append({
                "node_key": node.get("node_key"),
                "hostname": hostname,
                "ok": bool(removed_count),
                "config_only": bool(removed_count),
                "config_removed": config_removed_count,
                "runtime_removed": runtime_removed_count,
                "error": "" if removed_count else "missing system_id and no matching Stage1 or lab entry",
            })
            if removed_count:
                removable_node_keys.add(node.get("node_key"))
            continue

        command = maas_cli("machine", "delete", system_id)
        if dry:
            results.append({
                "node_key": node.get("node_key"),
                "hostname": hostname,
                "ok": True,
                "dry_run": True,
                "config_removed": config_removed_count,
                "command": sanitize_text(" ".join(shell_quote(part) for part in command)),
            })
            continue

        result = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        ok = result.returncode == 0
        if ok:
            clear_storage_state_by_system_id(system_id)
            removable_node_keys.add(node.get("node_key"))
        else:
            overall_ok = False
        results.append({
            "node_key": node.get("node_key"),
            "hostname": hostname,
            "ok": ok,
            "code": result.returncode,
            "config_removed": config_removed_count if ok else 0,
            "runtime_removed": runtime_removed_count if ok else 0,
            "output": sanitize_text(result.stdout[-4000:]),
        })

    if not dry:
        removable_nodes = [node for node in selected if node.get("node_key") in removable_node_keys]
        remove_nodes_from_lab_config(removable_nodes, dry=False)
        remove_nodes_from_stage1_runtime(removable_nodes, dry=False)
        for node in removable_nodes:
            clear_node_runtime_cache(node)

    output_lines = []
    for item in results:
        prefix = "[ok]" if item.get("ok") else "[failed]"
        output_lines.append(f"{prefix} {item.get('hostname')} ({item.get('node_key')})")
        if item.get("config_only"):
            output_lines.append("  MAAS 中没有 system_id，已按配置残留清理。")
        if item.get("config_removed"):
            output_lines.append(f"  removed config entries: {item['config_removed']}")
        if item.get("runtime_removed"):
            output_lines.append(f"  removed Stage1 entries: {item['runtime_removed']}")
        if item.get("command"):
            output_lines.append(f"  {item['command']}")
        if item.get("error"):
            output_lines.append(f"  error: {item['error']}")
        if item.get("output"):
            output_lines.append(item["output"].rstrip())
    return {
        "ok": overall_ok,
        "code": 0 if overall_ok else 1,
        "action": "delete-nodes",
        "results": results,
        "runtime_removed": runtime_removals,
        "output": "\n".join(output_lines).strip(),
    }


def wipe_nodes(node_keys, dry=False):
    if not node_keys:
        return {"ok": False, "error": "no nodes selected"}
    if not dry and not ALLOW_MUTATION:
        return {"ok": False, "error": "mutation disabled; set MAAS_CONSOLE_ALLOW_MUTATION=1"}

    selected_keys = set(node_keys)
    all_nodes = build_nodes()
    selected = [node for node in all_nodes if node.get("node_key") in selected_keys]
    if not selected:
        return {"ok": False, "error": "selected nodes not found"}

    register_result = ensure_wipe_script(dry=dry)
    if not register_result.get("ok"):
        return register_result

    wipe_script_name = str(console_value("wipe_script_name", WIPE_SCRIPT_NAME) or WIPE_SCRIPT_NAME)
    results = []
    overall_ok = True

    for node in selected:
        hostname = node.get("hostname") or node.get("sn") or node.get("node_key")
        system_id = str(node.get("system_id") or node.get("maas_system_id") or "").strip()
        if not system_id:
            overall_ok = False
            results.append({"node_key": node.get("node_key"), "hostname": hostname, "ok": False, "error": "missing system_id"})
            continue
        wipe_policy = (node.get("actions") or {}).get("wipe") or {}
        if not wipe_policy.get("allowed"):
            overall_ok = False
            results.append({
                "node_key": node.get("node_key"),
                "hostname": hostname,
                "ok": False,
                "error": wipe_policy.get("reason") or f"status {node.get('status')} is not wipeable",
            })
            continue

        command = [
            *maas_cli(
            "machine",
            "test",
            system_id,
            f"testing_scripts={wipe_script_name}",
        )]
        if dry:
            results.append({
                "node_key": node.get("node_key"),
                "hostname": hostname,
                "ok": True,
                "dry_run": True,
                "command": sanitize_text(" ".join(shell_quote(part) for part in command)),
            })
            continue

        result = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        ok = result.returncode == 0
        if ok:
            clear_storage_state(node)
        if not ok:
            overall_ok = False
        results.append({
            "node_key": node.get("node_key"),
            "hostname": hostname,
            "ok": ok,
            "code": result.returncode,
            "output": sanitize_text(result.stdout[-4000:]),
        })

    output_lines = []
    for item in results:
        prefix = "[ok]" if item.get("ok") else "[failed]"
        output_lines.append(f"{prefix} {item.get('hostname')} ({item.get('node_key')})")
        if item.get("command"):
            output_lines.append(f"  {item['command']}")
        if item.get("error"):
            output_lines.append(f"  error: {item['error']}")
        if item.get("output"):
            output_lines.append(item["output"].rstrip())
    return {
        "ok": overall_ok,
        "code": 0 if overall_ok else 1,
        "action": "wipe-nodes",
        "results": results,
        "output": "\n".join(
            ["[ok] 已同步 MAAS 清盘/RAID 脚本", register_result.get("output", ""), *output_lines]
        ).strip(),
    }


def apply_storage_nodes(node_keys, dry=False):
    if not node_keys:
        return {"ok": False, "error": "no nodes selected"}
    if not dry and not ALLOW_MUTATION:
        return {"ok": False, "error": "mutation disabled; set MAAS_CONSOLE_ALLOW_MUTATION=1"}

    selected_keys = set(node_keys)
    all_nodes = build_nodes()
    selected = [node for node in all_nodes if node.get("node_key") in selected_keys]
    if not selected:
        return {"ok": False, "error": "selected nodes not found"}

    storage_csv = str(SOURCES / "stage1/export/maas.csv")
    deploy_policy = str(console_value("deploy_policy", DEPLOY_POLICY) or "").strip()
    results = []
    overall_ok = True

    for node in selected:
        hostname = node.get("hostname") or node.get("sn") or node.get("node_key")
        status = str(node.get("status") or "").strip().lower()
        system_id = str(node.get("system_id") or node.get("maas_system_id") or "").strip()
        if not system_id:
            overall_ok = False
            results.append({"node_key": node.get("node_key"), "hostname": hostname, "ok": False, "error": "missing system_id"})
            continue
        storage_policy = (node.get("actions") or {}).get("apply_storage") or {}
        if not storage_policy.get("allowed"):
            overall_ok = False
            results.append({
                "node_key": node.get("node_key"),
                "hostname": hostname,
                "ok": False,
                "error": storage_policy.get("reason") or f"status {node.get('status')} cannot apply storage",
            })
            continue

        command = [
            "python3",
            str(ROOT / "docs/scripts/maas_apply_storage_policy.py"),
            "--csv",
            storage_csv,
            system_id,
        ]
        if status == "failed deployment":
            command.insert(-1, "--include-failed-deployment")
        if deploy_policy:
            command[2:2] = ["--policy", deploy_policy]
        env = os.environ.copy()
        env["PROFILE"] = MAAS_PROFILE

        if dry:
            env_prefix = [f"PROFILE={MAAS_PROFILE}"]
            results.append({
                "node_key": node.get("node_key"),
                "hostname": hostname,
                "ok": True,
                "dry_run": True,
                "command": sanitize_text(" ".join(env_prefix + [shell_quote(part) for part in command])),
            })
            continue

        result = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        ok = result.returncode == 0
        update_storage_state(node, ok=ok, output=result.stdout or "", policy=deploy_policy or "default")
        if not ok:
            overall_ok = False
        results.append({
            "node_key": node.get("node_key"),
            "hostname": hostname,
            "ok": ok,
            "code": result.returncode,
            "output": sanitize_text(result.stdout[-4000:]),
        })

    output_lines = []
    for item in results:
        prefix = "[ok]" if item.get("ok") else "[failed]"
        output_lines.append(f"{prefix} {item.get('hostname')} ({item.get('node_key')})")
        if item.get("command"):
            output_lines.append(f"  {item['command']}")
        if item.get("error"):
            output_lines.append(f"  error: {item['error']}")
        if item.get("output"):
            output_lines.append(item["output"].rstrip())
    return {
        "ok": overall_ok,
        "code": 0 if overall_ok else 1,
        "action": "apply-storage-nodes",
        "results": results,
        "output": "\n".join(output_lines).strip(),
    }


def run_action(name, args, mutate=False, dry_run=False):
    if mutate and not ALLOW_MUTATION:
        return {"ok": False, "error": "mutation disabled; set MAAS_CONSOLE_ALLOW_MUTATION=1"}
    if dry_run:
        return {"ok": True, "code": 0, "action": name, "output": sanitize_text(" ".join(shell_quote(arg) for arg in args))}
    result = subprocess.run(args, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    return {"ok": result.returncode == 0, "code": result.returncode, "action": name, "output": sanitize_text(result.stdout[-12000:])}


def maas_cli_action(name, args, dry_run=False):
    if dry_run:
        return {"ok": True, "code": 0, "action": name, "output": sanitize_text(" ".join(shell_quote(arg) for arg in args))}
    if not ALLOW_MUTATION:
        return {"ok": False, "error": "mutation disabled; set MAAS_CONSOLE_ALLOW_MUTATION=1"}
    result = subprocess.run(args, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    return {"ok": result.returncode == 0, "code": result.returncode, "action": name, "output": sanitize_text(result.stdout[-12000:])}


def flow_target_keys(include_statuses):
    keys = []
    wanted = {item.lower() for item in include_statuses}
    flow_tag = str(console_value("flow_tag", FLOW_TAG) or "").strip().lower()
    for node in build_nodes():
        status = str(node.get("status") or "").strip().lower()
        tags = node_tags(node)
        if status in wanted and (not flow_tag or flow_tag in tags):
            keys.append(node.get("node_key"))
    return keys


def rendered_wipe_script_text():
    script_path = ROOT / "scripts" / "wipe-raid-and-disks-test.sh"
    if not script_path.exists():
        raise FileNotFoundError(str(script_path))
    rendered = script_path.read_text(encoding="utf-8")
    replacements = {
        "__OFFLINE_TOOL_BASE_URL__": str(raid_value("tools_base_url", derive_tools_base_url(read_lab_config())) or ""),
        "__STAGE1_CONFIG_URL__": (
            f"http://{stage1_server_ip()}:{os.environ.get('MAAS_CONSOLE_PORT', '8088')}/api/storage-config"
        ),
        "__BOOT_VD_NAME__": str(raid_value("boot_vd_name", "ssd01") or "ssd01"),
        "__SINGLE_DISK_RAID_LEVEL__": str(raid_value("single_disk_raid_level", "r0") or "r0"),
        "__MULTI_DISK_RAID_LEVEL__": str(raid_value("multi_disk_raid_level", "r1") or "r1"),
        "__BOOT_DISK_COUNT__": str(raid_value("boot_disk_count", 2) or 2),
        "__DATA_DISK_RAID_LAYOUT__": json.dumps(raid_value("data_disk_raid_layout", []) or [], ensure_ascii=False),
    }
    for source, target in replacements.items():
        rendered = rendered.replace(source, target)
    return rendered


def ensure_wipe_script(dry=False):
    script_path = ROOT / "scripts" / "wipe-raid-and-disks-test.sh"
    if not script_path.exists():
        return {"ok": False, "error": f"missing script: {script_path}"}
    wipe_script_name = str(console_value("wipe_script_name", WIPE_SCRIPT_NAME) or WIPE_SCRIPT_NAME)
    rendered_path = ROOT / ".tmp" / f"{wipe_script_name}.rendered.sh"
    rendered_path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(rendered_path, rendered_wipe_script_text())

    read_result = subprocess.run(
        maas_cli("node-script", "read", wipe_script_name),
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    exists = read_result.returncode == 0
    if exists:
        command = maas_cli(
            "node-script",
            "update",
            wipe_script_name,
            f"script@={rendered_path}",
            "comment=sync wipe script from web console",
        )
        action = "update-wipe-script"
    else:
        command = maas_cli(
            "node-scripts",
            "create",
            f"name={wipe_script_name}",
            f"script@={rendered_path}",
            "comment=register wipe script from web console",
        )
        action = "create-wipe-script"
    result = maas_cli_action(action, command, dry_run=dry)
    if result.get("ok"):
        prefix = "已更新" if exists else "已注册"
        tool_base_url = raid_value("tools_base_url", derive_tools_base_url(read_lab_config()))
        result["output"] = sanitize_text(
            f"{prefix} {wipe_script_name}\n"
            f"tools_base_url={tool_base_url}\n"
            f"storage_config_url=http://{stage1_server_ip()}:{os.environ.get('MAAS_CONSOLE_PORT', '8088')}/api/storage-config\n"
            f"boot_vd_name={raid_value('boot_vd_name', 'ssd01')}\n"
            f"single_disk_raid_level={raid_value('single_disk_raid_level', 'r0')}\n"
            f"multi_disk_raid_level={raid_value('multi_disk_raid_level', 'r1')}\n"
            f"boot_disk_count={raid_value('boot_disk_count', 2)}\n"
            f"{result.get('output', '')}".strip()
        )
    return result


def wipe_ready_nodes(dry=False):
    node_keys = [
        node.get("node_key") for node in flow_nodes(build_nodes(), {"ready", "failed testing"})
        if not (node.get("storage_status") or {}).get("ok")
    ]
    if not node_keys:
        return {"ok": False, "error": f"没有命中 tag={console_value('flow_tag', FLOW_TAG)} 且等待清盘/创建 RAID 的节点"}
    return wipe_nodes(node_keys, dry=dry)


def apply_storage_ready_nodes(dry=False):
    node_keys = [
        node.get("node_key") for node in flow_nodes(build_nodes(), {"ready", "failed deployment"})
        if int(node.get("block_device_count") or 0) > 0
        and not (node.get("storage_status") or {}).get("ok")
    ]
    if not node_keys:
        return {"ok": False, "error": f"没有命中 tag={console_value('flow_tag', FLOW_TAG)} 且可套盘的节点"}
    return apply_storage_nodes(node_keys, dry=dry)


def deploy_ready_nodes(dry=False):
    node_keys = [
        node.get("node_key") for node in flow_nodes(build_nodes(), {"ready", "failed deployment"})
        if ((node.get("actions") or action_policy(node)).get("deploy") or {}).get("allowed")
    ]
    if not node_keys:
        return {"ok": False, "error": f"没有命中 tag={console_value('flow_tag', FLOW_TAG)} 且可部署的节点"}
    return deploy_nodes(node_keys, dry=dry)


def lab_server():
    return (read_lab_config().get("server") or {})


def lab_clients():
    return effective_lab_config().get("clients") or []


def storage_config_for_sn(sn):
    wanted = str(sn or "").strip().lower()
    client = next(
        (
            item for item in lab_clients()
            if str(item.get("sn") or item.get("node_id") or "").strip().lower() == wanted
        ),
        None,
    )
    if not client:
        return None
    policy = client.get("raid") or {}
    return {
        "sn": client.get("sn") or sn,
        "boot_vd_name": policy.get("boot_vd_name", raid_value("boot_vd_name", "ssd01")),
        "single_disk_raid_level": policy.get("single_disk_raid_level", raid_value("single_disk_raid_level", "r0")),
        "multi_disk_raid_level": policy.get("multi_disk_raid_level", raid_value("multi_disk_raid_level", "r1")),
        "boot_disk_count": policy.get("boot_disk_count", raid_value("boot_disk_count", 2)),
        "data_disk_raid_layout": policy.get("data_disk_raid_layout", raid_value("data_disk_raid_layout", []) or []),
    }


def server_value(name, default=""):
    value = lab_server().get(name)
    return str(value if value is not None else default)


def console_value(name, default=""):
    value = lab_console().get(name)
    return value if value is not None else default


def raid_value(name, default=""):
    value = lab_raid().get(name)
    return value if value is not None else default


def stage1_server_ip():
    server = lab_server()
    explicit = primary_ip(server.get("stage1_server_ip") or server.get("diskless_server_ip"))
    if explicit:
        return explicit
    _, inferred = infer_stage1_local_endpoint()
    return inferred or primary_ip(server.get("external_ip")) or "127.0.0.1"


def dhcp_range_parts():
    text = server_value("dhcp_range", "")
    parts = [part.strip() for part in text.split(",")]
    return (parts + ["", ""])[:2]


def local_ipv4_endpoints():
    endpoints = []
    if not shutil.which("ip"):
        return endpoints
    try:
        result = subprocess.run(
            ["ip", "-o", "-4", "addr", "show"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return endpoints
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            endpoints.append((parts[1].split("@", 1)[0], parts[3]))
    return endpoints


def infer_stage1_local_endpoint():
    """Choose the local NIC/address on the planned nodes' deployment subnet."""
    import ipaddress

    server = lab_server()
    configured_interface = clean_optional_text(server.get("dhcp_interface"))
    configured_ip = primary_ip(server.get("stage1_server_ip") or server.get("diskless_server_ip"))
    endpoints = local_ipv4_endpoints()
    for interface, cidr in endpoints:
        address = primary_ip(cidr)
        if configured_ip and address == configured_ip and (not configured_interface or interface == configured_interface):
            return interface, address

    planned_networks = []
    for client in effective_lab_config().get("clients") or []:
        value = clean_optional_text(client.get("25g")).split(",", 1)[0]
        if not value or "/" not in value:
            continue
        try:
            planned_networks.append(ipaddress.ip_interface(value).network)
        except ValueError:
            continue
    for interface, cidr in endpoints:
        if configured_interface and interface != configured_interface:
            continue
        try:
            address = ipaddress.ip_interface(cidr).ip
        except ValueError:
            continue
        if any(address in network for network in planned_networks):
            return interface, str(address)

    for interface, cidr in endpoints:
        if configured_interface and interface == configured_interface:
            return interface, primary_ip(cidr)
    return configured_interface, configured_ip


def infer_stage1_dhcp_settings():
    """Infer the narrowest safe Stage1 DHCP setup from the saved node plan."""
    interface = server_value("dhcp_interface", "")
    start_ip, end_ip = dhcp_range_parts()
    inferred_interface, server_ip = infer_stage1_local_endpoint()
    interface = interface or inferred_interface

    if not start_ip or not end_ip:
        planned_ips = []
        for client in effective_lab_config().get("clients") or []:
            address = primary_ip(client.get("25g"))
            if address and address != server_ip:
                planned_ips.append(address)
        if planned_ips:
            # Stage1 only needs leases for planned nodes.  Keeping this range
            # narrow avoids accidentally becoming a general LAN DHCP server.
            import ipaddress

            try:
                ordered = sorted({ipaddress.ip_address(item) for item in planned_ips})
                start_ip = start_ip or str(ordered[0])
                end_ip = end_ip or str(ordered[-1])
            except ValueError:
                pass
    return interface, start_ip, end_ip


def infer_stage1_gateway(server_ip):
    import ipaddress

    for client in effective_lab_config().get("clients") or []:
        parts = [part.strip() for part in clean_optional_text(client.get("25g")).split(",")]
        if len(parts) < 2 or not parts[1]:
            continue
        try:
            network = ipaddress.ip_interface(parts[0]).network
            gateway = ipaddress.ip_address(parts[1])
            if ipaddress.ip_address(server_ip) in network and gateway in network:
                return str(gateway)
        except ValueError:
            continue
    # server.dhcp_router may describe the management LAN in older configs.
    # Without a matching client CIDR it is safer to keep Stage1 traffic local
    # than to advertise an unrelated gateway.
    return server_ip


def pxe_mode_common_args():
    args = [
        "--offline-root", server_value("offline_root", "/srv/maas-offline"),
        "--http-port", server_value("http_port", "8083"),
        "--stage1-port", server_value("stage1_port", "8091"),
    ]
    interface, start_ip, end_ip = infer_stage1_dhcp_settings()
    if interface and start_ip and end_ip:
        args += ["--maas-dhcp-interface", interface, "--maas-dhcp-start", start_ip, "--maas-dhcp-end", end_ip]
        args += ["--maas-dhcp-gateway", infer_stage1_gateway(stage1_server_ip())]
        if server_value("dhcp_dns", ""):
            args += ["--maas-dhcp-dns", server_value("dhcp_dns")]
    return args


def maas_install_args(dry=False):
    server_ip = server_value("external_ip", "127.0.0.1")
    args = [
        str(ROOT / "docs/maas-control-plane-oneclick.sh"),
        "--server-ip", server_ip,
        "--maas-url", server_value("maas_url", f"http://{server_ip}:5240/MAAS"),
        "--offline-root", server_value("offline_root", "/srv/maas-offline"),
        "--http-port", server_value("http_port", "8083"),
        "--admin-user", server_value("admin_user", "admin"),
        "--admin-password", server_value("admin_password"),
        "--install-curtin-template",
    ]
    if dry:
        args.append("--dry-run")
    return args


def diskless_install_args(dry=False):
    server = lab_server()
    args = [
        str(ROOT / "docs/diskless-stage1-oneclick.sh"),
        "--server-ip", stage1_server_ip(),
        "--offline-root", server_value("offline_root", "/srv/maas-offline"),
        "--http-port", server_value("http_port", "8083"),
        "--stage1-port", server_value("stage1_port", "8091"),
        "--uefi-ipxe-source", server_value("stage1_uefi_ipxe_source", "ipxe.efi"),
    ]
    interface, start_ip, end_ip = infer_stage1_dhcp_settings()
    if interface and start_ip and end_ip:
        args += ["--dhcp-interface", interface, "--dhcp-range", f"{start_ip},{end_ip},12h"]
        # The control-plane address is the correct default gateway for an
        # isolated provisioning network when none was explicitly configured.
        args += ["--dhcp-router", infer_stage1_gateway(stage1_server_ip())]
        if server.get("dhcp_dns"):
            args += ["--dhcp-dns", server_value("dhcp_dns")]
    if dry:
        args.append("--dry-run")
    return args


def validate_diskless_install_settings():
    interface, start_ip, end_ip = infer_stage1_dhcp_settings()
    missing = []
    if not interface:
        missing.append("server.dhcp_interface（无法按 server.stage1_server_ip/external_ip 自动识别网卡）")
    if not start_ip or not end_ip:
        missing.append("server.dhcp_range（且节点 CSV 没有可推导的 25g 地址）")
    if missing:
        return {
            "ok": False,
            "error": "无盘 DHCP/TFTP 配置不完整，不能安装可切换的无盘服务",
            "details": missing,
            "output": "\n".join(f"- 缺少 {item}" for item in missing),
        }
    return None


def reinstall_control_service(name, install_args, dry=False):
    """Repair/reinstall a control-plane component and restore the prior PXE mode."""
    previous_mode = active_service_mode()
    restore_mode = previous_mode if previous_mode != "conflict" else "maintenance_locked"
    mode_script = str(ROOT / "docs/scripts/maas_pxe_mode.sh")
    common = pxe_mode_common_args()
    # Always normalize first. This also repairs a partially-maintained state
    # where an auxiliary service was left active without a DHCP/TFTP conflict.
    commands = [["sudo", "-n", mode_script, "maintenance_locked", *common]]
    commands.append(["sudo", "-n", *list(install_args)])
    # Installers may start auxiliary HTTP/collector units. Re-apply even the
    # maintenance target so the action cannot leave a mixed partial state.
    commands.append(["sudo", "-n", mode_script, restore_mode, *common])
    if dry:
        return {
            "ok": True,
            "code": 0,
            "action": name,
            "output": "\n".join("[dry-run] " + " ".join(shell_quote(arg) for arg in command) for command in commands),
        }
    if not ALLOW_MUTATION:
        return {"ok": False, "error": "mutation disabled; set MAAS_CONSOLE_ALLOW_MUTATION=1"}
    outputs = []
    for command in commands:
        result = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        outputs.append(f"$ {' '.join(shell_quote(arg) for arg in command)}\n{result.stdout or ''}".rstrip())
        if result.returncode != 0:
            return {"ok": False, "code": result.returncode, "action": name, "output": sanitize_text("\n\n".join(outputs)[-12000:])}
    return {"ok": True, "code": 0, "action": name, "output": sanitize_text("\n\n".join(outputs)[-12000:])}


def stage1_validate_args(skip_systemd_checks=False):
    args = [
        str(ROOT / "docs/scripts/validate_stage1_pxe.sh"),
        "--offline-root", server_value("offline_root", "/srv/maas-offline"),
        "--http-port", server_value("http_port", "8083"),
        "--stage1-port", server_value("stage1_port", "8091"),
    ]
    if skip_systemd_checks:
        args.append("--skip-systemd-checks")
    return args


def write_text_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def load_yaml(path):
    try:
        import yaml
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def automation_login():
    data = load_yaml(POLICY_FILES["default_user_data"])
    users = data.get("users") or []
    username = next(
        (str(item.get("name")) for item in users if isinstance(item, dict) and item.get("name")),
        "ubuntu",
    )
    password = ""
    chpasswd = data.get("chpasswd") or {}
    entries = chpasswd.get("users") or chpasswd.get("list") or []
    if isinstance(entries, str):
        entries = entries.splitlines()
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("name") or "").strip() == username:
            password = str(entry.get("password") or "").strip()
            break
        candidate, separator, value = str(entry).partition(":")
        if separator and candidate.strip() == username:
            password = value.strip()
            break
    if not password:
        content = POLICY_FILES["default_user_data"].read_text(encoding="utf-8")
        match = re.search(
            rf"name:\s*{re.escape(username)}\s*,\s*password:\s*['\"]([^'\"]+)['\"]",
            content,
        )
        if match:
            password = match.group(1)
    return username, password


def persist_connectivity_result(key, result):
    with CONNECTIVITY_LOCK:
        state = read_json(CONNECTIVITY_STATE, {"nodes": {}})
        state.setdefault("nodes", {})[key] = result
        write_text_atomic(CONNECTIVITY_STATE, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def run_connectivity_check(node):
    key = node_state_key(node)
    host = primary_ip(node.get("25g"))
    started_at = int(time.time())
    result = {
        "status": "running",
        "host": host,
        "started_at": started_at,
        "updated_at": started_at,
        "checks": {},
    }
    persist_connectivity_result(key, result)
    try:
        if not host:
            raise RuntimeError("节点未配置可检测的业务 IP")
        ping = subprocess.run(
            ["ping", "-c", "1", "-W", "2", host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        result["checks"]["icmp"] = ping.returncode == 0
        with socket.create_connection((host, 22), timeout=5):
            result["checks"]["tcp_22"] = True
        try:
            import paramiko
        except ImportError as exc:
            raise RuntimeError("控制节点缺少 python3-paramiko") from exc
        username, password = automation_login()
        if not username or not password:
            raise RuntimeError("default-user-data.yaml 未配置可用于 SSH 的默认账号密码")
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(host, username=username, password=password, timeout=10, banner_timeout=10, auth_timeout=10)
            _stdin, stdout, stderr = client.exec_command("hostname", timeout=10)
            actual_hostname = stdout.read().decode("utf-8", errors="replace").strip()
            error = stderr.read().decode("utf-8", errors="replace").strip()
            if stdout.channel.recv_exit_status() != 0:
                raise RuntimeError(error or "SSH 登录成功但远程命令执行失败")
            result["checks"]["ssh_login"] = True
            result["remote_hostname"] = actual_hostname
            result["username"] = username
        finally:
            client.close()
        result["status"] = "passed"
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = str(exc)
    finally:
        result["updated_at"] = int(time.time())
        result["finished_at"] = int(time.time())
        persist_connectivity_result(key, result)
        with CONNECTIVITY_LOCK:
            CONNECTIVITY_ACTIVE.discard(key)


def ensure_connectivity_checks(nodes, force_keys=None):
    force_keys = set(force_keys or [])
    state = read_json(CONNECTIVITY_STATE, {"nodes": {}})
    now = int(time.time())
    for node in nodes:
        if node_status_key(node) != "deployed":
            continue
        key = node_state_key(node)
        host = primary_ip(node.get("25g"))
        if not key:
            continue
        previous = (state.get("nodes") or {}).get(key) or {}
        retry_due = previous.get("status") == "failed" and now - int(previous.get("updated_at") or 0) >= 60
        stale_pending = previous.get("status") in {"queued", "running"} and now - int(previous.get("updated_at") or 0) >= 30
        needs_check = (
            key in force_keys
            or not previous
            or previous.get("host") != host
            or retry_due
            or stale_pending
        )
        if not needs_check:
            continue
        with CONNECTIVITY_LOCK:
            if key in CONNECTIVITY_ACTIVE:
                continue
            CONNECTIVITY_ACTIVE.add(key)
        queued = {"status": "queued", "host": host, "updated_at": now, "checks": {}}
        persist_connectivity_result(key, queued)
        threading.Thread(target=run_connectivity_check, args=(dict(node),), daemon=True).start()


def safe_bundle_id(value):
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip()).strip(".-")[:80]


def bundle_metadata(path):
    manifest = load_yaml(path / "manifest.yaml")
    playbook = str(manifest.get("playbook") or "site.yml").strip()
    playbook_path = (path / playbook).resolve()
    valid_playbook = playbook_path.is_file() and path.resolve() in playbook_path.parents
    return {
        "id": path.name,
        "name": str(manifest.get("name") or path.name),
        "version": str(manifest.get("version") or ""),
        "description": str(manifest.get("description") or ""),
        "playbook": playbook,
        "valid": bool(manifest and valid_playbook),
        "checks": manifest.get("checks") if isinstance(manifest.get("checks"), list) else [],
        "sha256": (path / ".sha256").read_text(encoding="utf-8").strip() if (path / ".sha256").exists() else "",
    }


def list_bundles():
    if not ANSIBLE_BUNDLES.exists():
        return []
    return [bundle_metadata(path) for path in sorted(ANSIBLE_BUNDLES.iterdir()) if path.is_dir()]


def install_bundle(filename, encoded):
    if not filename.lower().endswith((".tar.gz", ".tgz")):
        raise ValueError("仅支持 .tar.gz 或 .tgz 剧本包")
    try:
        content = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ValueError("剧本包不是有效的 base64 数据") from exc
    if not content or len(content) > 128 * 1024 * 1024:
        raise ValueError("剧本包为空或超过 128 MiB")
    digest = hashlib.sha256(content).hexdigest()
    upload_root = AUTOMATION_ROOT / f".upload-{uuid.uuid4().hex}"
    archive = upload_root / "bundle.tgz"
    extract_root = upload_root / "extract"
    upload_root.mkdir(parents=True, exist_ok=False)
    archive.write_bytes(content)
    extract_root.mkdir()
    try:
        with tarfile.open(archive, "r:gz") as handle:
            members = handle.getmembers()
            if len(members) > 10000:
                raise ValueError("剧本包文件数量超过限制")
            for member in members:
                normalized = Path(member.name)
                if normalized.is_absolute() or ".." in normalized.parts:
                    raise ValueError(f"剧本包包含不安全路径: {member.name}")
                if member.issym() or member.islnk() or member.isdev():
                    raise ValueError(f"剧本包包含不允许的文件类型: {member.name}")
            handle.extractall(extract_root)
        roots = [path for path in extract_root.iterdir() if path.name != "__MACOSX"]
        source = roots[0] if len(roots) == 1 and roots[0].is_dir() else extract_root
        manifest_path = source / "manifest.yaml"
        if not manifest_path.is_file():
            raise ValueError("剧本包根目录缺少 manifest.yaml")
        try:
            import yaml
            manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"manifest.yaml 格式错误: {exc}") from exc
        if not isinstance(manifest, dict):
            raise ValueError("manifest.yaml 必须是 YAML 对象")
        bundle_id = safe_bundle_id(manifest.get("id") or manifest.get("name") or filename.rsplit(".", 2)[0])
        if not bundle_id:
            raise ValueError("manifest.yaml 缺少有效 name 或 id")
        target = ANSIBLE_BUNDLES / bundle_id
        if target.exists():
            raise ValueError(f"剧本包 {bundle_id} 已存在，请修改 id 或先更新版本标识")
        ANSIBLE_BUNDLES.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        (target / ".sha256").write_text(digest + "\n", encoding="utf-8")
        metadata = bundle_metadata(target)
        if not metadata["valid"]:
            shutil.rmtree(target)
            raise ValueError(f"找不到 manifest 指定的 playbook: {metadata['playbook']}")
        return metadata
    finally:
        shutil.rmtree(upload_root, ignore_errors=True)


def job_path(job_id):
    return AUTOMATION_JOBS / safe_bundle_id(job_id)


def read_job(job_id):
    path = job_path(job_id)
    data = read_json(path / "job.json", {})
    if not data:
        return None
    if data.get("status") == "running" and not (path / ".active").exists():
        data["status"] = "interrupted"
    log_path = path / "stdout.log"
    data["output"] = sanitize_text(log_path.read_text(encoding="utf-8", errors="replace")[-50000:] if log_path.exists() else "")
    return data


def write_job(job_id, data):
    path = job_path(job_id)
    path.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path / "job.json", json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def list_jobs():
    if not AUTOMATION_JOBS.exists():
        return []
    jobs = [read_job(path.name) for path in AUTOMATION_JOBS.iterdir() if path.is_dir()]
    return sorted((item for item in jobs if item), key=lambda item: item.get("created_at", 0), reverse=True)[:50]


def selected_automation_nodes(node_keys):
    wanted = set(node_keys or [])
    nodes = [node for node in build_nodes() if not wanted or node.get("node_key") in wanted]
    return [
        node for node in nodes
        if node_status_key(node) == "deployed"
        and primary_ip(node.get("25g"))
        and connectivity_for_node(node).get("status") == "passed"
    ]


def new_job(kind, node_keys, bundle_id="", check_mode=False):
    job_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    job = {
        "id": job_id,
        "kind": kind,
        "bundle_id": bundle_id,
        "node_keys": list(node_keys),
        "check_mode": bool(check_mode),
        "status": "queued",
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }
    write_job(job_id, job)
    return job


def update_job(job_id, **values):
    with JOB_LOCK:
        job = read_json(job_path(job_id) / "job.json", {})
        job.update(values)
        job["updated_at"] = int(time.time())
        write_job(job_id, job)
    return job


def append_job_log(job_id, text):
    path = job_path(job_id) / "stdout.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(sanitize_text(text))


def run_ansible_job(job_id):
    active = job_path(job_id) / ".active"
    active.touch()
    try:
        job = update_job(job_id, status="running", started_at=int(time.time()))
        bundle = next((item for item in list_bundles() if item["id"] == job.get("bundle_id")), None)
        nodes = selected_automation_nodes(job.get("node_keys"))
        executable = shutil.which("ansible-playbook")
        if not executable:
            raise RuntimeError("控制节点未安装 ansible-playbook")
        if not bundle or not bundle.get("valid"):
            raise RuntimeError("剧本包不存在或无效")
        if not nodes:
            raise RuntimeError("未选择已部署且具有业务 IP 的节点")
        username, password = automation_login()
        if not password:
            raise RuntimeError("default-user-data.yaml 未提供可用于 SSH 的默认账号密码")
        inventory = {"all": {"hosts": {}, "vars": {"ansible_user": username, "ansible_password": password, "ansible_become_password": password, "ansible_connection": "paramiko", "ansible_python_interpreter": "/usr/bin/python3"}}}
        for node in nodes:
            inventory_name = str(node.get("hostname") or node.get("system_id"))
            inventory["all"]["hosts"][inventory_name] = {"ansible_host": primary_ip(node.get("25g")), "maas_node_key": node.get("node_key")}
        inventory_path = job_path(job_id) / "inventory.json"
        inventory_path.write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(inventory_path, 0o600)
        bundle_path = ANSIBLE_BUNDLES / bundle["id"]
        command = [executable, "-i", str(inventory_path), str(bundle_path / bundle["playbook"])]
        if job.get("check_mode"):
            command.append("--check")
        append_job_log(job_id, f"$ ansible-playbook -i inventory.json {bundle['playbook']}{' --check' if job.get('check_mode') else ''}\n")
        ansible_env = os.environ.copy()
        ansible_env["ANSIBLE_HOST_KEY_CHECKING"] = "False"
        with (job_path(job_id) / "stdout.log").open("a", encoding="utf-8") as output:
            result = subprocess.run(command, cwd=bundle_path, env=ansible_env, text=True, stdout=output, stderr=subprocess.STDOUT, check=False)
        update_job(job_id, status="succeeded" if result.returncode == 0 else "failed", code=result.returncode, finished_at=int(time.time()))
    except Exception as exc:
        append_job_log(job_id, f"ERROR: {exc}\n")
        update_job(job_id, status="failed", error=str(exc), finished_at=int(time.time()))
    finally:
        active.unlink(missing_ok=True)


def evaluate_check(actual, spec):
    expected = spec.get("expected", "")
    operator = spec.get("operator", "equals")
    if operator == "contains":
        return str(expected) in actual
    if operator == "regex":
        return bool(re.search(str(expected), actual))
    if operator == "min_bytes":
        try:
            return int(actual.strip()) >= int(expected)
        except Exception:
            return False
    return actual.strip() == str(expected).strip()


def run_compliance_job(job_id):
    active = job_path(job_id) / ".active"
    active.touch()
    try:
        job = update_job(job_id, status="running", started_at=int(time.time()))
        nodes = selected_automation_nodes(job.get("node_keys"))
        if not nodes:
            raise RuntimeError("未选择已部署且具有业务 IP 的节点")
        try:
            import paramiko
        except ImportError as exc:
            raise RuntimeError("控制节点缺少 python3-paramiko") from exc
        username, password = automation_login()
        if not password:
            raise RuntimeError("default-user-data.yaml 未提供可用于 SSH 的默认账号密码")
        state = read_json(COMPLIANCE_STATE, {"nodes": {}})
        results = []
        for node in nodes:
            host = primary_ip(node.get("25g"))
            node_result = {"node_key": node.get("node_key"), "hostname": node.get("hostname"), "system_id": node.get("system_id"), "host": host, "checks": [], "checked_at": int(time.time())}
            try:
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                client.connect(host, username=username, password=password, timeout=12, banner_timeout=12, auth_timeout=12)
                checks = []
                bundle_id = job.get("bundle_id")
                if bundle_id:
                    bundle = next((item for item in list_bundles() if item["id"] == bundle_id), None)
                    if bundle:
                        checks.extend(bundle.get("checks") or [])
                for spec in checks:
                    command = str(spec.get("command") or "").strip()
                    if not command:
                        continue
                    _stdin, stdout, stderr = client.exec_command(command, timeout=30)
                    actual = stdout.read().decode("utf-8", errors="replace").strip()
                    error = stderr.read().decode("utf-8", errors="replace").strip()
                    passed = stdout.channel.recv_exit_status() == 0 and evaluate_check(actual, spec)
                    node_result["checks"].append({"id": spec.get("id"), "label": spec.get("label") or spec.get("id"), "passed": passed, "actual": actual, "expected": spec.get("expected"), "error": error})
                client.close()
                if not node_result["checks"]:
                    node_result["status"] = "not_configured"
                else:
                    node_result["status"] = "passed" if all(item["passed"] for item in node_result["checks"]) else "failed"
            except Exception as exc:
                node_result["status"] = "unreachable"
                node_result["error"] = str(exc)
            system_id = str(node.get("system_id") or "")
            if system_id:
                state.setdefault("nodes", {})[system_id] = node_result
            results.append(node_result)
            append_job_log(job_id, f"{node_result['hostname']}: {node_result['status']}\n")
        write_text_atomic(COMPLIANCE_STATE, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        success = all(item.get("status") in {"passed", "not_configured"} for item in results)
        update_job(job_id, status="succeeded" if success else "failed", results=results, finished_at=int(time.time()))
    except Exception as exc:
        append_job_log(job_id, f"ERROR: {exc}\n")
        update_job(job_id, status="failed", error=str(exc), finished_at=int(time.time()))
    finally:
        active.unlink(missing_ok=True)


def automation_payload():
    eligible_nodes = selected_automation_nodes([])
    return {
        "ok": True,
        "runtime": {
            "ansible_available": bool(shutil.which("ansible-playbook")),
            "ansible_path": shutil.which("ansible-playbook") or "",
            "paramiko_available": bool(importlib.util.find_spec("paramiko")),
        },
        "repository": str(ANSIBLE_BUNDLES),
        "eligible_node_keys": [node.get("node_key") for node in eligible_nodes],
        "bundles": list_bundles(),
        "jobs": list_jobs(),
    }


def yaml_scalar(value):
    text = str(value or "")
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def sync_lab_stage1(dry=False):
    config = effective_lab_config()
    clients = config.get("clients") or []
    validation_errors = config.get("validation_errors") or []
    if validation_errors:
        return {"ok": False, "error": "配置不完整，无法生成节点清单", "details": validation_errors, "output": "\n".join(validation_errors)}
    fieldnames = [
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
    inventory_path = SOURCES / "stage1/inventory.csv"
    defaults_path = SOURCES / "stage1/defaults.yaml"
    state_path = SOURCES / "stage1/state.json"
    console = config.get("console") or {}
    defaults_cfg = config.get("defaults") or {}
    default_type = ((config.get("node_types") or {}).get("default") or {})
    defaults = {
        "node_id": console.get("default_node_id", ""),
        "bmc_user": defaults_cfg.get("bmc_user", ""),
        "bmc_pass": defaults_cfg.get("bmc_pass", ""),
        "tag": console.get("default_client_tag", "physical-test,no-gpu"),
        "25g": "",
        "25g_mode": "",
        "power_driver": defaults_cfg.get("power_driver", "ipmi"),
        "power_driver_fallback": defaults_cfg.get("power_driver_fallback", ""),
        "boot_mode": defaults_cfg.get("boot_mode", "uefi"),
        "network_mode": ((default_type.get("networking") or {}).get("mode") or "bond25g"),
        "25g_apply": str(((default_type.get("networking") or {}).get("apply_on_first_boot", True))).lower(),
        "boot_vd_name": raid_value("boot_vd_name", "ssd01"),
        "single_disk_raid_level": raid_value("single_disk_raid_level", "r0"),
        "multi_disk_raid_level": raid_value("multi_disk_raid_level", "r1"),
        "boot_disk_count": raid_value("boot_disk_count", 2),
        "data_disk_raid_layout": json.dumps(raid_value("data_disk_raid_layout", []) or [], ensure_ascii=False),
    }

    import io

    handle = io.StringIO()
    writer = csv.DictWriter(handle, fieldnames=fieldnames)
    writer.writeheader()
    for index, client in enumerate(clients, start=2):
        writer.writerow({
            "hostname": client.get("hostname") or f"node-physical-{index}",
            "pxe_mac": client.get("pxe_mac", ""),
            "bmc_ip": client.get("bmc_ip", ""),
            "bmc_user": client.get("bmc_user", ""),
            "bmc_pass": client.get("bmc_pass", ""),
            "node_id": resolved_node_id(client.get("node_id"), client.get("sn"), console.get("default_node_id")),
            "sn": client.get("sn", ""),
            "25g": client.get("25g", ""),
            "25g_mode": client.get("25g_mode", ""),
            "tag": client.get("tag", console.get("default_client_tag", "physical-test,no-gpu")),
            "type": client.get("type", "default"),
            "power_driver": client.get("power_driver", defaults_cfg.get("power_driver", "ipmi")),
            "power_driver_fallback": client.get("power_driver_fallback", defaults_cfg.get("power_driver_fallback", "")),
            "boot_mode": client.get("boot_mode", defaults_cfg.get("boot_mode", "uefi")),
            "network_mode": ((client.get("networking") or {}).get("mode") or "bond25g"),
            "25g_apply": str(((client.get("networking") or {}).get("apply_on_first_boot", True))).lower(),
            "boot_vd_name": (client.get("raid") or {}).get("boot_vd_name", raid_value("boot_vd_name", "ssd01")),
            "single_disk_raid_level": (client.get("raid") or {}).get("single_disk_raid_level", raid_value("single_disk_raid_level", "r0")),
            "multi_disk_raid_level": (client.get("raid") or {}).get("multi_disk_raid_level", raid_value("multi_disk_raid_level", "r1")),
            "boot_disk_count": (client.get("raid") or {}).get("boot_disk_count", raid_value("boot_disk_count", 2)),
            "data_disk_raid_layout": json.dumps((client.get("raid") or {}).get("data_disk_raid_layout", []), ensure_ascii=False),
        })
    defaults_text = "defaults:\n" + "".join(f"  {key}: {yaml_scalar(value)}\n" for key, value in defaults.items())
    output = (
        f"inventory={inventory_path}\n"
        f"defaults={defaults_path}\n"
        f"state={state_path}\n\n"
        f"{handle.getvalue()}\n{defaults_text}"
    )
    if dry:
        return {"ok": True, "code": 0, "action": "sync-lab-stage1", "output": sanitize_text(output)}
    if not ALLOW_MUTATION:
        return {"ok": False, "error": "mutation disabled; set MAAS_CONSOLE_ALLOW_MUTATION=1"}
    write_text_atomic(inventory_path, handle.getvalue())
    write_text_atomic(defaults_path, defaults_text)
    if not state_path.exists():
        write_text_atomic(state_path, '{"reports": {}}\n')
    return {"ok": True, "code": 0, "action": "sync-lab-stage1", "output": sanitize_text(output)}


def reset_stage1_state(dry=False):
    state_path = SOURCES / "stage1/state.json"
    output = f"reset_state={state_path}\n"
    if dry:
        return {"ok": True, "code": 0, "action": "reset-stage1-state", "output": output}
    if not ALLOW_MUTATION:
        return {"ok": False, "error": "mutation disabled; set MAAS_CONSOLE_ALLOW_MUTATION=1"}
    write_text_atomic(state_path, '{"reports": {}}\n')
    return {"ok": True, "code": 0, "action": "reset-stage1-state", "output": output}


def ping_host(host):
    if not host:
        return "missing"
    result = subprocess.run(["ping", "-c", "1", "-W", "1", host], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return "ok" if result.returncode == 0 else "failed"


def network_check():
    lab = effective_lab_config()
    checks = []
    server = lab.get("server") or {}
    if server.get("external_ip"):
        checks.append({"name": server.get("hostname", "server"), "target": server["external_ip"], "status": ping_host(server["external_ip"]), "required": True})
    for item in lab.get("known_bmcs") or []:
        target = primary_ip(item.get("bmc_ip", ""))
        checks.append({"name": item.get("name", "bmc"), "target": target, "status": ping_host(target), "required": True})
    for client in lab.get("clients") or []:
        if client.get("external_ip"):
            checks.append({"name": client.get("hostname", "client"), "target": client["external_ip"], "status": ping_host(client["external_ip"]), "required": False})
        if client.get("bmc_ip"):
            target = primary_ip(client["bmc_ip"])
            checks.append({"name": f"{client.get('hostname', 'client')}-bmc", "target": target, "status": ping_host(target), "required": False})
    # A newly uploaded node may intentionally have no reachable management IP
    # yet: Stage1 configures and verifies that BMC through local IPMI/KCS. Keep
    # client reachability as a warning, while server/control-plane failures
    # remain blocking.
    ok = all(item["status"] == "ok" for item in checks if item.get("required"))
    return {"ok": ok, "checks": checks, "warnings": [item for item in checks if not item.get("required") and item["status"] != "ok"], "output": json.dumps(checks, ensure_ascii=False, indent=2)}


def stage1_precheck():
    effective = effective_lab_config()
    validation_errors = effective.get("validation_errors") or []
    confirmations = [
        "人工确认目标节点已拔盘；测试环境至少应先清空 RAID，确保节点不会从本地盘启动",
        "人工确认上传节点清单中的 hostname、sn、bmc_ip、bmc_user、bmc_pass 已核对无误",
        "人工确认目标节点已连接到 PXE 网口，并准备执行重启进入无盘抓配",
    ]
    if validation_errors:
        details = confirmations + [""] + [f"配置问题：{item}" for item in validation_errors]
        return {
            "ok": False,
            "error": "无盘抓配前置检查未通过，请先完善节点清单/BMC 配置并确认节点已处于拔盘状态",
            "details": validation_errors,
            "output": "\n".join(details),
        }
    pxe_check = run_action("validate-stage1-pxe", stage1_validate_args())
    if not pxe_check.get("ok"):
        output = pxe_check.get("output", "")
        details = [line for line in output.splitlines() if line.startswith("- ")]
        return {
            "ok": False,
            "error": "无盘抓配资源检查未通过，请先补齐 Stage1 PXE/TFTP 资源后再切换",
            "details": details,
            "output": "\n".join(confirmations + ["", output]).strip(),
        }
    return {
        "ok": True,
        "action": "stage1-precheck",
        "output": "\n".join(confirmations + ["", pxe_check.get("output", "")]).strip(),
    }


def csv_has_ready_rows(path):
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            return any(any(clean_optional_text(value) for value in (row or {}).values()) for row in reader)
    except Exception:
        return False


def import_nodes_action(dry=False):
    effective = effective_lab_config()
    validation_errors = effective.get("validation_errors") or []
    if validation_errors:
        return {
            "ok": False,
            "error": "配置不完整，不能导入 MAAS",
            "details": validation_errors,
            "output": "\n".join(validation_errors),
        }
    export_csv = SOURCES / "stage1/export/maas.csv"
    if not export_csv.exists():
        return {"ok": False, "error": f"缺少导入文件：{export_csv}，请先执行“生成 MAAS 导入表”"}
    if not csv_has_ready_rows(export_csv):
        return {"ok": False, "error": "maas.csv 没有可导入节点，请先完成抓配并重新生成导入表"}
    flow_tag = str(console_value("flow_tag", FLOW_TAG) or "").strip()
    args = [
        "env", f"FLOW_TAG={flow_tag}",
        "bash", str(ROOT / "docs/scripts/maas_bulk_import_and_tag.sh"), str(export_csv),
    ]
    return run_action("import-nodes", args, mutate=not dry, dry_run=dry)


def export_stage1_action(dry=False):
    effective = effective_lab_config()
    validation_errors = effective.get("validation_errors") or []
    if validation_errors:
        return {
            "ok": False,
            "error": "配置不完整，不能生成 MAAS 导入表",
            "details": validation_errors,
            "output": "\n".join(validation_errors),
        }
    inventory_path = SOURCES / "stage1/inventory.csv"
    defaults_path = SOURCES / "stage1/defaults.yaml"
    state_path = SOURCES / "stage1/state.json"
    output_dir = SOURCES / "stage1/export"
    args = [
        str(ROOT / "docs/scripts/stage1_collector.py"),
        "export",
        "--inventory", str(inventory_path),
        "--defaults", str(defaults_path),
        "--state", str(state_path),
        "--output-dir", str(output_dir),
    ]
    return run_action("export-stage1", args, mutate=not dry, dry_run=dry)


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        rel = urlparse(path).path.lstrip("/") or "index.html"
        return str(STATIC / rel)

    def send_json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_action_error(self, action, exc):
        self.send_json(
            {"ok": False, "action": action, "error": f"{type(exc).__name__}: {exc}"},
            status=500,
        )

    def read_json_body(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/summary":
            self.send_json(summary())
            return
        if parsed.path == "/api/config":
            self.send_json(config_payload())
            return
        if parsed.path == "/api/automation":
            self.send_json(automation_payload())
            return
        if parsed.path.startswith("/api/automation/jobs/"):
            job = read_job(unquote(parsed.path.rsplit("/", 1)[-1]))
            self.send_json(job or {"ok": False, "error": "job not found"}, status=200 if job else 404)
            return
        if parsed.path.startswith("/api/storage-config/"):
            sn = unquote(parsed.path.rsplit("/", 1)[-1])
            payload = storage_config_for_sn(sn)
            if payload is None:
                self.send_json({"ok": False, "error": f"storage config not found for SN={sn}"}, status=404)
            else:
                self.send_json(payload)
            return
        if parsed.path == "/api/actions/pxe-mode":
            mode = parse_qs(parsed.query).get("mode", ["status"])[0]
            dry = parse_qs(parsed.query).get("dry_run", ["1"])[0] != "0"
            confirm_stage1 = parse_qs(parsed.query).get("confirm_stage1", ["0"])[0] == "1"
            if mode in MODE_INFO and mode != "conflict":
                switch_gate = summary().get("control", {}).get("mode_gates", {}).get(mode, {})
                if switch_gate.get("allowed") is False:
                    self.send_json({"ok": False, "error": switch_gate.get("reason") or "当前流程状态不允许切换模式"}, status=409)
                    return
            if mode == "diskless_stage1":
                precheck = stage1_precheck()
                if not precheck.get("ok"):
                    self.send_json(precheck, status=400)
                    return
                if not confirm_stage1:
                    self.send_json(
                        {
                            "ok": False,
                            "error": "执行无盘抓配前，请先人工确认节点已拔盘/已清空 RAID，且 BMC 配置已核对",
                            "output": precheck.get("output", ""),
                        },
                        status=400,
                    )
                    return
            args = [
                "sudo", "-n",
                str(ROOT / "docs/scripts/maas_pxe_mode.sh"),
                mode,
                *pxe_mode_common_args(),
            ]
            if dry:
                args.append("--dry-run")
            self.send_json(run_action(f"pxe-mode:{mode}", args, mutate=not dry))
            return
        if parsed.path == "/api/actions/validate-sources":
            result = run_action("validate-sources", [str(ROOT / "docs/scripts/validate_maas_sources.sh"), str(SOURCES)])
            record_workflow_check("validate-sources", result)
            if not result.get("ok"):
                invalidate_workflow_check("network-check")
            self.send_json(result)
            return
        if parsed.path == "/api/actions/network-check":
            if not workflow_check_ok("validate-sources"):
                self.send_json({"ok": False, "error": "请先完成离线资源校验"}, status=409)
                return
            result = network_check()
            record_workflow_check("network-check", result)
            self.send_json(result)
            return
        if parsed.path == "/api/actions/sync-lab-stage1":
            blocked = require_service_modes(("maintenance_locked", "diskless_stage1"), "同步节点规划")
            if blocked:
                self.send_json(blocked, status=409)
                return
            dry = parse_qs(parsed.query).get("dry_run", ["1"])[0] != "0"
            self.send_json(sync_lab_stage1(dry=dry))
            return
        if parsed.path == "/api/actions/reset-stage1-state":
            blocked = require_service_mode("maintenance_locked", "清空抓配状态")
            if blocked:
                self.send_json(blocked, status=409)
                return
            dry = parse_qs(parsed.query).get("dry_run", ["1"])[0] != "0"
            self.send_json(reset_stage1_state(dry=dry))
            return
        if parsed.path == "/api/actions/install-maas":
            dry = parse_qs(parsed.query).get("dry_run", ["1"])[0] != "0"
            self.send_json(reinstall_control_service("install-maas", maas_install_args(dry=dry), dry=dry))
            return
        if parsed.path == "/api/actions/install-diskless":
            dry = parse_qs(parsed.query).get("dry_run", ["1"])[0] != "0"
            invalid = validate_diskless_install_settings()
            if invalid:
                self.send_json(invalid, status=400)
                return
            self.send_json(reinstall_control_service("install-diskless", diskless_install_args(dry=dry), dry=dry))
            return
        if parsed.path == "/api/actions/register-wipe-script":
            blocked = require_service_mode("maas_provision", "注册清盘脚本")
            if blocked:
                self.send_json(blocked, status=409)
                return
            dry = parse_qs(parsed.query).get("dry_run", ["1"])[0] != "0"
            self.send_json(ensure_wipe_script(dry=dry))
            return
        if parsed.path == "/api/actions/export-stage1":
            blocked = require_service_mode("diskless_stage1", "生成 MAAS 导入表")
            if blocked:
                self.send_json(blocked, status=409)
                return
            dry = parse_qs(parsed.query).get("dry_run", ["1"])[0] != "0"
            self.send_json(export_stage1_action(dry=dry))
            return
        if parsed.path == "/api/actions/import-nodes":
            blocked = require_service_mode("maas_provision", "导入节点")
            if blocked:
                self.send_json(blocked, status=409)
                return
            dry = parse_qs(parsed.query).get("dry_run", ["1"])[0] != "0"
            self.send_json(import_nodes_action(dry=dry))
            return
        if parsed.path == "/api/actions/wipe-ready":
            blocked = require_service_mode("maas_provision", "批量清盘/清 RAID")
            if blocked:
                self.send_json(blocked, status=409)
                return
            dry = parse_qs(parsed.query).get("dry_run", ["1"])[0] != "0"
            self.send_json(wipe_ready_nodes(dry=dry))
            return
        if parsed.path == "/api/actions/apply-storage-ready":
            blocked = require_service_mode("maas_provision", "批量套存储策略")
            if blocked:
                self.send_json(blocked, status=409)
                return
            dry = parse_qs(parsed.query).get("dry_run", ["1"])[0] != "0"
            self.send_json(apply_storage_ready_nodes(dry=dry))
            return
        if parsed.path == "/api/actions/deploy-ready":
            blocked = require_service_mode("maas_provision", "批量部署")
            if blocked:
                self.send_json(blocked, status=409)
                return
            dry = parse_qs(parsed.query).get("dry_run", ["1"])[0] != "0"
            self.send_json(deploy_ready_nodes(dry=dry))
            return
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/config":
            payload = self.read_json_body()
            config = payload.get("config", payload)
            try:
                validate_and_write_policy_files(payload.get("policy_files"))
                write_lab_config(config)
                self.send_json(config_payload())
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        if parsed.path == "/api/automation/bundles":
            payload = self.read_json_body()
            try:
                bundle = install_bundle(payload.get("filename", ""), payload.get("content_base64", ""))
                self.send_json({"ok": True, "bundle": bundle})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        if parsed.path == "/api/automation/jobs":
            if not ALLOW_MUTATION:
                self.send_json({"ok": False, "error": "当前控制台为仅预演模式，不能执行 Ansible"}, status=409)
                return
            payload = self.read_json_body()
            node_keys = payload.get("node_keys") or []
            bundle_id = safe_bundle_id(payload.get("bundle_id"))
            if not bundle_id:
                self.send_json({"ok": False, "error": "请选择剧本包"}, status=400)
                return
            if not shutil.which("ansible-playbook"):
                self.send_json({"ok": False, "error": "控制节点未安装 ansible-playbook；请补齐离线 Ansible 运行时后重试"}, status=409)
                return
            targets = selected_automation_nodes(node_keys)
            if not targets:
                self.send_json({"ok": False, "error": "没有已部署且通过网络/SSH 门禁的目标节点"}, status=409)
                return
            resolved_keys = [node.get("node_key") for node in targets]
            job = new_job("ansible", resolved_keys, bundle_id=bundle_id, check_mode=bool(payload.get("check_mode")))
            threading.Thread(target=run_ansible_job, args=(job["id"],), daemon=True).start()
            self.send_json({"ok": True, "job": job}, status=202)
            return
        if parsed.path == "/api/automation/checks":
            payload = self.read_json_body()
            node_keys = payload.get("node_keys") or []
            targets = selected_automation_nodes(node_keys)
            if not targets:
                self.send_json({"ok": False, "error": "没有已部署且通过网络/SSH 门禁的目标节点"}, status=409)
                return
            job = new_job("compliance", [node.get("node_key") for node in targets], bundle_id=safe_bundle_id(payload.get("bundle_id")))
            threading.Thread(target=run_compliance_job, args=(job["id"],), daemon=True).start()
            self.send_json({"ok": True, "job": job}, status=202)
            return
        if parsed.path == "/api/automation/connectivity":
            payload = self.read_json_body()
            node_keys = payload.get("node_keys") or []
            nodes = [node for node in build_nodes() if not node_keys or node.get("node_key") in set(node_keys)]
            ensure_connectivity_checks(nodes, force_keys=[node_state_key(node) for node in nodes])
            self.send_json({"ok": True, "queued": [node.get("node_key") for node in nodes if node_status_key(node) == "deployed"]}, status=202)
            return
        if parsed.path == "/api/actions/reboot-nodes":
            blocked = require_service_modes(("diskless_stage1", "maas_provision"), "重启节点")
            if blocked:
                self.send_json(blocked, status=409)
                return
            payload = self.read_json_body()
            dry = bool(payload.get("dry_run", not ALLOW_MUTATION))
            node_keys = payload.get("node_keys") or []
            self.send_json(reboot_nodes(node_keys, dry=dry))
            return
        if parsed.path == "/api/actions/reverify-bmc-nodes":
            blocked = require_service_modes(
                ("maintenance_locked", "diskless_stage1", "maas_provision"),
                "重新校验 BMC",
            )
            if blocked:
                self.send_json(blocked, status=409)
                return
            payload = self.read_json_body()
            dry = bool(payload.get("dry_run", not ALLOW_MUTATION))
            node_keys = payload.get("node_keys") or []
            self.send_json(reverify_bmc_nodes(node_keys, dry=dry))
            return
        if parsed.path == "/api/actions/recommission-nodes":
            blocked = require_service_mode("maas_provision", "重新 Commissioning")
            if blocked:
                self.send_json(blocked, status=409)
                return
            payload = self.read_json_body()
            dry = bool(payload.get("dry_run", not ALLOW_MUTATION))
            node_keys = payload.get("node_keys") or []
            self.send_json(recommission_nodes(node_keys, dry=dry))
            return
        if parsed.path == "/api/actions/delete-nodes":
            blocked = require_service_modes(("maintenance_locked", "diskless_stage1", "maas_provision"), "删除节点")
            if blocked:
                self.send_json(blocked, status=409)
                return
            payload = self.read_json_body()
            dry = bool(payload.get("dry_run", not ALLOW_MUTATION))
            node_keys = payload.get("node_keys") or []
            try:
                self.send_json(delete_nodes(node_keys, dry=dry))
            except Exception as exc:
                self.send_action_error("delete-nodes", exc)
            return
        if parsed.path == "/api/actions/wipe-nodes":
            blocked = require_service_mode("maas_provision", "清盘/清 RAID")
            if blocked:
                self.send_json(blocked, status=409)
                return
            payload = self.read_json_body()
            dry = bool(payload.get("dry_run", not ALLOW_MUTATION))
            node_keys = payload.get("node_keys") or []
            self.send_json(wipe_nodes(node_keys, dry=dry))
            return
        if parsed.path == "/api/actions/apply-storage-nodes":
            blocked = require_service_mode("maas_provision", "套存储策略")
            if blocked:
                self.send_json(blocked, status=409)
                return
            payload = self.read_json_body()
            dry = bool(payload.get("dry_run", not ALLOW_MUTATION))
            node_keys = payload.get("node_keys") or []
            self.send_json(apply_storage_nodes(node_keys, dry=dry))
            return
        if parsed.path == "/api/actions/deploy-nodes":
            blocked = require_service_mode("maas_provision", "系统部署")
            if blocked:
                self.send_json(blocked, status=409)
                return
            payload = self.read_json_body()
            dry = bool(payload.get("dry_run", not ALLOW_MUTATION))
            node_keys = payload.get("node_keys") or []
            self.send_json(deploy_nodes(node_keys, dry=dry))
            return
        self.send_json({"ok": False, "error": "not found"}, status=404)


def main():
    host = os.environ.get("MAAS_CONSOLE_HOST", "127.0.0.1")
    port = int(os.environ.get("MAAS_CONSOLE_PORT", "8088"))
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"MAAS console: http://{host}:{port}")
    print(f"MAAS sources: {SOURCES}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
