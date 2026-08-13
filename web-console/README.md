# MAAS Offline Web Console

轻量离线控制台，无外部依赖，默认只执行 dry-run 动作。

启动：

```bash
python3 web-console/server.py
```

可选环境变量：

```bash
MAAS_SOURCES=/srv/maas-offline
MAAS_CONSOLE_HOST=0.0.0.0
MAAS_CONSOLE_PORT=8088
MAAS_CONSOLE_ALLOW_MUTATION=1
MAAS_LAB_CONFIG=docs/lab/two-node-physical.local.json
```

`docs/lab/two-node-physical.local.json` 可选设置 `server.stage1_server_ip`，用于无盘 Stage1 走独立网卡或独立子网时给 Web 控制台传递正确的引导地址。

控制台在切换到 `diskless_stage1` 前会自动校验 Stage1 PXE/TFTP 资源。若缺少 `ipxe.efi`、`undionly.kpxe`、`stage1.ipxe`、`inventory.csv`、`defaults.yaml`、`state.json` 或 dnsmasq/collector 配置文件，会直接阻断切换并回显失败项，避免节点再次卡在 `tftp://.../ipxe.efi` 超时。

首页按 `maintenance_locked`、`diskless_stage1`、`maas_provision` 三种模式执行后端门禁。无盘模式只允许目标 BMC 配置、回读、硬件采集和导出；MAAS 模式才允许导入、Commissioning、清盘、RAID/分区和部署。旧报告若没有本机 IPMI/KCS 配置或复用证据，不会进入 `stage1_ready`。

控制台默认不改动系统。只有在服务环境里显式设置 `MAAS_CONSOLE_ALLOW_MUTATION=1` 后，“进入无盘抓配 / 切到无盘 / 切回 MAAS”等动作才会真实执行；否则页面会明确提示当前是“仅预演”，节点不会有任何动静。

在真实执行模式下，切到 `diskless_stage1` 只负责切换 PXE/DHCP/TFTP 控制面，不代表节点会自动启动。标准流程是：切换成功后先人工重启目标节点，让它们进入 PXE/Stage1；BMC 连通与账号验证属于节点进入 Stage1 之后的抓配结果，不应反过来作为进入 Stage1 的前置依赖。

默认页面：`http://127.0.0.1:8088`

## Ansible 与节点验收

控制台从 `<MAAS_SOURCES>/ansible/bundles` 读取上传的 `.tar.gz/.tgz` 剧本包。MAAS 服务端一键部署会安装 `python3-paramiko`，并在存在 `<MAAS_SOURCES>/ansible/runtime/debs/*.deb` 时离线安装 Ansible Core。缺少 Ansible 运行时时，页面会禁用剧本执行。

剧本包格式见 `docs/ansible/README.md`。系统部署完成后，控制节点自动检测业务 IP、TCP/22 和默认系统账号 SSH 登录；这是进入 Ansible 阶段的通用门禁。Ubuntu/内核/分区、GPU、OFED、Docker 等项目要求不在平台中写死，统一由剧本包 `manifest.yaml` 的 `checks` 定义，结果仅供节点验收，不自动阻塞流程。Ansible 实际执行成功后，节点进入最终的“节点验收 / Ready”阶段。

自动化页未选择节点时，默认对全部“已部署且网络/SSH 门禁通过”的节点执行；勾选节点后只执行所选的符合条件节点。`--check` 任务只用于预演，不计为项目配置完成。

物理双机测试流程见：

```text
docs/lab/Physical_Two_Node_Test_Runbook.md
```

默认不改动系统。未设置 `MAAS_CONSOLE_ALLOW_MUTATION=1` 时，控制台里的部署、导入和 PXE 模式切换动作都不会真正执行。
