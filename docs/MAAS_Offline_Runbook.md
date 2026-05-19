# MAAS 离线部署与 GPU 服务器交付 Runbook

## 流程图

![GPU 服务器交付链路](./gpu-server-delivery-flow.svg)

## 0. 目标与约束

- 目标：离线环境下完成 GPU 服务器的纳管、清盘初始化、批量装机，并形成可复用的操作手册。
- 约束：集群节点无法联网；所有依赖（ISO、apt 镜像、工具）必须由内网 HTTP 提供。

## 1. 离线资源服务（单服务 + 单端口 + 单根目录）

### 1.1 目录规划（示例）

- 统一根目录：`/srv/maas-offline`
- APT 镜像目录：`/srv/maas-offline/mirror`
- ISO 目录：`/srv/maas-offline/iso`
- 工具目录：`/srv/maas-offline/tools`

### 1.2 用一个端口对外提供三个路径

仓库内提供了一个轻量的多路径静态文件服务脚本：

- 服务脚本：[maas-offline-http.py](./maas-offline-http.py)
- systemd 单元样例：[maas-offline-http.service](./systemd/maas-offline-http.service)

建议统一用 `8083`：

- `http://10.161.139.136:8083/mirror/` → `/srv/maas-offline/mirror`
- `http://10.161.139.136:8083/iso/` → `/srv/maas-offline/iso`
- `http://10.161.139.136:8083/tools/` → `/srv/maas-offline/tools`

### 1.3 部署为 systemd 服务

在 `10.161.139.136` 上执行：

```bash
sudo mkdir -p /srv/maas-offline/{mirror,iso,tools}
sudo mkdir -p /opt/maas-offline
sudo cp ./docs/maas-offline-http.py /opt/maas-offline/maas-offline-http.py
sudo chmod +x /opt/maas-offline/maas-offline-http.py

sudo cp ./docs/systemd/maas-offline-http.service /etc/systemd/system/maas-offline-http.service
sudo systemctl daemon-reload
sudo systemctl enable --now maas-offline-http.service

sudo systemctl status maas-offline-http.service --no-pager
journalctl -u maas-offline-http.service -n 100 --no-pager
```

如果你之前已经把资源分散放在旧目录，可以先归并到统一根目录：

```bash
sudo mkdir -p /srv/maas-offline/{mirror,iso,tools}
sudo rsync -a /srv/maas-mirror/ /srv/maas-offline/mirror/
sudo rsync -a /root/ubuntu22.04.4/ /srv/maas-offline/iso/
sudo rsync -a /root/tools/ /srv/maas-offline/tools/
```

如果希望“一键替换旧的临时 HTTP 服务 + 拉起统一的 systemd 服务”，可直接用：

```bash
chmod +x ./docs/maas-offline-oneclick.sh
./docs/maas-offline-oneclick.sh
```

部署后自检：

```bash
curl -I http://10.161.139.136:8083/mirror/
curl -I http://10.161.139.136:8083/iso/
curl -I http://10.161.139.136:8083/tools/
```

### 1.4 工具分发约定

清盘脚本会按需从 `BASE_URL` 下载工具：

- `storcli_007.2508.0000.0000_all.deb`
- `MegaCli64`
- `sas3ircu`
- `sas2ircu`

建议统一放在 `/srv/maas-offline/tools/` 下，通过：

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

现在仓库里已经统一成“同一个策略 YAML 管 cloud-init 与 storage”：

- 配置文件：[deploy-policy.yaml](./cloud-init/deploy-policy.yaml)
- storage 应用脚本：[maas_apply_storage_policy.py](./scripts/maas_apply_storage_policy.py)

系统盘分区大小直接写在 `deploy-policy.yaml` 的 `storage` 段里：

```yaml
defaults:
  storage:
    boot_size: 2G
    root_size: 200G
    data_mount: /data

policies:
  default: {}
  h100:
    match_tags: [h100]
    storage:
      root_size: 300G
```

含义：

- `boot_size`：`/boot`
- `root_size`：`/`
- `data_mount`：剩余空间创建分区并挂到该目录，默认 `/data`

在 MAAS 里底层仍然是通过 CLI/API 落地：

- `block-device set-boot-disk`
- `machine set-storage-layout`
- `partitions create`
- `partition mount`

