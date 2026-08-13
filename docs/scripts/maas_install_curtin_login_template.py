#!/usr/bin/env python3

import argparse
import json
import re
import shlex
from pathlib import Path

try:
    import yaml
except ImportError as exc:
    raise SystemExit(
        "missing dependency: PyYAML\n"
        "install with: sudo apt-get install -y python3-yaml"
    ) from exc

from maas_policy_deploy import (
    build_effective_policy,
    deep_merge,
    load_default_user_data,
    login_policy_from_user_data,
    load_csv_rows,
    normalize_bool,
    normalize_list,
    parse_csv_tags,
    resolve_policy,
)


LATE_COMMAND_ANCHOR = (
    "  maas: [wget, '--no-proxy', {{node_disable_pxe_url|escape.json}}, "
    "'--post-data', {{node_disable_pxe_data|escape.json}}, '-O', '/dev/null']"
)

BEGIN_MARKER = "  # BEGIN MAAS LOGIN INJECTION"
END_MARKER = "  # END MAAS LOGIN INJECTION"


def parse_args():
    script_dir = Path(__file__).resolve().parent
    default_config = script_dir.parent / "cloud-init" / "deploy-policy.yaml"
    default_user_data = script_dir.parent / "cloud-init" / "default-user-data.yaml"
    parser = argparse.ArgumentParser(
        description=(
            "Render and install a reusable MAAS curtin template that enforces "
            "login user/password and sshd policy during install-time late_commands."
        )
    )
    parser.add_argument("--config", default=str(default_config), help="Policy YAML path")
    parser.add_argument("--user-data", default=str(default_user_data), help="Default account/cloud-init YAML path")
    parser.add_argument(
        "--policy",
        help=(
            "Policy name from deploy-policy.yaml. In --csv mode, omit this to auto-resolve "
            "policy from each row's tag/tags field."
        ),
    )
    parser.add_argument("--series", default="jammy", help="Ubuntu series, default: jammy")
    parser.add_argument("--arch", default="amd64", help="Architecture, default: amd64")
    parser.add_argument("--subarch", default="generic", help="Sub-architecture, default: generic")
    parser.add_argument(
        "--osystem", default="ubuntu", help="Operating system segment in template name, default: ubuntu"
    )
    parser.add_argument(
        "--hostname",
        help="Optional hostname for a node-specific template; omit to render a generic series template",
    )
    parser.add_argument(
        "--csv",
        help=(
            "Optional CSV file with hostname/tag(s). When set, the script renders one "
            "node-specific template per CSV row and auto-resolves policy from row tags."
        ),
    )
    parser.add_argument(
        "--source-template",
        default="/etc/maas/preseeds/curtin_userdata",
        help="MAAS source preseed template to copy from",
    )
    parser.add_argument(
        "--output",
        help="Output path; default writes under /etc/maas/preseeds using the derived template name",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print rendered content instead of writing the target file",
    )
    return parser.parse_args()


def shell_single_quote(text):
    return text.replace("'", "'\"'\"'")


def yaml_list(items):
    return " ".join(shlex.quote(str(item)) for item in items if str(item).strip())


def derive_template_name(args):
    name = f"curtin_userdata_{args.osystem}_{args.arch}_{args.subarch}_{args.series}"
    if args.hostname:
        name += f"_{args.hostname}"
    return name


def load_config(config_path):
    config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    policies = config.get("policies") or {}
    if not policies:
        raise SystemExit(f"invalid config, policies is empty: {config_path}")
    return config


def resolve_login_policy(config, policy_name, login_defaults=None):
    policies = config.get("policies") or {}
    if policy_name not in policies:
        raise SystemExit(f"policy not found: {policy_name}")
    return deep_merge(login_defaults or {}, build_effective_policy(config, policy_name))


def predictable_ifname_kernel_cmdline(policy):
    networking = policy.get("networking") or {}
    enabled = networking.get("predictable_interface_names")
    if not normalize_bool(enabled, default=False):
        return ""
    return str(
        networking.get("predictable_interface_kernel_cmdline")
        or "net.ifnames=1 biosdevname=0"
    ).strip()


