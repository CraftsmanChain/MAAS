#!/usr/bin/env python3

import argparse
import json
import time
import subprocess
from pathlib import Path

from maas_policy_deploy import (
    build_effective_policy,
    ensure_machine_tags,
    list_targets,
    load_csv_rows,
    maas_json,
    merge_machine_csv_tags,
    machine_tags,
    resolve_csv_row,
    resolve_policy,
    resolve_profile,
)


def run_cmd(cmd):
    result = subprocess.run(cmd, check=False, text=True, capture_output=True)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        detail = stderr or stdout or "no output"
        raise SystemExit(
            f"command failed (exit={result.returncode}): {' '.join(cmd)}\n{detail}"
        )
    return result


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


def clear_layout(profile, sysid):
    cmd = ["maas", profile, "machine", "set-storage-layout", sysid, "storage_layout=blank"]
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.returncode == 0:
        return
    run_cmd(["maas", profile, "machine", "set-storage-layout", sysid, "layout=blank"])


def create_partition(profile, sysid, dev_id, size_bytes=None, bootable=False):
    cmd = ["maas", profile, "partitions", "create", sysid, str(dev_id)]
    if size_bytes is not None:
        cmd.append(f"size={size_bytes}")
    if bootable:
        cmd.append("bootable=true")
    return json.loads(run_cmd(cmd).stdout)


def format_and_mount_partition(profile, sysid, dev_id, part_id, fstype, mount_point, label=None):
    cmd = ["maas", profile, "partition", "format", sysid, str(dev_id), str(part_id), f"fstype={fstype}"]
    if label:
        cmd.append(f"label={label}")
    run_cmd(cmd)
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


def set_layout(profile, sysid, dev_id, efi_size_bytes, boot_size_bytes, root_size_bytes, data_mount):
    clear_layout(profile, sysid)
    run_cmd(["maas", profile, "block-device", "set-boot-disk", sysid, str(dev_id)])

    efi = create_partition(profile, sysid, dev_id, efi_size_bytes, bootable=True)
    format_and_mount_partition(profile, sysid, dev_id, efi["id"], "fat32", "/boot/efi", label="efi")

    # Only the EFI partition should be marked bootable on UEFI systems.
    boot = create_partition(profile, sysid, dev_id, boot_size_bytes)
    format_and_mount_partition(profile, sysid, dev_id, boot["id"], "ext4", "/boot", label="boot")

    root = create_partition(profile, sysid, dev_id, root_size_bytes)
    format_and_mount_partition(profile, sysid, dev_id, root["id"], "ext4", "/", label="root")

    ensure_data_partition(profile, sysid, dev_id, data_mount)


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


def release_failed_deployment(profile, sysid):
    run_cmd(
        [
            "maas",
            profile,
            "machine",
            "release",
            sysid,
            'comment=auto release failed deployment before reapply storage',
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
    parser.add_argument("--csv", help="CSV file with tag metadata for policy matching")
    parser.add_argument("--tag", help="Apply to all machines under a MAAS tag")
    parser.add_argument("--all-ready", action="store_true", help="Apply to all Ready nodes")
    parser.add_argument(
        "--include-failed-deployment",
        action="store_true",
        help="Include nodes in Failed deployment and auto-release them before applying storage",
    )
    parser.add_argument("--dry-run", action="store_true", help="Only print resolved storage policy")
    return parser.parse_args()


def main():
    import yaml

    args = parse_args()
    with open(args.config, "r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    csv_rows = load_csv_rows(args.csv)

    profile = resolve_profile(config, args.profile)
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

    for sysid in targets:
        machine = maas_json(profile, "machine", "read", sysid)
        status_name = (machine.get("status_name") or "").lower()
        if status_name == "failed deployment" and args.include_failed_deployment:
            print(f"[storage] system_id={sysid} hostname={machine.get('hostname')} auto release from Failed deployment")
            if not args.dry_run:
                release_failed_deployment(profile, sysid)
                machine = wait_for_ready(profile, sysid)

        csv_row = {}
        if csv_rows:
            csv_row = resolve_csv_row(machine, csv_rows)
            csv_tags = ensure_machine_tags(profile, sysid, csv_row, existing_machine=machine, dry_run=args.dry_run)
            if csv_tags and not args.dry_run:
                machine = maas_json(profile, "machine", "read", sysid)
        machine_with_csv_tags = merge_machine_csv_tags(machine, csv_row)
        policy_name, reason = resolve_policy(config, machine_with_csv_tags, args.policy)
        effective_policy = build_effective_policy(config, policy_name)
        storage = effective_policy.get("storage") or {}
        efi_size = storage.get("efi_size", "2G")
        boot_size = storage.get("boot_size", "2G")
        root_size = storage.get("root_size", "200G")
        data_mount = storage.get("data_mount", "/data")
        dev_id = pick_boot_device_id(profile, sysid)

        print(
            f"[storage] system_id={sysid} hostname={machine.get('hostname')} "
            f"policy={policy_name} reason={reason} efi_size={efi_size} boot_size={boot_size} "
            f"root_size={root_size} data_mount={data_mount} "
            f"tags={','.join(machine_tags(machine_with_csv_tags)) or '-'}"
        )
        if args.dry_run:
            continue

        set_layout(
            profile,
            sysid,
            dev_id,
            parse_size_to_bytes(efi_size),
            parse_size_to_bytes(boot_size),
            parse_size_to_bytes(root_size),
            data_mount,
        )


if __name__ == "__main__":
    main()
