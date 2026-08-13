#!/usr/bin/env python3
import base64
import ipaddress
import json
import os
import re
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlparse

COLLECTOR_URL = "__COLLECTOR_URL__"


def read_first(paths):
    for path in paths:
        try:
            value = Path(path).read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            continue
        if value and value.lower() not in {"none", "unknown", "system serial number"}:
            return value
    return ""


def run_text(command, timeout=5):
    try:
        return subprocess.check_output(
            command,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        ).strip()
    except Exception:
        return ""


def run_command(command, timeout=15):
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
        return result.returncode == 0, result.stdout.strip()
    except Exception as exc:
        return False, str(exc)


def request_json(method, url, payload=None, timeout=8, auth=None, verify_tls=True):
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if auth:
        token = base64.b64encode(("%s:%s" % auth).encode("utf-8")).decode("ascii")
        headers["Authorization"] = "Basic " + token
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    context = None if verify_tls else ssl._create_unverified_context()
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def get_serial():
    serial = read_first(
        [
            "/sys/class/dmi/id/product_serial",
            "/sys/class/dmi/id/chassis_serial",
            "/sys/class/dmi/id/board_serial",
        ]
    )
    if serial:
        return serial
    return run_text(["dmidecode", "-s", "system-serial-number"])


def get_route_iface(target_host):
    output = run_text(["ip", "route", "get", target_host])
    match = re.search(r"\bdev\s+(\S+)", output)
    return match.group(1) if match else ""


def iface_mac(iface):
    if not iface:
        return ""
    return read_first([f"/sys/class/net/{iface}/address"]).lower()


def iface_pci(iface):
    if not iface:
        return ""
    try:
        target = os.path.realpath(f"/sys/class/net/{iface}/device")
    except OSError:
        return ""
    match = re.search(r"([0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7])$", target)
    return match.group(1) if match else ""


def primary_ip(value):
    text = str(value or "").split(",", 1)[0].split("/", 1)[0].strip()
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return text


def parse_target_network(value):
    parts = [item.strip() for item in str(value or "").split(",")]
    address = parts[0] if parts else ""
    gateway = parts[1] if len(parts) > 1 else ""
    try:
        interface = ipaddress.ip_interface(address)
    except ValueError:
        return "", "", gateway
    return str(interface.ip), str(interface.netmask), gateway


