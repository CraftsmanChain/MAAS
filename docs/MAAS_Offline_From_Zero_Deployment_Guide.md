# MAAS 离线全量包：从零部署与 PXE 操作手册

本文适用于全量包 `maas-offline-<RELEASE_VERSION>.tar.zst` 的全新控制节点部署。

## 1. 版本与网络变量

先在 Ubuntu 22.04 控制节点设置变量。请按现场修改，密码不要写入文档或 Shell 历史；命令会交互读取密码。

```bash
export RELEASE_VERSION='2026.08.12-03'
export RELEASE_ARCHIVE="maas-offline-${RELEASE_VERSION}.tar.zst"
export RELEASE_DIR="maas-offline-${RELEASE_VERSION}"

export OFFLINE_ROOT='/srv/maas-offline'
export OFFLINE_HTTP_ROOT='/opt/maas-offline'
export TOOLKIT_ROOT='/opt/maas-offline-toolkit'

# 控制节点：部署/PXE 网卡与 IP。
export PXE_INTERFACE='eno4'
export SERVER_IP='10.0.0.10'
export SERVER_CIDR='10.0.0.10/24'
export PXE_GATEWAY='10.0.0.1'
export PXE_DNS='192.168.2.1'

# 仅给待抓配/待部署节点预留的 DHCP 地址池。
export DHCP_START='10.0.0.11'
export DHCP_END='10.0.0.11'
export DHCP_LEASE='12h'

export MAAS_URL="http://${SERVER_IP}:5240/MAAS"
export HTTP_PORT='8083'
export STAGE1_PORT='8091'
export CONSOLE_PORT='8088'
export MAAS_ADMIN='admin'

read -rsp 'MAAS 管理员密码: ' MAAS_ADMIN_PASSWORD
echo
export MAAS_ADMIN_PASSWORD
```

已验证的软件基线：Ubuntu 22.04、MAAS `3.4.9`、PostgreSQL `14`、Python `3.10`。全量包包含 MAAS、PostgreSQL、Stage1、iPXE 和 Ansible 所需离线资源。

## 2. 恢复全量离线包

在保存全量包的目录执行。先校验包完整性；`*.sha256` 文件与包应放在同一目录。

```bash
sha256sum -c "${RELEASE_ARCHIVE}.sha256"

sudo mkdir -p "$OFFLINE_ROOT" "$TOOLKIT_ROOT"
tar --use-compress-program=unzstd -xf "$RELEASE_ARCHIVE"
cd "$RELEASE_DIR"

sudo rsync -a resources/ "$OFFLINE_ROOT/"
sudo rsync -a toolkit/ "$TOOLKIT_ROOT/"

cd "$TOOLKIT_ROOT"
bash -n docs/maas-control-plane-oneclick.sh \
  docs/diskless-stage1-oneclick.sh \
  docs/scripts/maas_pxe_mode.sh
python3 -m py_compile web-console/server.py
```

全量包不会带入现场节点清单、抓配报告、控制台运行状态或现场凭据。恢复后先在 Web 控制台录入节点 CSV，或按团队流程同步 `stage1/inventory.csv`。

## 3. 安装 MAAS 控制面

```bash
cd "$TOOLKIT_ROOT"

sudo ./docs/maas-control-plane-oneclick.sh \
  --server-ip "$SERVER_IP" \
  --maas-url "$MAAS_URL" \
  --offline-root "$OFFLINE_ROOT" \
  --http-port "$HTTP_PORT" \
  --admin-user "$MAAS_ADMIN" \
  --admin-password "$MAAS_ADMIN_PASSWORD" \
  --maas-dhcp-interface "$PXE_INTERFACE" \
  --maas-dhcp-start "$DHCP_START" \
  --maas-dhcp-end "$DHCP_END" \
  --maas-dhcp-gateway "$PXE_GATEWAY" \
  --maas-dhcp-dns "$PXE_DNS" \
  --install-curtin-template
```

这组 `--maas-dhcp-*` 参数会在 MAAS 初始化完成后立即补齐 PXE VLAN 的 DHCP 配置，确保 `/var/lib/maas/dhcpd-interfaces` 和 `maas-dhcpd.service` 在新环境首次部署后就处于可用状态。

验证 PostgreSQL 与 MAAS API：