注意：

- MAAS 在 UEFI 模式下会自动创建一个约 `512MiB` 的 `EFI System Partition` 挂载到 `/boot/efi`
- `boot_size=2G` 对应的是 `/boot` 分区，不是 `/boot/efi`
- 所以你要的合理默认应理解为：
  - `/boot/efi`：MAAS 自动创建（约 512MiB）
  - `/boot`：2G
  - `/`：200G 或 300G
  - `/data`：剩余空间

### 4.3 按策略 YAML 套用存储策略（推荐）

推荐直接用和 deploy 相同的策略来源：

- 存储脚本：[maas_apply_storage_policy.py](./scripts/maas_apply_storage_policy.py)
- 老脚本仍保留：[maas_apply_storage_policy_by_tag.sh](./scripts/maas_apply_storage_policy_by_tag.sh)

默认规则和 deploy 一样：

- 不指定 `--policy` 时，优先按节点 `tag` 命中 `match_tags`
- 节点没有 tag，或未命中任何策略时，自动回落到 `policies.default`
- 显式指定 `--policy` 时，强制使用该策略

推荐执行方式：

1. 先 dry-run 看命中情况：

```bash
PROFILE=admin ./docs/scripts/maas_apply_storage_policy.py --all-ready --dry-run
```

2. 按 tag 对 Ready 节点正式套存储：

```bash
PROFILE=admin ./docs/scripts/maas_apply_storage_policy.py --tag group-a
```

3. 对单台机器强制指定策略：

```bash
PROFILE=admin ./docs/scripts/maas_apply_storage_policy.py --policy h100 fntnkq
```

4. 如果你只想临时按 tag 指定单一 root 大小，仍可用旧脚本：

```bash
BOOT_SIZE=2G PROFILE=admin ./docs/scripts/maas_apply_storage_policy_by_tag.sh group-a 200G
PROFILE=admin ./docs/scripts/maas_apply_storage_policy_by_tag.sh group-b 300G
```

这一步必须在 `deploy` 前执行。核心原因：

- RAID 控制器层面的 `BootDrive` 只影响 BIOS/控制器启动顺序
- MAAS 自己仍然维护“哪个 block device 是 boot disk”
- 如果没有显式把 MAAS 的 boot disk 改成 `sda`，它可能继续按默认选择 `nvme0n1`
- 之前测试机把系统装到 `nvme`，就是因为这里没有正确生效

### 4.4 CLI 方式部署（以 fntnkq 为例）

```bash
maas admin machine deploy fntnkq distro_series=jammy
```

携带 raw cloud-init：

```bash
maas admin machine deploy fntnkq distro_series=jammy user_data="$(base64 -w0 ./user-data.yaml)"
```

仓库脚本：

- 单机部署：[maas_deploy_one.sh](./scripts/maas_deploy_one.sh)
- 批量部署：[maas_deploy_batch.sh](./scripts/maas_deploy_batch.sh)
- 策略渲染核心：[maas_policy_deploy.py](./scripts/maas_policy_deploy.py)
- 默认策略 YAML：[deploy-policy.yaml](./cloud-init/deploy-policy.yaml)
- 兼容旧方式的 raw 模板：[default-user-data.yaml](./cloud-init/default-user-data.yaml)

首次使用如果系统里没有 `PyYAML`：

```bash
sudo apt-get update
sudo apt-get install -y python3-yaml
```

### 4.4.1 默认规则

新的部署入口默认按以下规则工作：

- 如果执行脚本时没有指定 `policy`，先读取节点当前 tag
- 节点 tag 命中策略 YAML 的 `match_tags`，就套用对应策略
- 节点没有配置 tag，或者 tag 没命中任何策略，自动回落到 `policies.default`
- 如果脚本明确指定了 `policy`，则强制使用该策略，不再看节点 tag

### 4.4.2 YAML 可配置项

`deploy-policy.yaml` 里现在可以直接指定：

- 可 sudo 的登录用户
- 是否 `NOPASSWD`
- 该 sudo 用户的明文密码
- `root` 是否启用以及 `root` 密码
- SSH 是否允许密码登录
- SSH 是否允许 root 登录
- SSH 可登录用户白名单 `AllowUsers`
- sudo 用户的 `ssh_authorized_keys`