def build_login_injection_block(policy):
    sudo_user = policy.get("sudo_user") or {}
    root = policy.get("root") or {}
    ssh = policy.get("ssh") or {}

    sudo_user_name = str(sudo_user.get("name") or "ubuntu").strip()
    sudo_password = str(sudo_user.get("password") or "").strip()
    sudo_groups = [str(item).strip() for item in normalize_list(sudo_user.get("groups") or ["adm", "sudo"]) if str(item).strip()]
    sudo_shell = str(sudo_user.get("shell") or "/bin/bash").strip()
    sudo_nopasswd = normalize_bool(sudo_user.get("sudo_nopasswd"), default=True)

    root_enabled = normalize_bool(root.get("enabled"), default=True)
    root_password = str(root.get("password") or "").strip()

    password_auth = normalize_bool(ssh.get("password_authentication"), default=True)
    pubkey_auth = normalize_bool(ssh.get("pubkey_authentication"), default=True)

    allow_users = [str(item).strip() for item in normalize_list(ssh.get("allow_users")) if str(item).strip()]
    if not allow_users:
        allow_users = [sudo_user_name]
        if root_enabled:
            allow_users.append("root")

    password_lines = []
    if sudo_password:
        password_lines.append(f"{sudo_user_name}:{sudo_password}")
    if root_enabled and root_password:
        password_lines.append(f"root:{root_password}")
    if not password_lines:
        raise SystemExit("login injection requires at least one plaintext password in the selected policy")

    useradd_cmd = (
        f"grep -q '^{shell_single_quote(sudo_user_name)}:' $t/etc/passwd || "
        f"useradd -R $t -m -s {shlex.quote(sudo_shell)} -G {shlex.quote(','.join(sudo_groups))} "
        f"{shlex.quote(sudo_user_name)}"
    )
    password_cmd = (
        "printf '%s\\n' "
        + " ".join(shlex.quote(line) for line in password_lines)
        + " | chpasswd -R $t"
    )

    unlock_cmds = [f"passwd -R $t -u {shlex.quote(sudo_user_name)} || true"]
    if root_enabled and root_password:
        unlock_cmds.append("passwd -R $t -u root || true")

    sudo_rule = "NOPASSWD:ALL" if sudo_nopasswd else "ALL"
    sudoers_cmd = (
        "install -d -m 0755 $t/etc/sudoers.d; "
        f"printf '%s\\n' {shlex.quote(f'{sudo_user_name} ALL=(ALL) {sudo_rule}')} "
        f"> $t/etc/sudoers.d/90-maas-{sudo_user_name}; "
        f"chmod 0440 $t/etc/sudoers.d/90-maas-{sudo_user_name}"
    )

    predictable_ifnames_cmdline = predictable_ifname_kernel_cmdline(policy)
    predictable_ifnames_cfg = (
        "GRUB_CMDLINE_LINUX_DEFAULT="
        f"\"${{GRUB_CMDLINE_LINUX_DEFAULT:+$GRUB_CMDLINE_LINUX_DEFAULT }}"
        f"{predictable_ifnames_cmdline}\""
    )
    predictable_ifnames_cmd = (
        "install -d -m 0755 $t/etc/default/grub.d; "
        f"printf '%s\\n' {shlex.quote(predictable_ifnames_cfg)} "
        "> $t/etc/default/grub.d/90-maas-predictable-ifnames.cfg; "
        "chroot $t sh -c 'update-grub || true' || true"
        if predictable_ifnames_cmdline
        else ""
    )

    permit_root = "yes" if root_enabled else "no"
    ssh_lines = [
        f"PasswordAuthentication {'yes' if password_auth else 'no'}",
        f"PubkeyAuthentication {'yes' if pubkey_auth else 'no'}",
        "KbdInteractiveAuthentication no",
        "ChallengeResponseAuthentication no",
        "UsePAM yes",
        f"PermitRootLogin {permit_root}",
        "AllowUsers " + " ".join(allow_users),
    ]
    sshd_cmd = (
        "install -d -m 0755 $t/etc/ssh/sshd_config.d; "
        "rm -f $t/etc/ssh/sshd_config.d/50-cloud-init.conf; "
        f"printf '%s\\n' {' '.join(shlex.quote(line) for line in ssh_lines)} "
        "> $t/etc/ssh/sshd_config.d/00-maas-password-auth.conf"
    )

    enable_ssh_cmd = (
        "mkdir -p $t/etc/systemd/system/multi-user.target.wants; "
        "[ -e $t/etc/systemd/system/multi-user.target.wants/ssh.service ] || "
        "ln -sf /lib/systemd/system/ssh.service $t/etc/systemd/system/multi-user.target.wants/ssh.service || true"
    )

    common_prefix = "set -eux; t=$(echo /tmp/tmp*/target); [ -d $t/etc ] || { echo target-not-found; exit 3; }; "

    commands = [
        BEGIN_MARKER,
        "  maas_login_00_create_users: ["
        + json.dumps(
            "sh"
        )
        + ", "
        + json.dumps(
            "-c"
        )
        + ", "
        + json.dumps(common_prefix + useradd_cmd + "; " + password_cmd + "; " + "; ".join(unlock_cmds))
        + "]",
        "  maas_login_01_sudoers: ["
        + json.dumps("sh")
        + ", "
        + json.dumps("-c")
        + ", "
        + json.dumps(common_prefix + sudoers_cmd)
        + "]",
    ]
    if predictable_ifnames_cmd:
        commands.extend(
            [
                "  maas_login_02_predictable_ifnames: ["
                + json.dumps("sh")
                + ", "
                + json.dumps("-c")
                + ", "
                + json.dumps(common_prefix + predictable_ifnames_cmd)
                + "]"
            ]
        )
    commands.extend(
        [
            "  maas_login_03_sshd_dropin: ["
            + json.dumps("sh")
            + ", "
            + json.dumps("-c")
            + ", "
            + json.dumps(common_prefix + sshd_cmd)
            + "]",
            "  maas_login_04_enable_ssh: ["
            + json.dumps("sh")
            + ", "
            + json.dumps("-c")
            + ", "
            + json.dumps(common_prefix + enable_ssh_cmd)
            + "]",
            END_MARKER,
        ]
    )
    return "\n".join(commands)


