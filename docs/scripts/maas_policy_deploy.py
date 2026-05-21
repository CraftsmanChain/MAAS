#!/usr/bin/env python3

import argparse
import base64
import csv
import json
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    import yaml
except ImportError as exc:
    raise SystemExit(
        "missing dependency: PyYAML\n"
        "install with: sudo apt-get install -y python3-yaml"
    ) from exc


def deep_merge(base, override):
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            merged[key] = deep_merge(merged.get(key), value)
        return merged
    if override is None:
        return base
    return override


def run_cmd(cmd):
    return subprocess.run(cmd, check=True, text=True, capture_output=True)


def run_maas(profile, *args):
    return run_cmd(["maas", profile, *args]).stdout


def maas_json(profile, *args):
    return json.loads(run_maas(profile, *args))


def normalize_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def normalize_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def normalize_mac(value):
    if not value:
        return ""
    text = str(value).strip().lower().replace("-", ":")
    parts = [part.zfill(2) for part in text.split(":") if part]
    return ":".join(parts)


def first_non_empty(*values):
    for value in values:
        if value:
            return value
    return ""


def run_cmd_optional(cmd):
    return subprocess.run(cmd, check=False, text=True, capture_output=True)


def machine_tags(machine):
    tags = machine.get("tag_names")
    if tags:
        return normalize_list(tags)
    raw_tags = normalize_list(machine.get("tags"))
    names = []
    for item in raw_tags:
        if isinstance(item, dict) and item.get("name"):
            names.append(item["name"])
        elif isinstance(item, str):
            names.append(item)
    return names


def machine_mac_candidates(machine):
    candidates = set()
    boot_if = machine.get("boot_interface") or {}
    candidates.add(normalize_mac(boot_if.get("mac_address")))

    current_config = machine.get("current_config") or {}
    for iface in normalize_list(current_config.get("interface_set")):
        if isinstance(iface, dict):
            candidates.add(normalize_mac(iface.get("mac_address")))

    for nic in normalize_list(machine.get("interface_set")):
        if isinstance(nic, dict):
            candidates.add(normalize_mac(nic.get("mac_address")))

    return {item for item in candidates if item}


def resolve_profile(config, cli_profile):
    return cli_profile or config.get("defaults", {}).get("profile") or "admin"


def resolve_series(config, cli_series):
    return cli_series or config.get("defaults", {}).get("distro_series") or "jammy"


def load_csv_rows(csv_path):
    if not csv_path:
        return []
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [dict(row) for row in reader]


def parse_inline_mapping(value):
    if not value:
        return {}
    text = str(value).strip()
    if not text:
        return {}
    try:
        data = yaml.safe_load(text)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def policy_names_in_order(config):
    policies = config["policies"]
    configured = normalize_list(config.get("policy_match_order"))
    ordered = []
    for name in configured:
        if name in policies and name not in ordered:
            ordered.append(name)
    for name in policies.keys():
        if name not in ordered:
            ordered.append(name)
    if "default" in policies and ordered[-1] != "default":
        ordered = [name for name in ordered if name != "default"] + ["default"]
    return ordered


def policy_match_tags(name, policy):
    if "match_tags" not in policy:
        return [] if name == "default" else [name]
    return normalize_list(policy.get("match_tags"))


def resolve_policy(config, machine, forced_policy=None):
    policies = config.get("policies") or {}
    if not policies:
        raise SystemExit("invalid config: policies is empty")
    if "default" not in policies:
        raise SystemExit("invalid config: policies.default is required")
    if forced_policy:
        if forced_policy not in policies:
            raise SystemExit(f"policy not found: {forced_policy}")
        return forced_policy, "forced"

    tag_set = {tag.lower() for tag in machine_tags(machine)}
    for name in policy_names_in_order(config):
        if name == "default":
            continue
        match_tags = {tag.lower() for tag in policy_match_tags(name, policies[name])}
        if tag_set & match_tags:
            return name, "tag-match"
    return "default", "default-fallback"


def build_effective_policy(config, policy_name):
    defaults = config.get("defaults") or {}
    policy = (config.get("policies") or {}).get(policy_name) or {}
    merged = deep_merge(defaults, policy)
    merged["policy_name"] = policy_name
    return merged


