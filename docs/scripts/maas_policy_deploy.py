#!/usr/bin/env python3

import argparse
import base64
import json
import subprocess
import sys
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


def resolve_profile(config, cli_profile):
    return cli_profile or config.get("defaults", {}).get("profile") or "admin"


def resolve_series(config, cli_series):
    return cli_series or config.get("defaults", {}).get("distro_series") or "jammy"


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


def sudo_rule(nopasswd):
    return "ALL=(ALL) NOPASSWD:ALL" if normalize_bool(nopasswd) else "ALL=(ALL) ALL"


def ssh_permit_root(value):
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value is None:
        return "yes"
    text = str(value).strip()
    return text or "yes"


def render_cloud_init(machine, policy):
    sudo_user = policy.get("sudo_user") or {}
    root = policy.get("root") or {}
    ssh = policy.get("ssh") or {}

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

    cloud_init = {
        "hostname": machine.get("hostname") or machine.get("fqdn") or machine.get("system_id"),
        "disable_root": not root_enabled,
        "ssh_pwauth": normalize_bool(ssh.get("password_authentication"), True),
        "users": users,
        "write_files": [
            {
                "path": "/etc/ssh/sshd_config.d/99-maas-defaults.conf",
                "permissions": "0644",
                "content": "\n".join(ssh_lines) + "\n",
            }
        ],
        "runcmd": ["systemctl restart ssh || systemctl restart sshd || true"],
        "final_message": (
            f"cloud-init finished for {machine.get('hostname') or machine.get('system_id')} "
            f"with policy={policy['policy_name']}"
        ),
    }
    if chpasswd_users:
        cloud_init["chpasswd"] = {"expire": False, "users": chpasswd_users}
    return "#cloud-config\n" + yaml.safe_dump(cloud_init, sort_keys=False)


def list_targets(profile, tag=None, all_ready=False, explicit_ids=None):
    if explicit_ids:
        return explicit_ids
    if tag:
        machines = maas_json(profile, "tag", "machines", tag)
        return [item["system_id"] for item in machines]
    if all_ready:
        machines = maas_json(profile, "machines", "read")
        return [
            item["system_id"]
            for item in machines
            if (item.get("status_name") or "").lower() == "ready"
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
    parser.add_argument("--tag", help="Deploy all machines under a MAAS tag")
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

    profile = resolve_profile(config, args.profile)
    series = resolve_series(config, args.series)
    targets = list_targets(
        profile,
        tag=args.tag,
        all_ready=args.all_ready,
        explicit_ids=args.system_ids,
    )

    if not targets:
        raise SystemExit("no machines matched the requested target selector")

    for sysid in targets:
        machine = maas_json(profile, "machine", "read", sysid)
        policy_name, reason = resolve_policy(config, machine, args.policy)
        effective_policy = build_effective_policy(config, policy_name)
        user_data_text = render_cloud_init(machine, effective_policy)

        print(
            f"[deploy] system_id={sysid} hostname={machine.get('hostname')} "
            f"policy={policy_name} reason={reason} tags={','.join(machine_tags(machine)) or '-'}",
            file=sys.stderr,
        )
        if args.dry_run:
            print(user_data_text)
            continue
        deploy_machine(profile, series, machine, user_data_text, dry_run=False)


if __name__ == "__main__":
    main()
