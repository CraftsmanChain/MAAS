# MAAS 离线部署与 GPU 服务器交付 Runbook

## 流程图

![GPU 服务器交付链路](./gpu-server-delivery-flow.svg)

## 0. 目标与约束

- 目标：离线环境下完成 GPU 服务器的纳管、清盘初始化、批量装机，并形成可复用的操作手册。
- 约束：集群节点无法联网；所有依赖（ISO、apt 镜像、工具）必须由内网 HTTP 提供。

## 1. 离线资源服务（单端口多路径 + systemd）

### 1.1 目录规划（示例）

- MAAS apt 镜像：`/srv/maas-mirror`
- Ubuntu ISO：`/root/ubuntu22.04.4`
- 工具分发：`/root/tools`

### 1.2 用一个端口对外提供三个路径

仓库内提供了一个轻量的多路径静态文件服务脚本：

- 服务脚本：[maas-offline-http.py](./maas-offline-http.py)
- systemd 单元样例：[maas-offline-http.service](./systemd/maas-offline-http.service)

建议统一用 `8083`：

- `http://10.161.139.136:8083/mirror/` → `/srv/maas-mirror`
- `http://10.161.139.136:8083/iso/` → `/root/ubuntu22.04.4`
- `http://10.161.139.136:8083/tools/` → `/root/tools`

### 1.3 部署为 systemd 服务

在 `10.161.139.136` 上执行：

```bash
sudo mkdir -p /opt/maas-offline
sudo cp ./docs/maas-offline-http.py /opt/maas-offline/maas-offline-http.py
sudo chmod +x /opt/maas-offline/maas-offline-http.py

sudo cp ./docs/systemd/maas-offline-http.service /etc/systemd/system/maas-offline-http.service
sudo systemctl daemon-reload
sudo systemctl enable --now maas-offline-http.service

sudo systemctl status maas-offline-http.service --no-pager
journalctl -u maas-offline-http.service -n 100 --no-pager
```

如果希望“一键替换掉现有 3 个 http.server + 拉起 systemd 服务”，可直接用：

```bash
chmod +x ./docs/maas-offline-oneclick.sh
./docs/maas-offline-oneclick.sh
```

### 1.4 工具分发约定

清盘脚本会按需从 `BASE_URL` 下载工具：

- `storcli_007.2508.0000.0000_all.deb`
- `MegaCli64`
- `sas3ircu`
- `sas2ircu`

建议统一放在 `/root/tools/` 下，通过：

- `http://10.161.139.136:8083/tools/<文件名>` 访问

## 2. MAAS 纳管（节点批量录入/发现/标签/分组）

### 2.1 两种纳管模式

#### A. PXE 自动发现（推荐）

- 服务器设置 PXE Boot
- 与 MAAS 管理网络二层互通（DHCP/TFTP 由 MAAS 提供）
- 机器启动后自动 enlist

#### B. 批量显式录入（BMC/IPMI）

用于你已经掌握 BMC 地址、账号、密码，希望 MAAS 直接可控电源。

提示：不同 MAAS 版本参数字段略有差异，以 `maas admin machines create --help` 输出为准。常见思路是：

- `mac_addresses=<PXE 网口 MAC>`
- `power_type=ipmi`
- `power_parameters_power_address=<BMC IP>`
- `power_parameters_power_user=<BMC 用户>`
- `power_parameters_power_pass=<BMC 密码>`

### 2.2 标签/分组（推荐做法）

- 用 tag 标识角色：`gpu`、`h100`、`a800`、`cx6`、`prod`、`staging`
- 用资源池/zone/availability-zone 区分机房/机架/交换域
- 用约束选择部署：按 tag 批量 deploy / test / script-run

### 2.3 用文件批量纳管并打标签（CLI）

假设你有一个 `nodes.csv`，每行包含：

```text
hostname,pxe_mac,bmc_ip,bmc_user,bmc_pass,tags
node-GPU-135,52:54:00:aa:bb:cc,10.0.0.135,ADMIN,*****,"gpu,group-a"
node-GPU-136,52:54:00:aa:bb:dd,10.0.0.136,ADMIN,*****,"gpu,group-b"
```

建议流程：

1. 创建机器（纳管）
2. 用 `tag update-nodes add=<system_id>` 打标签

仓库脚本：

- 批量纳管 + 打标签：[maas_bulk_import_and_tag.sh](./scripts/maas_bulk_import_and_tag.sh)

```bash
PROFILE=admin ./docs/scripts/maas_bulk_import_and_tag.sh ./nodes.csv
```

## 3. 初始化（清 RAID + 清盘）

### 3.1 为什么用 testing 脚本

- commissioning 内置脚本不可改；离线环境中容易因默认脚本安装包失败导致 `Failed commissioning`
- 将“清盘/清 RAID”作为 `testing` 脚本执行，可规避 commissioning 默认脚本链路

### 3.2 清盘脚本（默认策略）

- 脚本：`wipe-raid-and-disks-test`
- 目标：
  - 识别 RAID 控制器类型并自动选择工具
  - SSD 系统盘：每块 SSD 创建单盘 `RAID0 VD`，第一块命名 `ssd01` 并设为 BootDrive
  - 全盘清理：对系统可见盘执行 `wipefs/sgdisk/blkdiscard`（包含 NVMe）

## 4. 批量装机（Deploy）

### 4.1 存储：系统盘选择

你的默认策略：

- SSD：系统盘（两块盘作为 `sda/sdb` 逻辑盘）
- NVMe：数据盘

### 4.2 默认分区策略在哪里配置

你描述的默认策略：

- `/boot` 2G
- `/` 200G / 300G
- `/data` 其余

在 MAAS 里通常有两种来源：

1. 机器级 Storage 配置（UI：Machines → 选机器 → Storage）
2. 通过 CLI/API 配置 storage（`block-device set-boot-disk` / `machine set-storage-layout` / `partitions create` / `partition mount`）

### 4.3 按 tag 自动套用存储策略（推荐）

准备两个标签：

- `group-a`：`/` 200G
- `group-b`：`/` 300G

使用仓库脚本按 tag 套用：

- [maas_apply_storage_policy_by_tag.sh](./scripts/maas_apply_storage_policy_by_tag.sh)

```bash
PROFILE=admin ./docs/scripts/maas_apply_storage_policy_by_tag.sh group-a 200G
PROFILE=admin ./docs/scripts/maas_apply_storage_policy_by_tag.sh group-b 300G
```

### 4.4 CLI 方式部署（以 fntnkq 为例）

```bash
maas admin machine deploy fntnkq distro_series=jammy
```

携带 cloud-init：

```bash
maas admin machine deploy fntnkq distro_series=jammy user_data="$(base64 -w0 ./user-data.yaml)"
```

仓库脚本：

- [maas_deploy_one.sh](./scripts/maas_deploy_one.sh)

### 4.5 批量锁机（防误操作）

单台：

```bash
maas admin machine lock fntnkq comment="freeze after delivery"
```

按 tag 批量：

- [maas_bulk_lock.sh](./scripts/maas_bulk_lock.sh)

```bash
PROFILE=admin ./docs/scripts/maas_bulk_lock.sh group-a
```