def resolve_csv_hostname(machine, csv_rows):
    if not csv_rows:
        return ""

    machine_hostname = (machine.get("hostname") or "").strip().lower()
    macs = machine_mac_candidates(machine)

    for row in csv_rows:
        row_mac = normalize_mac(row.get("pxe_mac"))
        if row_mac and row_mac in macs and row.get("hostname"):
            return row["hostname"].strip()

    for row in csv_rows:
        row_hostname = (row.get("hostname") or "").strip()
        if row_hostname and row_hostname.lower() == machine_hostname:
            return row_hostname

    return ""


def resolve_csv_row(machine, csv_rows):
    if not csv_rows:
        return {}

    machine_hostname = (machine.get("hostname") or "").strip().lower()
    macs = machine_mac_candidates(machine)

    for row in csv_rows:
        row_mac = normalize_mac(row.get("pxe_mac"))
        if row_mac and row_mac in macs:
            return row

    for row in csv_rows:
        row_hostname = (row.get("hostname") or "").strip().lower()
        if row_hostname and row_hostname == machine_hostname:
            return row

    return {}


def sudo_rule(nopasswd):
    return "ALL=(ALL) NOPASSWD:ALL" if normalize_bool(nopasswd) else "ALL=(ALL) ALL"


def ssh_permit_root(value):
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value is None:
        return "yes"
    text = str(value).strip()
    return text or "yes"


def deep_merge_copy(base, override):
    return deep_merge(base or {}, override or {})


def parse_csv_tags(csv_row):
    raw_value = first_non_empty((csv_row or {}).get("tag"), (csv_row or {}).get("tags"))
    text = str(raw_value or "").strip().strip('"')
    if not text:
        return []
    tags = []
    seen = set()
    for item in text.replace(";", ",").split(","):
        tag = item.strip()
        if not tag:
            continue
        lowered = tag.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        tags.append(tag)
    return tags


def merge_machine_csv_tags(machine, csv_row):
    merged = dict(machine or {})
    combined = []
    seen = set()
    for tag in machine_tags(machine or {}) + parse_csv_tags(csv_row):
        lowered = str(tag or "").strip().lower()
        if not lowered or lowered in seen:
            continue
        seen.add(lowered)
        combined.append(str(tag).strip())
    merged["tag_names"] = combined
    return merged


def ensure_machine_tags(profile, sysid, csv_row, existing_machine=None, dry_run=False):
    desired_tags = parse_csv_tags(csv_row)
    if not desired_tags:
        return []

    existing_machine = existing_machine or {}
    existing = {tag.lower() for tag in machine_tags(existing_machine)}
    missing = [tag for tag in desired_tags if tag.lower() not in existing]
    if not missing or dry_run:
        return desired_tags

    for tag in missing:
        run_cmd_optional(["maas", profile, "tags", "create", f"name={tag}"])
        run_cmd(["maas", profile, "tag", "update-nodes", tag, f"add={sysid}"])
    return desired_tags


def parse_25g_value(raw_value):
    text = str(raw_value or "").strip()
    if not text:
        return {}
    parsed = parse_inline_mapping(text)
    if parsed:
        return parsed

    parts = [part.strip() for part in text.replace(";", ",").split(",") if part.strip()]
    if not parts:
        return {}
    data = {"address": parts[0]}
    if len(parts) > 1:
        data["gateway4"] = parts[1]
    return data


def normalize_bond_parameter_key(key):
    normalized = str(key or "").strip().lower().replace("-", "_")
    normalized = re.sub(r"\s+", "_", normalized)
    mapping = {
        "mode": "mode",
        "miimon": "mii-monitor-interval",
        "mii_monitor_interval": "mii-monitor-interval",
        "mii-monitor-interval": "mii-monitor-interval",
        "xmit_hash_policy": "transmit-hash-policy",
        "xmit__hash_policy": "transmit-hash-policy",
        "transmit_hash_policy": "transmit-hash-policy",
        "transmit-hash-policy": "transmit-hash-policy",
        "lacp_rate": "lacp-rate",
        "lacp-rate": "lacp-rate",
    }
    return mapping.get(normalized, normalized.replace("_", "-"))


def parse_25g_mode(raw_value):
    text = str(raw_value or "").strip()
    if not text:
        return {}
    parsed = parse_inline_mapping(text)
    if parsed:
        if "parameters" in parsed and isinstance(parsed["parameters"], dict):
            return parsed["parameters"]
        return parsed
    normalized_text = re.sub(r"xmit\s+_hash_policy", "xmit_hash_policy", text, flags=re.IGNORECASE)
    token_values = {}
    for token in normalized_text.replace(",", " ").replace(";", " ").split():
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        key = normalize_bond_parameter_key(key)
        value = value.strip()
        if not key or not value:
            continue
        token_values[key] = int(value) if value.isdigit() else value
    if token_values:
        return token_values
    return {"mode": text}


