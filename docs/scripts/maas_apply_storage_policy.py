#!/usr/bin/env python3

import argparse
import json
import subprocess
from pathlib import Path

from maas_policy_deploy import (
    build_effective_policy,
    list_targets,
    maas_json,
    machine_tags,
    resolve_policy,
    resolve_profile,
)


def run_cmd(cmd):
    return subprocess.run(cmd, check=True, text=True, capture_output=True)


def parse_size_to_bytes(size_text):
    value = str(size_text).strip().upper()
    multiplier = 1
    if value.endswith("K"):
        multiplier = 1024
        value = value[:-1]
    elif value.endswith("M"):
        multiplier = 1024**2
        value = value[:-1]
    elif value.endswith("G"):
        multiplier = 1024**3
        value = value[:-1]
    elif value.endswith("T"):
        multiplier = 1024**4
        value = value[:-1]
    return int(float(value) * multiplier)


def set_layout(profile, sysid, boot_size_bytes, root_size_bytes):
    cmd = [
        "maas",
        profile,
        "machine",
        "set-storage-layout",
        sysid,
        "layout=flat",
        f"boot_size={boot_size_bytes}",
        f"root_size={root_size_bytes}",
    ]
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.returncode == 0:
        return
    run_cmd(
        [
            "maas",
            profile,
            "machine",
            "set-storage-layout",
            sysid,
            "storage_layout=flat",
            f"boot_size={boot_size_bytes}",
            f"root_size={root_size_bytes}",
        ]
    )


def pick_boot_device_id(profile, sysid):
    devices = maas_json(profile, "block-devices", "read", sysid)
    physical = [dev for dev in devices if dev.get("type") == "physical"]
    if not physical:
        raise SystemExit(f"no physical block device found for {sysid}")

    def rank(dev):
        name = (dev.get("name") or "").lower()
        model = (dev.get("model") or "").lower()
        size = dev.get("size") or 0
        is_nvme = name.startswith("nvme")
        is_sda = name == "sda"
        looks_ssd = ("ssd" in model) or ("mr9560" in model) or (name.startswith("sd") and not is_nvme)
        return (
            0 if is_sda else 1,
            0 if looks_ssd and not is_nvme else 1,
            0 if not is_nvme else 1,
            size,
            name,
        )

    physical.sort(key=rank)
    return physical[0]["id"]


def ensure_data_partition(profile, sysid, dev_id, mount_point):
    if not mount_point:
        return
    partition = json.loads(
        run_cmd(["maas", profile, "partitions", "create", sysid, str(dev_id)]).stdout
    )
    part_id = partition["id"]
    run_cmd(["maas", profile, "partition", "format", sysid, str(dev_id), str(part_id), "fstype=ext4"])
    run_cmd(
        [
            "maas",
            profile,
            "partition",
            "mount",
            sysid,
            str(dev_id),
            str(part_id),
            f"mount_point={mount_point}",
        ]
    )


def parse_args():
    script_dir = Path(__file__).resolve().parent
    default_config = script_dir.parent / "cloud-init" / "deploy-policy.yaml"

    parser = argparse.ArgumentParser(
        description="Apply MAAS storage layout from the same tag-aware deploy policy YAML."
    )
    parser.add_argument("system_ids", nargs="*", help="Target MAAS system_id list")
    parser.add_argument("--config", default=str(default_config), help="Policy YAML path")
    parser.add_argument("--profile", help="MAAS CLI profile, default from YAML or admin")
    parser.add_argument("--policy", help="Force policy name and bypass tag auto-match")
    parser.add_argument("--tag", help="Apply to all machines under a MAAS tag")
    parser.add_argument("--all-ready", action="store_true", help="Apply to all Ready nodes")
    parser.add_argument("--dry-run", action="store_true", help="Only print resolved storage policy")
    return parser.parse_args()


def main():
    import yaml

    args = parse_args()
    with open(args.config, "r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}

    profile = resolve_profile(config, args.profile)
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
        storage = effective_policy.get("storage") or {}
        boot_size = storage.get("boot_size", "2G")
        root_size = storage.get("root_size", "200G")
        data_mount = storage.get("data_mount", "/data")
        dev_id = pick_boot_device_id(profile, sysid)

        print(
            f"[storage] system_id={sysid} hostname={machine.get('hostname')} "
            f"policy={policy_name} reason={reason} boot_size={boot_size} "
            f"root_size={root_size} data_mount={data_mount} "
            f"tags={','.join(machine_tags(machine)) or '-'}"
        )
        if args.dry_run:
            continue

        run_cmd(["maas", profile, "block-device", "set-boot-disk", sysid, str(dev_id)])
        set_layout(profile, sysid, parse_size_to_bytes(boot_size), parse_size_to_bytes(root_size))
        ensure_data_partition(profile, sysid, dev_id, data_mount)


if __name__ == "__main__":
    main()