def ping_ok(ip):
    if not ip:
        return False
    return (
        subprocess.call(
            ["ping", "-c", "1", "-W", "1", ip],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        == 0
    )


def ipmitool(args, timeout=15):
    return run_command(["ipmitool", "-I", "open", *args], timeout=timeout)


def local_bmc_channel():
    for module in ("ipmi_msghandler", "ipmi_si", "ipmi_devintf"):
        run_command(["modprobe", module], timeout=10)
    ok, output = ipmitool(["mc", "info"])
    if not ok:
        return "", f"local IPMI/KCS unavailable: {output}"
    for channel in range(1, 17):
        ok, output = ipmitool(["lan", "print", str(channel)])
        if ok and "IP Address" in output:
            return str(channel), ""
    return "", "no BMC LAN channel found"


def parse_lan(output):
    values = {}
    for line in str(output or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip().lower()] = value.strip()
    return values


def lan_readback(channel):
    ok, output = ipmitool(["lan", "print", channel])
    if not ok:
        return {}, output
    return parse_lan(output), ""


def user_readback(channel, user_id, username):
    ok, users_output = ipmitool(["user", "list", channel])
    if not ok:
        return False, False, {}, f"user list failed: {users_output}"
    user_pattern = re.compile(
        rf"^\s*{re.escape(str(user_id))}\s+{re.escape(username)}(?:\s+|$)",
        re.IGNORECASE,
    )
    user_ok = any(user_pattern.search(line) for line in users_output.splitlines())

    ok, access_output = ipmitool(["channel", "getaccess", channel, str(user_id)])
    if not ok:
        return user_ok, False, {}, f"channel getaccess failed: {access_output}"
    access = parse_lan(access_output)
    link_auth = access.get("link authentication", "").lower()
    ipmi_messaging = access.get("ipmi messaging", "").lower()
    privilege = access.get("privilege level", "").lower()
    access_ok = (
        link_auth in {"enabled", "on", "true", "yes"}
        and ipmi_messaging in {"enabled", "on", "true", "yes"}
        and privilege in {"administrator", "admin", "4"}
    )
    return user_ok, access_ok, access, ""


def select_user_id(channel, username):
    ok, output = ipmitool(["user", "list", channel])
    if not ok:
        return "", output
    free_ids = []
    for line in output.splitlines():
        fields = line.split()
        if not fields or not fields[0].isdigit():
            continue
        user_id = fields[0]
        name = fields[1] if len(fields) > 1 else ""
        if name == username:
            return user_id, ""
        if not name or name.lower() in {"true", "false"}:
            free_ids.append(int(user_id))
    candidates = [item for item in free_ids if item >= 3]
    if candidates:
        return str(min(candidates)), ""
    return "", f"no free BMC user slot for {username}"


def configure_bmc(config):
    target_ip, target_netmask, target_gateway = parse_target_network(config.get("bmc_ip", ""))
    target_user = str(config.get("bmc_user") or "").strip()
    target_pass = str(config.get("bmc_pass") or "")
    result = {
        "bmc_ip": target_ip,
        "bmc_user": target_user,
        "bmc_pass": target_pass,
        "bmc_configured": False,
        "bmc_reused": False,
        "bmc_readback_ok": False,
        "bmc_user_readback_ok": False,
        "bmc_access_readback_ok": False,
        "bmc_network_readback_ok": False,
        "bmc_lan_access_enabled": False,
        "bmc_reachable": False,
        "bmc_auth_ok": False,
        "bmc_channel": "",
        "bmc_user_id": "",
        "bmc_access": {},
        "bmc_verify_pending": False,
        "bmc_method": "local-ipmi-kcs",
        "error_code": "",
        "message": "",
    }
    if not (target_ip and target_netmask and target_user and target_pass):
        result.update(error_code="BMC_PLAN_REQUIRED", message="target BMC IP/prefix/user/password is incomplete")
        return result
    if not Path("/usr/bin/ipmitool").exists() and not Path("/bin/ipmitool").exists():
        result.update(error_code="BMC_TOOL_MISSING", message="ipmitool is not installed in Stage1 environment")
        return result

    channel, error = local_bmc_channel()
    result["bmc_channel"] = channel
    if not channel:
        result.update(error_code="BMC_KCS_UNAVAILABLE", message=error)
        return result
    before, error = lan_readback(channel)
    if error:
        result.update(error_code="BMC_READ_FAILED", message=error)
        return result
    current_ip = before.get("ip address", "")

    if current_ip != target_ip and ping_ok(target_ip):
        result.update(
            error_code="BMC_IP_CONFLICT",
            message=f"target BMC IP {target_ip} already responds while local BMC reports {current_ip or 'unknown'}",
        )
        return result

    user_id, error = select_user_id(channel, target_user)
    if not user_id:
        result.update(error_code="BMC_USER_SLOT_UNAVAILABLE", message=error)
        return result
    result["bmc_user_id"] = user_id
    commands = [
        ["user", "set", "name", user_id, target_user],
        ["user", "set", "password", user_id, target_pass],
        ["user", "enable", user_id],
        ["channel", "setaccess", channel, user_id, "callin=on", "ipmi=on", "link=on", "privilege=4"],
        ["user", "priv", user_id, "4", channel],
        ["lan", "set", channel, "access", "on"],
        ["lan", "set", channel, "ipsrc", "static"],
        ["lan", "set", channel, "ipaddr", target_ip],
        ["lan", "set", channel, "netmask", target_netmask],
    ]
    if target_gateway:
        commands.append(["lan", "set", channel, "defgw", "ipaddr", target_gateway])
    for command in commands:
        ok, output = ipmitool(command, timeout=20)
        if not ok:
            result.update(
                error_code="BMC_CONFIG_FAILED",
                message=f"ipmitool {' '.join(command[:4])} failed: {output}",
            )
            return result
    result["bmc_configured"] = True
    result["bmc_lan_access_enabled"] = True

    user_ok, access_ok, access, error = user_readback(channel, user_id, target_user)
    result["bmc_user_readback_ok"] = user_ok
    result["bmc_access_readback_ok"] = access_ok
    result["bmc_access"] = access
    if error or not user_ok or not access_ok:
        result.update(
            error_code="BMC_USER_READBACK_FAILED",
            message=error or f"user={target_user} id={user_id} user_ok={user_ok} access_ok={access_ok}",
        )
        return result

    after, error = lan_readback(channel)
    actual_ip = after.get("ip address", "")
    actual_netmask = after.get("subnet mask", "")
    actual_gateway = after.get("default gateway ip", "")
    result["bmc_ip"] = actual_ip or target_ip
    result["bmc_network_readback_ok"] = (
        actual_ip == target_ip
        and actual_netmask == target_netmask
        and (not target_gateway or actual_gateway == target_gateway)
    )
    result["bmc_readback_ok"] = (
        result["bmc_user_readback_ok"]
        and result["bmc_access_readback_ok"]
        and result["bmc_network_readback_ok"]
        and result["bmc_lan_access_enabled"]
    )
    if not result["bmc_network_readback_ok"]:
        result.update(
            error_code="BMC_READBACK_FAILED",
            message=f"actual={actual_ip}/{actual_netmask},{actual_gateway} target={target_ip}/{target_netmask},{target_gateway}",
        )
        return result
    result["bmc_verify_pending"] = True
    result["message"] = "local IPMI/KCS readback passed; waiting for collector IPMI LAN verification"
    return result


def hardware_snapshot():
    memory_kib = ""
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                memory_kib = line.split()[1]
                break
    except OSError:
        pass
    block_devices = []
    try:
        block_devices = json.loads(
            run_text(["lsblk", "-J", "-b", "-o", "NAME,TYPE,SIZE,MODEL,SERIAL,ROTA"])
            or "{}"
        ).get("blockdevices", [])
    except Exception:
        pass
    return {
        "manufacturer": read_first(["/sys/class/dmi/id/sys_vendor"]),
        "product": read_first(["/sys/class/dmi/id/product_name"]),
        "cpu_count": os.cpu_count() or 0,
        "memory_kib": memory_kib,
        "block_devices": block_devices,
        "pci_devices": run_text(["lspci", "-mm"]).splitlines(),
    }


def main():
    collector = COLLECTOR_URL.rstrip("/")
    collector_host = urlparse(collector).hostname or "10.0.0.10"
    sn = get_serial()
    iface = get_route_iface(collector_host)
    mac = iface_mac(iface)
    config = {}
    error_code = ""
    message = ""

    if sn:
        try:
            config = request_json("GET", f"{collector}/api/v1/config/{quote(sn)}")
        except urllib.error.HTTPError as exc:
            error_code = "CONFIG_NOT_FOUND"
            message = f"collector config lookup failed: HTTP {exc.code}"
        except Exception as exc:
            error_code = "CONFIG_FETCH_FAILED"
            message = str(exc)
    else:
        error_code = "SN_NOT_FOUND"
        message = "system serial number is empty"

    progress_base = {
        "sn": sn,
        "pxe_mac": mac,
        "iface_name": iface,
        "pci_bus": iface_pci(iface),
    }
    if config:
        request_json(
            "POST",
            f"{collector}/api/v1/report",
            payload={**progress_base, "stage1_status": "bmc_configuring"},
        )
    bmc_result = configure_bmc(config) if config else {
        "bmc_ip": "",
        "bmc_user": "",
        "bmc_pass": "",
        "bmc_configured": False,
        "bmc_reused": False,
        "bmc_readback_ok": False,
        "bmc_user_readback_ok": False,
        "bmc_access_readback_ok": False,
        "bmc_network_readback_ok": False,
        "bmc_lan_access_enabled": False,
        "bmc_reachable": False,
        "bmc_auth_ok": False,
        "bmc_channel": "",
        "bmc_user_id": "",
        "bmc_access": {},
        "bmc_verify_pending": False,
        "bmc_method": "local-ipmi-kcs",
    }
    if bmc_result.get("error_code"):
        error_code = bmc_result["error_code"]
        message = bmc_result.get("message", "")
    elif config:
        request_json(
            "POST",
            f"{collector}/api/v1/report",
            payload={**progress_base, **bmc_result, "stage1_status": "stage1_capturing"},
        )
    payload = {
        "sn": sn,
        **bmc_result,
        **progress_base,
        "hardware": hardware_snapshot(),
        "error_code": error_code,
        "message": message,
    }
    request_json("POST", f"{collector}/api/v1/report", payload=payload, timeout=45)


for attempt in range(1, 31):
    try:
        main()
        break
    except Exception as exc:
        Path("/run/maas-stage1-report.last-error").write_text(str(exc), encoding="utf-8")
        time.sleep(5)