def build_bond25g_config(policy, csv_row):
    raw_25g = csv_row.get("25g")
    if not str(raw_25g or "").strip():
        return {}

    network_defaults = ((policy.get("networking") or {}).get("bond25g") or {})
    csv_25g = parse_25g_value(raw_25g)
    mode_override = parse_25g_mode(csv_row.get("25g_mode"))
    merged = deep_merge_copy(network_defaults, csv_25g)
    merged["parameters"] = deep_merge_copy(network_defaults.get("parameters"), mode_override)

    interfaces = normalize_list(merged.get("interfaces"))
    if not interfaces:
        return {}

    bond_name = merged.get("bond_name", "bond0")
    address = merged.get("address")
    gateway4 = merged.get("gateway4")
    nameservers = normalize_list(merged.get("nameservers"))

    ethernets = {iface: {"dhcp4": False} for iface in interfaces}
    bond = {
        "interfaces": interfaces,
        "parameters": merged.get("parameters") or {},
        "dhcp4": False,
    }
    if address:
        bond["addresses"] = [address]
    if gateway4:
        bond["gateway4"] = gateway4
    if nameservers:
        bond["nameservers"] = {"addresses": nameservers}

    return {
        "version": 2,
        "ethernets": ethernets,
        "bonds": {bond_name: bond},
    }


def fetch_redfish_serial(csv_row):
    bmc_ip = (csv_row.get("bmc_ip") or "").strip()
    bmc_user = (csv_row.get("bmc_user") or "").strip()
    bmc_pass = (csv_row.get("bmc_pass") or "").strip()
    node_id = (csv_row.get("node_id") or "1").strip() or "1"
    if not (bmc_ip and bmc_user and bmc_pass):
        return ""

    result = run_cmd_optional(
        [
            "curl",
            "-sk",
            "-u",
            f"{bmc_user}:{bmc_pass}",
            f"https://{bmc_ip}/redfish/v1/Systems/{node_id}",
        ]
    )
    if result.returncode != 0:
        return ""

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return ""
    return str(payload.get("SerialNumber") or "").strip()


def verify_serial(csv_row):
    expected = str(csv_row.get("sn") or "").strip()
    if not expected:
        return True, "", ""
    actual = fetch_redfish_serial(csv_row)
    if not actual:
        return False, expected, ""
    return actual == expected, expected, actual