def inject_block(template_text, block):
    block_pattern = re.compile(
        rf"{re.escape(BEGIN_MARKER)}.*?{re.escape(END_MARKER)}\n?",
        re.DOTALL,
    )
    if block_pattern.search(template_text):
        return block_pattern.sub(block + "\n", template_text, count=1)

    if LATE_COMMAND_ANCHOR not in template_text:
        raise SystemExit(
            "failed to locate late_commands anchor in source template: "
            f"{LATE_COMMAND_ANCHOR}"
        )
    return template_text.replace(LATE_COMMAND_ANCHOR, LATE_COMMAND_ANCHOR + "\n\n" + block, 1)


def render_to_target(template_text, policy, output_path=None, stdout=False):
    rendered = inject_block(template_text, build_login_injection_block(policy))
    if stdout:
        print(rendered, end="")
        return
    if not output_path:
        raise SystemExit("output_path is required when --stdout is not set")
    output_path.write_text(rendered, encoding="utf-8")
    print(f"wrote {output_path}")


def render_batch(args, config, template_text, login_defaults):
    if args.stdout:
        raise SystemExit("--stdout cannot be combined with --csv batch mode")

    rows = load_csv_rows(args.csv)
    if not rows:
        raise SystemExit(f"no CSV rows found: {args.csv}")

    output_dir = Path(args.output) if args.output else Path("/etc/maas/preseeds")
    if args.output and output_dir.suffix:
        raise SystemExit("--output must be a directory when --csv batch mode is used")
    output_dir.mkdir(parents=True, exist_ok=True)

    for row in rows:
        hostname = str(row.get("hostname") or "").strip()
        if not hostname:
            continue
        fake_machine = {"hostname": hostname, "tag_names": parse_csv_tags(row)}
        policy_name = args.policy
        if not policy_name:
            policy_name, _ = resolve_policy(config, fake_machine, forced_policy=None)
        policy = resolve_login_policy(config, policy_name, login_defaults)
        node_args = argparse.Namespace(**vars(args))
        node_args.hostname = hostname
        target_path = output_dir / derive_template_name(node_args)
        render_to_target(template_text, policy, output_path=target_path, stdout=False)


def main():
    args = parse_args()
    config = load_config(args.config)
    login_defaults = login_policy_from_user_data(load_default_user_data(args.user_data))
    source_template = Path(args.source_template)
    if not source_template.exists():
        raise SystemExit(f"source template not found: {source_template}")
    template_text = source_template.read_text(encoding="utf-8")

    if args.csv:
        render_batch(args, config, template_text, login_defaults)
        return

    policy_name = args.policy or "default"
    policy = resolve_login_policy(config, policy_name, login_defaults)

    if args.stdout:
        render_to_target(template_text, policy, stdout=True)
        return

    output_path = Path(args.output) if args.output else Path("/etc/maas/preseeds") / derive_template_name(args)
    render_to_target(template_text, policy, output_path=output_path, stdout=False)


if __name__ == "__main__":
    main()
