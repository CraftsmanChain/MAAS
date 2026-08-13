# MAAS Offline Delivery Toolkit

离线 MAAS 部署、纳管、清盘初始化、批量装机与交付链路的可复用 Runbook 与脚本集合。

## 快速入口

- Runbook: [docs/MAAS_Offline_Runbook.md](./docs/MAAS_Offline_Runbook.md)
- 端到端推进计划: [docs/MAAS_End_to_End_Execution_Plan.md](./docs/MAAS_End_to_End_Execution_Plan.md)
- Stage1 手动测试: [docs/stage1/Stage1_Diskless_Manual_Test.md](./docs/stage1/Stage1_Diskless_Manual_Test.md)
- Web 控制台设计草案: [docs/MAAS_Web_Console_Design.md](./docs/MAAS_Web_Console_Design.md)
- Web 控制台第一版: [web-console/README.md](./web-console/README.md)
- Docker 本地验证: [docs/docker/README.md](./docs/docker/README.md)

## 当前交付范围

- 已沉淀为可复用方案：
  - 离线资源服务一键归并与拉起
  - MAAS 控制端离线安装、初始化、资源配置一键入口
  - 无盘 Stage1 采集服务一键入口
  - 分时独占 PXE 模式切换脚本
  - 离线 Web 控制台第一版：总览、节点、服务、资源、PXE 模式 dry-run/受控操作
  - CSV 批量纳管与打标签
  - 基于统一策略 YAML 的 storage / deploy
  - `cloud-init final` 失败场景的 install-safe 兜底
  - 基于 curtin `late_commands` 的安装期登录注入模板
- 当前“一键离线部署”指的是：
  - 在 Ubuntu 22.04 控制机与离线资源已经准备好的前提下，用仓库脚本离线安装 MAAS 控制端、无盘 Stage1、boot/package repo、纳管、套盘、部署链路
  - MAAS 控制端入口：`docs/maas-control-plane-oneclick.sh`
  - 无盘 Stage1 入口：`docs/diskless-stage1-oneclick.sh`
  - PXE 模式切换入口：`docs/scripts/maas_pxe_mode.sh`
- 当前第一阶段规划：
  - 先通过 Stage1 汇总器手动验证 `SN -> BMC -> PXE MAC -> maas.csv`
  - 再补节点侧执行器和无盘服务模式控制
- 当前 Web 控制台规划：
  - 先按“分时独占”管理无盘 DHCP/TFTP 与 MAAS PXE 的切换
  - 再逐步接入节点状态、批量任务、异常中心、统计总览与操作审计
  - 切到 `diskless_stage1` 前会自动校验 Stage1 PXE/TFTP 资源，缺少 `ipxe.efi`、`stage1.ipxe`、TFTP 根目录或 collector/dnsmasq 配置时直接阻断，避免节点卡在 UEFI PXE 拉取 `ipxe.efi` 超时
- 当前不包含：
  - 从裸机开始安装 Ubuntu 控制机
  - 自动完成交换机、VLAN、路由和 BMC 数据采集

## 一键边界

- 当前可以做到：
  - Ubuntu 22.04 控制机上的 MAAS 控制端离线安装、PostgreSQL 初始化、MAAS schema 迁移、管理员创建、离线 boot-source / package repositories 配置
  - 无盘 Stage1 采集服务安装、可切换 DHCP/TFTP unit 安装、PXE 分时独占切换
  - MAAS 初始化完成后的纳管、套盘、部署
- 当前还做不到：
  - 从裸机开始，把 Ubuntu 控制机、交换机网络、VLAN、路由、BMC 数据采集全部自动完成
- 当前测试验证基线：
  - Ubuntu 22.04
  - MAAS `3.4.9`
  - `cloud-init 25.3`
  - `curtin-common 23.1.1`
  - `grub-efi-amd64 2.06-2ubuntu14.8`
  - 详细版本矩阵见 `docs/MAAS_Offline_Runbook.md` 的 `0.2`

## 推荐入口

- 离线控制端安装与一键边界：`docs/MAAS_Offline_Runbook.md` 的 `0.1`、`0.2`、`0.3`、`1.4.1`
- 全新离线环境资源清单与最短操作链：`docs/MAAS_Offline_Runbook.md` 的 `1.5`、`1.6`
- 端到端推进计划：`docs/MAAS_End_to_End_Execution_Plan.md`
- Stage1 手动测试：`docs/stage1/Stage1_Diskless_Manual_Test.md`
  - Web 控制台与分时独占设计：`docs/MAAS_Web_Console_Design.md`
  - Web 控制台本地启动：`python3 web-console/server.py`
- Docker / dry-run 验证：`docs/docker/README.md`
- 单机最佳实践：`docs/MAAS_Offline_Runbook.md` 的 `4.4.3`
- 批量最佳实践：`docs/MAAS_Offline_Runbook.md` 的 `4.4.4`
- 问题收口与复用方案清单：`docs/MAAS_Offline_Runbook.md` 的 `4.4.6`、`5.2`、`5.4`