def render_cloud_init(machine, policy, hostname_override="", csv_row=None):
    sudo_user = policy.get("sudo_user") or {}
    root = policy.get("root") or {}
    ssh = policy.get("ssh") or {}
    csv_row = csv_row or {}
    hostname = hostname_override or machine.get("hostname") or machine.get("fqdn") or machine.get("system_id")

    sudo_user_name = sudo_user.get("name", "ubuntu")
    sudo_groups = normalize_list(sudo_user.get("groups") or ["adm", "sudo"])
    sudo_password = sudo_user.get("password")
    sudo_keys = normalize_list(sudo_user.get("ssh_authorized_keys"))
    root_enabled = normalize_bool(root.get("enabled"), default=True)
    root_password = root.get("password")
    allow_users = normalize_list(ssh.get("allow_users"))
    if not allow_users:
        allow_users = [sudo_user_name]
        if root_enabled:
            allow_users.append("root")

    users = [
        {
            "name": sudo_user_name,
            "gecos": sudo_user.get("gecos", sudo_user_name),
            "groups": sudo_groups,
            "shell": sudo_user.get("shell", "/bin/bash"),
            "sudo": sudo_rule(sudo_user.get("sudo_nopasswd")),
            "lock_passwd": False if sudo_password else True,
        }
    ]
    if sudo_keys:
        users[0]["ssh_authorized_keys"] = sudo_keys

    chpasswd_users = []
    if sudo_password:
        chpasswd_users.append(
            {"name": sudo_user_name, "password": sudo_password, "type": "text"}
        )
    if root_enabled and root_password:
        chpasswd_users.append({"name": "root", "password": root_password, "type": "text"})

    ssh_lines = [
        f"PermitRootLogin {ssh_permit_root(ssh.get('permit_root_login'))}",
        f"PasswordAuthentication {'yes' if normalize_bool(ssh.get('password_authentication'), True) else 'no'}",
        f"PubkeyAuthentication {'yes' if normalize_bool(ssh.get('pubkey_authentication'), True) else 'no'}",
        "KbdInteractiveAuthentication no",
        "ChallengeResponseAuthentication no",
        "UsePAM yes",
    ]
    if allow_users:
        ssh_lines.append("AllowUsers " + " ".join(allow_users))

    hosts_text = "\n".join(
        [
            f"127.0.0.1 localhost {hostname}",
            "::1 localhost ip6-localhost ip6-loopback",
            "ff02::1 ip6-allnodes",
            "ff02::2 ip6-allrouters",
        ]
    ) + "\n"

    write_files = [
        {
            "path": "/etc/ssh/sshd_config.d/99-maas-defaults.conf",
            "permissions": "0644",
            "content": "\n".join(ssh_lines) + "\n",
        },
        {
            "path": "/etc/hosts",
            "permissions": "0644",
            "content": hosts_text,
        },
    ]

    runcmd = ["systemctl restart ssh || systemctl restart sshd || true"]

    bond25g = build_bond25g_config(policy, csv_row)
    if bond25g:
        write_files.append(
            {
                "path": "/etc/netplan/99-bond25g.yaml",
                "permissions": "0644",
                "content": yaml.safe_dump({"network": bond25g}, sort_keys=False),
            }
        )
        runcmd.append("netplan generate && netplan apply || true")

    cloud_init = {
        "hostname": hostname,
        "fqdn": hostname,
        "preserve_hostname": False,
        "manage_etc_hosts": False,
        "disable_root": not root_enabled,
        "ssh_pwauth": normalize_bool(ssh.get("password_authentication"), True),
        "users": users,
        "write_files": write_files,
        "runcmd": runcmd,
        "final_message": (
            f"cloud-init finished for {hostname} "
            f"with policy={policy['policy_name']}"
        ),
    }
    if chpasswd_users:
        cloud_init["chpasswd"] = {"expire": False, "users": chpasswd_users}
    return "#cloud-config\n" + yaml.safe_dump(cloud_init, sort_keys=False)


def list_targets(profile, tag=None, all_ready=False, explicit_ids=None, include_statuses=None):
    include_statuses = {item.lower() for item in normalize_list(include_statuses)}
    if explicit_ids:
        return explicit_ids
    if tag:
        machines = maas_json(profile, "tag", "machines", tag)
        if include_statuses:
            return [
                item["system_id"]
                for item in machines
                if (item.get("status_name") or "").lower() in include_statuses
            ]
        return [item["system_id"] for item in machines]
    if all_ready:
        machines = maas_json(profile, "machines", "read")
        return [
            item["system_id"]
            for item in machines
            if (item.get("status_name") or "").lower()
            in (include_statuses or {"ready"})
        ]
    raise SystemExit("no target machines: provide system_id, --tag, or --all-ready")


def deploy_machine(profile, series, machine, user_data_text, dry_run=False):
    if dry_run:
        return
    encoded = base64.b64encode(user_data_text.encode("utf-8")).decode("ascii")
    run_cmd(
        [
            "maas",
            profile,
            "machine",
            "deploy",
            machine["system_id"],
            f"distro_series={series}",
            f"user_data={encoded}",
        ]
    )


def release_failed_deployment(profile, sysid):
    run_cmd(
        [
            "maas",
            profile,
            "machine",
            "release",
            sysid,
            'comment=auto release failed deployment before retry deploy',
            "erase=false",
        ]
    )


def wait_for_ready(profile, sysid, timeout=180):
    deadline = time.time() + timeout
    last_status = ""
    while time.time() < deadline:
        machine = maas_json(profile, "machine", "read", sysid)
        last_status = (machine.get("status_name") or "").lower()
        if last_status == "ready":
            return machine
        time.sleep(3)
    raise SystemExit(f"{sysid} did not return to Ready within {timeout}s, last_status={last_status}")


def maybe_update_maas_hostname(profile, machine, target_hostname, dry_run=False):
    current_hostname = (machine.get("hostname") or "").strip()
    target_hostname = (target_hostname or "").strip()
    if not target_hostname or target_hostname == current_hostname:
        return
    if dry_run:
        return
    run_cmd(
        [
            "maas",
            profile,
            "machine",
            "update",
            machine["system_id"],
            f"hostname={target_hostname}",
        ]
    )


