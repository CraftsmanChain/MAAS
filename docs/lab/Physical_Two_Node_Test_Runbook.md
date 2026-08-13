# Physical Two-Node Test Runbook

This runbook covers the first physical validation loop:

- Server node: `192.168.2.200`, runs offline resources, MAAS, Stage1, and Web Console.
- Client node: `192.168.2.201`, installed by MAAS after Stage1 discovery.
- Server BMC: `192.168.2.150`.
- Client BMC: `192.168.2.139`.

Credentials are stored only in `docs/lab/two-node-physical.local.json`, which is ignored by git.

## Network Model

Use the time-exclusive PXE model first:

1. `diskless_stage1`: MAAS rack/DHCP is stopped, Stage1 dnsmasq DHCP/TFTP is started.
2. `maas_provision`: Stage1 dnsmasq is stopped, MAAS rack is started.
3. `maintenance_locked`: both DHCP/TFTP paths are stopped.

If the switch does not support bonding or VLAN isolation, keep the first test unbonded. The only hard requirement is that the client PXE NIC and the server DHCP/TFTP NIC are on the same L2 segment and no other DHCP server is active on that segment during Stage1.

## Required Local Edit

Before starting real Stage1 DHCP/TFTP, set these fields in `docs/lab/two-node-physical.local.json`:

```json
"stage1_server_ip": "SERVER_STAGE1_IP",
"dhcp_interface": "SERVER_PXE_INTERFACE",
"dhcp_range": "192.168.2.210,192.168.2.230,12h"
```

If Stage1 uses a dedicated NIC or subnet, set `stage1_server_ip` to the address reachable by the client during PXE and Stage1. If it is omitted, the Web Console falls back to `external_ip`.

If `192.168.2.0/24` already has production DHCP, do not use that subnet for Stage1 DHCP. Use an isolated NIC, isolated switch port group, or temporary direct L2 segment.

## Web Flow

Open the console on the server:

```bash
MAAS_CONSOLE_HOST=0.0.0.0 MAAS_CONSOLE_ALLOW_MUTATION=1 python3 web-console/server.py
```

Then open:

```text
http://192.168.2.200:8088
```

Run actions in this order:

1. 校验离线资源
2. 检查网络连通
3. 写入测试节点
4. 部署 MAAS 服务端
5. 部署无盘服务
6. 进入无盘抓配
7. Manually reboot the client, let it boot by PXE, and wait for the Stage1 report
8. 生成 MAAS 导入表
9. 进入 MAAS 部署
10. 导入节点
11. 批量部署

Keep mutation disabled for rehearsal. Enable `MAAS_CONSOLE_ALLOW_MUTATION=1` only on the real server when ready to change services.