```bash
sudo pg_lsclusters
sudo -u postgres pg_isready

sudo systemctl start maas-regiond maas-rackd
curl -fsS "${MAAS_URL}/api/2.0/version/"
maas list
sudo systemctl is-active maas-dhcpd.service
sudo cat /var/lib/maas/dhcpd-interfaces
```

预期：`pg_lsclusters` 显示 `14 main 5432 online`；MAAS API 返回版本 JSON；`maas-dhcpd.service` 为 `active`，且 `dhcpd-interfaces` 包含 `$PXE_INTERFACE`。

## 4. 安装 Web 控制台（8088）

控制面安装不自动启用 Web 控制台，需额外执行一次：

```bash
cd "$TOOLKIT_ROOT"
sudo ./docs/install-web-console.sh

sudo systemctl status maas-web-console.service --no-pager
ss -lntp | grep ":${CONSOLE_PORT}"
curl -fsS "http://127.0.0.1:${CONSOLE_PORT}/api/summary"
```

浏览器地址：`http://${SERVER_IP}:${CONSOLE_PORT}`。

若控制台导入节点时提示 `argument COMMAND: invalid choice: 'admin'`，说明运行控制台的 `ubuntu` 用户尚未拥有 MAAS CLI profile。先切到 MAAS PXE 模式，再执行：

```bash
API_KEY="$(sudo maas apikey --username "$MAAS_ADMIN")"
sudo -u ubuntu maas login admin "$MAAS_URL" "$API_KEY"
sudo -u ubuntu maas list
```

重新执行 `sudo ./docs/install-web-console.sh` 也会自动创建该 profile。这个错误与 CSV 中的 `bmc_pass`/`CHANGE_ME` 无关；后者仅用于节点 BMC 登录。

在控制台配置中心粘贴节点清单 CSV。最小字段：

```csv
hostname,bmc_ip,bmc_user,bmc_pass,sn,25g,tag
node-GPU-201,"192.168.2.139/24,192.168.2.1",bmc-user,CHANGE_ME,EXAMPLE-SN,"10.0.0.11/24,10.0.0.1",cpu
```

保存后应看到“配置校验通过”。将 `CHANGE_ME`、示例 SN 和 BMC 信息替换为现场值。

## 5. 手动安装无盘 Stage1 服务

这一步仅部署服务和资源，不立刻启用无盘 DHCP/TFTP，避免与 MAAS PXE 冲突。

```bash
cd "$TOOLKIT_ROOT"

sudo ./docs/diskless-stage1-oneclick.sh \
  --server-ip "$SERVER_IP" \
  --offline-root "$OFFLINE_ROOT" \
  --http-port "$HTTP_PORT" \
  --stage1-port "$STAGE1_PORT" \
  --dhcp-interface "$PXE_INTERFACE" \
  --dhcp-range "${DHCP_START},${DHCP_END},${DHCP_LEASE}" \
  --dhcp-router "$PXE_GATEWAY" \
  --dhcp-dns "$PXE_DNS" \
  --uefi-ipxe-source ipxe.efi

sudo ./docs/scripts/validate_stage1_pxe.sh \
  --offline-root "$OFFLINE_ROOT" \
  --http-port "$HTTP_PORT" \
  --stage1-port "$STAGE1_PORT"
```

## 6. 切换到无盘抓配模式

仅在目标节点已接入 `$PXE_INTERFACE` 对应网络、准备从 PXE 启动时执行：

```bash
sudo "$TOOLKIT_ROOT/docs/scripts/maas_pxe_mode.sh" diskless_stage1 \
  --offline-root "$OFFLINE_ROOT" \
  --http-port "$HTTP_PORT" \
  --stage1-port "$STAGE1_PORT"

sudo "$TOOLKIT_ROOT/docs/scripts/maas_pxe_mode.sh" status
```

预期状态：`maas-offline-http`、`stage1-collector`、`diskless-stage1-dnsmasq` 为 active；`maas-regiond`、`maas-rackd`、`maas-dhcpd` 为 inactive。

检查 DHCP/TFTP 只绑定 PXE 网卡：

```bash
sudo ss -lunp | grep -E ':(67|69)\b'
sudo grep -E '^(interface|dhcp-range|dhcp-boot|tftp-root|dhcp-option)' \
  /etc/maas-offline/diskless-stage1-dnsmasq.conf
```