示例：

```yaml
defaults:
  profile: admin
  distro_series: jammy
  sudo_user:
    name: ubuntu
    password: "Lexun@12#$"
    sudo_nopasswd: true
  root:
    enabled: true
    password: "Lexun@12#$"
  ssh:
    password_authentication: true
    permit_root_login: true
    pubkey_authentication: true
    allow_users: [ubuntu, root]

policies:
  default: {}
  h100:
    match_tags: [h100]
    sudo_user:
      name: h100ops
      password: "H100Ops@123"
      sudo_nopasswd: true
    ssh:
      allow_users: [h100ops, root]
```

### 4.4.3 直接使用方法

0. 推荐顺序：先套 storage，再 deploy：

```bash
PROFILE=admin ./docs/scripts/maas_apply_storage_policy.py --policy default fntnkq
PROFILE=admin SERIES=jammy ./docs/scripts/maas_deploy_one.sh fntnkq
```

1. 默认自动策略，单机部署：

```bash
PROFILE=admin SERIES=jammy ./docs/scripts/maas_deploy_one.sh fntnkq
```

2. 强制指定策略名，不看节点 tag：

```bash
PROFILE=admin SERIES=jammy ./docs/scripts/maas_deploy_one.sh fntnkq h100
```

3. 保持兼容，直接喂你自己的 raw cloud-init：

```bash
PROFILE=admin SERIES=jammy ./docs/scripts/maas_deploy_one.sh fntnkq ./my-user-data.yaml
```

4. 按 tag 批量部署，未指定 `--policy` 时自动按节点 tag 选策略，未命中则走 `default`：

```bash
PROFILE=admin SERIES=jammy ./docs/scripts/maas_deploy_batch.sh --tag group-a
```

5. 批量部署全部 `Ready` 节点：

```bash
PROFILE=admin SERIES=jammy ./docs/scripts/maas_deploy_batch.sh --all-ready
```

6. 批量部署时强制所有节点都走同一个策略：

```bash
PROFILE=admin SERIES=jammy ./docs/scripts/maas_deploy_batch.sh --tag group-a --policy default
```

7. 先 dry-run 看策略命中与渲染结果，再正式 deploy：

```bash
PROFILE=admin SERIES=jammy ./docs/scripts/maas_deploy_batch.sh --tag h100 --dry-run
```

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

## 5. 部署失败排查

### 5.1 这次 `Failed deployment` 的判断

从你贴的事件看：

- 机器已经成功 PXE 启动、下载内核/initrd/squashfs
- 已进入目标系统并跑到了 `cloud-init` 的 final 阶段

这说明：

- 不是最前面的 PXE / 镜像下载问题
- 更像是“装机后校验或最终状态上报失败”，以及更关键的：这次装机目标盘选错了，系统实际装到了 `nvme`

### 5.2 为什么系统跑到了 NVMe

根因不是前面的 RAID 清理脚本，而是 MAAS 的 boot disk 选择没有被正确改到 `sda`。

已经修复：

- `maas_apply_storage_policy_by_tag.sh` 现在会优先选择 `sda`
- 如果没有 `sda`，优先非 `nvme` 的 SSD/逻辑盘
- 只有找不到合适 SSD 时，才会退化选择其他盘

### 5.3 正确的重新部署顺序

建议按这条做：

1. 释放失败节点：

```bash
maas admin machine release fntnkq comment="retry deploy after fixing boot disk" erase=false
```

2. 应用存储策略，强制让 `sda` 作为系统盘：

```bash
PROFILE=admin ./docs/scripts/maas_apply_storage_policy_by_tag.sh group-a 200G
```

3. 检查 MAAS 视角下的 boot disk 和分区：

```bash
maas admin block-devices read fntnkq
maas admin machine read fntnkq
```

4. 再执行 deploy：

```bash
PROFILE=admin ./docs/scripts/maas_deploy_one.sh fntnkq
```

5. 如果再次失败，继续查这几处：

- `maas admin events query id=fntnkq limit=100`
- `maas admin machine get-curtin-config fntnkq`
- 机器本地：`/var/log/cloud-init.log`
- 机器本地：`/var/log/cloud-init-output.log`
- 机器本地：`/var/log/installer/curtin-install.log`