def parse_args():
    script_dir = Path(__file__).resolve().parent
    default_config = script_dir.parent / "cloud-init" / "deploy-policy.yaml"

    parser = argparse.ArgumentParser(
        description="Deploy MAAS machines with tag-aware policy YAML and cloud-init rendering."
    )
    parser.add_argument("system_ids", nargs="*", help="Target MAAS system_id list")
    parser.add_argument("--config", default=str(default_config), help="Policy YAML path")
    parser.add_argument("--profile", help="MAAS CLI profile, default from YAML or admin")
    parser.add_argument("--series", help="Ubuntu distro series, default from YAML or jammy")
    parser.add_argument("--policy", help="Force policy name and bypass tag auto-match")
    parser.add_argument("--csv", help="CSV file with hostname,pxe_mac,... used to override target hostname")
    parser.add_argument("--tag", help="Deploy all machines under a MAAS tag")
    parser.add_argument(
        "--include-failed-deployment",
        action="store_true",
        help="Include nodes in Failed deployment when selecting by --tag or --all-ready",
    )
    parser.add_argument(
        "--all-ready",
        action="store_true",
        help="Deploy all MAAS machines whose status is Ready",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print resolved policy and rendered cloud-init without deploy",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    with open(args.config, "r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    csv_rows = load_csv_rows(args.csv)

    profile = resolve_profile(config, args.profile)
    series = resolve_series(config, args.series)
    include_statuses = {"ready"}
    if args.include_failed_deployment:
        include_statuses.add("failed deployment")
    targets = list_targets(
        profile,
        tag=args.tag,
        all_ready=args.all_ready,
        explicit_ids=args.system_ids,
        include_statuses=include_statuses if (args.tag or args.all_ready) else None,
    )

    if not targets:
        raise SystemExit("no machines matched the requested target selector")

    failures = []

    for sysid in targets:
        machine = maas_json(profile, "machine", "read", sysid)
        if (machine.get("status_name") or "").lower() == "failed deployment" and args.include_failed_deployment:
            print(
                f"[deploy] system_id={sysid} hostname={machine.get('hostname')} auto release from Failed deployment",
                file=sys.stderr,
            )
            if not args.dry_run:
                release_failed_deployment(profile, sysid)
                machine = wait_for_ready(profile, sysid)
        csv_row = resolve_csv_row(machine, csv_rows)
        csv_tags = ensure_machine_tags(profile, sysid, csv_row, existing_machine=machine, dry_run=args.dry_run)
        if csv_tags and not args.dry_run:
            machine = maas_json(profile, "machine", "read", sysid)
        machine_with_csv_tags = merge_machine_csv_tags(machine, csv_row)
        policy_name, reason = resolve_policy(config, machine_with_csv_tags, args.policy)
        effective_policy = build_effective_policy(config, policy_name)
        hostname_override = first_non_empty(
            (csv_row or {}).get("hostname"),
            resolve_csv_hostname(machine, csv_rows),
        )
        target_hostname = first_non_empty(
            hostname_override,
            machine.get("hostname"),
            machine.get("fqdn"),
            machine.get("system_id"),
        )
        serial_ok, expected_sn, actual_sn = verify_serial(csv_row)
        if not serial_ok:
            message = (
                f"[deploy] system_id={sysid} hostname={target_hostname} serial mismatch "
                f"expected={expected_sn or '-'} actual={actual_sn or 'unavailable'}"
            )
            print(message, file=sys.stderr)
            failures.append(message)
            continue
        user_data_text = render_cloud_init(
            machine,
            effective_policy,
            hostname_override=hostname_override,
            csv_row=csv_row,
        )

        print(
            f"[deploy] system_id={sysid} hostname={target_hostname} "
            f"policy={policy_name} reason={reason} tags={','.join(machine_tags(machine_with_csv_tags)) or '-'}",
            file=sys.stderr,
        )
        if args.dry_run:
            print(user_data_text)
            continue
        maybe_update_maas_hostname(profile, machine, target_hostname, dry_run=False)
        deploy_machine(profile, series, machine, user_data_text, dry_run=False)

    if failures:
        raise SystemExit("\n".join(failures))


if __name__ == "__main__":
    main()