完成节点 Stage1 抓配、BMC 回读及硬件上报后，生成 `maas.csv` 并导入 MAAS。可通过 8088 控制台执行，或使用现有导入脚本。

## 7. 切换到 MAAS PXE 模式

无盘抓配完成且需要 MAAS commissioning/存储/部署时，切换到 MAAS PXE。切换会自动停止无盘 DHCP/TFTP。

```bash
sudo "$TOOLKIT_ROOT/docs/scripts/maas_pxe_mode.sh" maas_provision \
  --offline-root "$OFFLINE_ROOT" \
  --http-port "$HTTP_PORT" \
  --stage1-port "$STAGE1_PORT" \
  --maas-dhcp-interface "$PXE_INTERFACE" \
  --maas-dhcp-start "$DHCP_START" \
  --maas-dhcp-end "$DHCP_END" \
  --maas-dhcp-gateway "$PXE_GATEWAY" \
  --maas-dhcp-dns "$PXE_DNS"

sudo "$TOOLKIT_ROOT/docs/scripts/maas_pxe_mode.sh" status
sudo systemctl is-active maas-dhcpd.service
sudo cat /var/lib/maas/dhcpd-interfaces
```

预期状态：`maas-regiond`、`maas-rackd`、`maas-dhcpd` 为 active；`diskless-stage1-dnsmasq` 和 `stage1-collector` 为 inactive。

## 8. 维护锁定与常用检查

维护时同时停止两套 PXE：

```bash
sudo "$TOOLKIT_ROOT/docs/scripts/maas_pxe_mode.sh" maintenance_locked \
  --offline-root "$OFFLINE_ROOT" \
  --http-port "$HTTP_PORT" \
  --stage1-port "$STAGE1_PORT"
```

统一状态检查：

```bash
sudo "$TOOLKIT_ROOT/docs/scripts/maas_pxe_mode.sh" status
sudo pg_lsclusters
curl -fsS "http://127.0.0.1:${CONSOLE_PORT}/api/summary"
curl -fsS "${MAAS_URL}/api/2.0/version/"
```

## 9. 完全清理后重新部署

以下操作会卸载 MAAS、PostgreSQL、Web 控制台、Stage1 服务，并删除离线资源、MAAS 数据库、节点记录、日志和 toolkit。仅在需要把控制节点恢复到“重新验证全量包”的状态时执行。

```bash
sudo systemctl disable --now \
  maas-web-console.service \
  diskless-stage1-dnsmasq.service \
  stage1-collector.service \
  maas-offline-http.service || true

sudo systemctl stop \
  maas-http \
  maas-regiond \
  maas-rackd \
  named \
  bind9 \
  postgresql || true

sudo apt-get purge -y \
  maas-region-api \
  maas-rack-controller \
  maas-cli \
  maas-common \
  bind9 \
  bind9-utils \
  dnsutils \
  bind9-dnsutils \
  postgresql \
  postgresql-contrib || true

sudo apt-get autoremove -y --purge
sudo apt-get clean

sudo rm -rf \
  /etc/maas \
  /var/lib/maas \
  /var/log/maas \
  /var/lib/postgresql \
  /etc/postgresql \
  "$OFFLINE_ROOT" \
  "$OFFLINE_HTTP_ROOT" \
  "$TOOLKIT_ROOT"
```

清理完成后回到第 2 节，重新解压并恢复 `maas-offline-${RELEASE_VERSION}.tar.zst`。

## 10. 关键规则

- 同一 PXE 网段任一时刻只允许一套 DHCP/TFTP：`diskless_stage1` 或 `maas_provision`。
- 无盘抓配使用 `$PXE_INTERFACE`（例如 `eno4`）和 `$SERVER_IP`（例如 `10.0.0.10`）；不要误用管理网网卡。
- 切换到 MAAS PXE 前，必须停止无盘 DHCP/TFTP；使用脚本切换，不要手工同时启动服务。
- 8088 不监听时执行 `sudo "$TOOLKIT_ROOT/docs/install-web-console.sh"`，然后检查 `maas-web-console.service`。
- PostgreSQL socket 报错时检查 `sudo pg_lsclusters`；应存在且在线的 `14/main`、端口 `5432`。
