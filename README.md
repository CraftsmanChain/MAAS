# MAAS Offline Delivery Toolkit

离线 MAAS 部署、纳管、清盘初始化、批量装机与交付链路的可复用 Runbook 与脚本集合。

## 快速入口

- Runbook: [docs/MAAS_Offline_Runbook.md](./docs/MAAS_Offline_Runbook.md)

## 当前交付范围

- 已沉淀为可复用方案：
  - 离线资源服务一键归并与拉起
  - CSV 批量纳管与打标签
  - 基于统一策略 YAML 的 storage / deploy
  - `cloud-init final` 失败场景的 install-safe 兜底
  - 基于 curtin `late_commands` 的安装期登录注入模板
- 当前“一键离线部署”指的是：
  - 在 MAAS 主机与离线资源已经准备好的前提下，用仓库脚本把离线服务、boot/package repo、纳管、套盘、部署标准化落地
- 当前不包含：
  - 从裸机开始安装 Ubuntu
  - 从零安装和初始化 MAAS 软件包
  - 自动完成交换机、VLAN、DHCP、路由和 BMC 数据采集

## 一键边界

- 当前可以做到：
  - MAAS 控制端初始化完成后的条件式一键离线交付
  - 包括离线资源服务、boot-source、package repositories、纳管、套盘、部署
- 当前还做不到：
  - 从裸机开始，把 Ubuntu 控制机、MAAS region/rack、数据库、网络全部一键拉起
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
- 单机最佳实践：`docs/MAAS_Offline_Runbook.md` 的 `4.4.3`
- 批量最佳实践：`docs/MAAS_Offline_Runbook.md` 的 `4.4.4`
- 问题收口与复用方案清单：`docs/MAAS_Offline_Runbook.md` 的 `4.4.6`、`5.2`、`5.4`
