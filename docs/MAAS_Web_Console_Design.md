# MAAS 交付控制台设计草案

## 1. 定位

该控制台定位为 GPU 服务器离线交付的统一操作面，不替代 MAAS，而是在 MAAS 之上补齐这些能力：

- 阶段 1 无盘采集、BMC 配置、PXE MAC 采集、异常收口
- 无盘服务与 MAAS PXE 服务的分时独占切换
- MAAS 批量纳管、清盘、套盘、部署的编排入口
- 集群级节点状态、统计、问题原因、操作审计
- 面向现场交付的批量操作、回滚、重试、证据链查看

设计参考 JumpServer 这类成熟运维平台的思路：资产统一建模、账号/权限独立管理、工作台式操作入口、审计可追溯、任务有状态、组件边界清晰。JumpServer 本身是面向特权访问管理的平台，提供 Web 入口、资产访问、认证授权、账号管理和审计能力；本项目借鉴的是这些产品结构，而不是照搬其业务模型。

## 2. 当前采用的 PXE 控制策略

短期采用“分时独占”：

- 同一个二层广播域里，同一批 PXE 客户端只允许一个 DHCP/PXE 决策入口生效
- 需要阶段 1 无盘采集时，启用无盘 DHCP/TFTP/HTTP，停用 MAAS 对目标网段的 DHCP/PXE
- 阶段 1 完成后，停用无盘 DHCP/TFTP，启用 MAAS 对目标网段的 DHCP/PXE
- 已部署机器应保持本地盘优先启动，除非人为设置一次性 PXE Boot

不建议在同一 VLAN 里同时打开无盘 DHCP/PXE 和 MAAS DHCP/PXE。这样会导致节点随机进入不同启动链路，并产生难以复现的脏状态。

中长期如果需要 100 台混合状态并行推进，推荐升级为分 VLAN：

- `stage1` VLAN：无盘采集
- `maas-provision` VLAN：MAAS 纳管和部署
- `prod` VLAN：已部署业务网络

如果现场不能分 VLAN，又必须同网段并行，则需要引入单一 DHCP 控制器按 MAC 白名单分流。该模式复杂度高，暂不作为当前主方案。

## 3. 服务模式模型

控制台需要把 PXE 控制面抽象成“服务模式”，而不是让操作人员直接登录机器启停服务。

### 3.1 服务模式

- `diskless_stage1`
  - 无盘采集模式
  - 启用无盘 DHCP/TFTP/HTTP
  - 禁止 MAAS PXE 对目标网段生效
  - 允许节点执行 SN 认领、BMC 配置、PXE MAC 上报

- `maas_provision`
  - MAAS 装机模式
  - 停用无盘 DHCP/TFTP
  - 启用 MAAS DHCP/TFTP/PXE
  - 允许纳管、清盘、套盘、部署

- `maintenance_locked`
  - 维护锁定模式
  - 两边 PXE 都不对目标节点提供引导
  - 用于排查 DHCP 冲突、交换机故障、批量任务异常

### 3.2 切换保护

服务模式切换必须做保护：

- 切换前显示当前模式、目标模式、影响的网段、影响的节点数
- 切换前检查是否有正在运行的批量任务
- 切换前检查是否存在正在无盘启动或正在 MAAS deploy 的节点
- 切换动作必须落审计日志
- 切换失败时必须显示失败服务、失败命令、最近日志摘要

## 4. 节点状态模型

节点以 SN 作为阶段 1 主键，以 MAAS `system_id` 作为阶段 2 之后的 MAAS 主键。控制台内部应同时保存两者，并用 `pxe_mac` 做强校验。

推荐状态：

- `inventory_pending`：清单已导入，节点未上报
- `stage1_claimed`：节点通过 SN 认领了任务
- `bmc_configuring`：正在配置 BMC
- `bmc_verified`：BMC IP 和账号已验证
- `mac_reported`：PXE MAC 已上报
- `stage1_ready`：可导出到 `maas.csv`
- `maas_imported`：已导入 MAAS
- `ready`：MAAS 中处于 Ready
- `testing`：正在清盘/RAID 初始化
- `storage_applied`：已套用存储策略
- `deploying`：正在部署系统
- `deployed`：部署完成
- `repairing`：维修或返修中
- `failed`：存在异常，等待处理
- `quarantined`：高风险隔离，不允许批量操作

每个状态都应记录：

- 最近一次状态变更时间
- 最近一次操作人或自动任务
- 最近一次任务 ID
- 失败原因和标准错误码
- 可执行的下一步动作

## 5. Web 页面结构

### 5.1 总览页

用于看整个集群的实时状态：

- 总节点数
- 清单待处理数
- 阶段 1 就绪数
- MAAS Ready 数
- 部署中数量
- 已部署数量
- 异常节点数
- 维修节点数
- 当前服务模式
- 最近批量任务
- Top 失败原因

交互要求：

- 点击任何统计卡片都进入对应筛选后的节点列表
- 异常原因按数量排序
- 显示当前是否允许发起无盘采集或 MAAS 部署
- 显示关键服务健康状态：DHCP、TFTP、HTTP、MAAS API、MAAS rackd/regiond

### 5.2 节点列表

核心工作台，面向批量筛选和操作：

- 支持按状态、tag、机型、机柜、BMC 网段、错误码筛选
- 支持按 SN、hostname、BMC IP、PXE MAC、MAAS system_id 搜索
- 支持列配置和保存视图
- 支持多选后批量操作
- 每行展示当前阶段、下一步建议、最近错误摘要

批量操作：

- 导入清单
- 标记维修
- 进入阶段 1 队列
- 导出 `maas.csv`
- 导入 MAAS
- 执行清盘
- 套用存储策略
- 部署系统
- 重试失败任务
- 隔离节点

### 5.3 节点详情页

用于排障和证据链查看：

- 基础信息：SN、hostname、BMC IP、PXE MAC、tag、机型、机柜
- 阶段状态时间线
- BMC 配置前后信息
- BMC 账号探测结果：`nxdx` 到 `nxdx9`
- PXE 网卡识别依据：接口名、MAC、PCI bus
- MAAS 信息：system_id、status、power_state、boot_interface
- 存储策略命中结果
- deploy 策略命中结果
- 最近任务日志
- 标准错误码和处理建议

### 5.4 服务模式页

专门管理分时独占：

- 当前模式：无盘采集 / MAAS 装机 / 维护锁定
- 模式切换按钮
- 目标网段和 DHCP 状态
- 无盘服务状态
- MAAS 服务状态
- 最近一次切换记录
- 切换前检查结果

该页面必须做强确认，避免误切服务影响现场部署。

### 5.5 批量任务页

所有批量操作都进入任务系统：

- 任务类型
- 任务发起人
- 目标节点数
- 成功数、失败数、跳过数
- 当前进度
- 每台机器的子任务状态
- 可重试项
- 日志下载

任务必须支持幂等重试。重复执行不应破坏已经成功的节点。

### 5.6 异常中心

把排障从“看日志”升级为“看原因”：

- 按错误码聚合
- 按阶段聚合
- 按机型/批次/交换机端口聚合
- 显示最近新增异常
- 显示可自动修复和需人工处理的分类

常见错误码：

- `SN_NOT_FOUND`
- `SN_DUPLICATED`
- `BMC_UNREACHABLE`
- `BMC_AUTH_FAILED`
- `BMC_USER_READBACK_FAILED`
- `BMC_ACCESS_READBACK_FAILED`
- `BMC_NETWORK_READBACK_FAILED`
- `BMC_REMOTE_IPMI_FAILED`
- `BMC_IPMI_LAN_DISABLED`
- `BMC_IPMI_LAN_UNAVAILABLE`
- `BMC_USER_EXHAUSTED`
- `PXE_MAC_NOT_FOUND`
- `PXE_MAC_DUPLICATED`
- `DHCP_CONFLICT`
- `MAAS_IMPORT_FAILED`
- `MAAS_DIRTY_RECORD`
- `TESTING_TIMEOUT`
- `STORAGE_APPLY_FAILED`
- `DEPLOY_FAILED`
- `CLOUD_INIT_FINAL_FAILED`

### 5.7 设置页

- MAAS API profile
- 无盘服务配置
- 目标网段配置
- BMC 默认账号策略
- `nxdx` 到 `nxdx9` 用户策略
- 25G 默认配置
- storage/deploy policy 管理
- 操作权限与审计配置

## 6. 后端设计

推荐后端技术栈：

- Python `FastAPI`
- PostgreSQL
- Redis
- Celery 或 RQ 作为任务队列
- WebSocket 或 Server-Sent Events 推送任务进度
- SQLAlchemy 或 SQLModel 管理数据模型
- MAAS CLI/API 适配层
- 无盘阶段 agent API

核心后端模块：

- `inventory`：清单导入、校验、去重
- `stage1`：无盘采集、BMC 配置结果接收、状态机推进
- `pxe_mode`：分时独占服务模式控制
- `maas_adapter`：封装 MAAS 纳管、tag、storage、deploy
- `job_runner`：批量任务、子任务、重试
- `diagnostics`：错误码、日志摘要、原因归因
- `audit`：操作审计
- `metrics`：统计数据和时间序列

## 7. 前端设计

推荐前端技术栈：

- Vue 3 + TypeScript 或 React + TypeScript
- Vite
- TanStack Query 或同类数据请求状态管理
- ECharts 用于统计图表
- WebSocket/SSE 用于任务进度
- 表格使用支持虚拟滚动、列配置、批量选择的组件

界面风格建议：

- 以“运维工作台”为核心，不做营销式首页
- 左侧主导航：总览、节点、批量任务、异常中心、服务模式、策略、审计、设置
- 内容区保持高信息密度，适合现场快速扫描
- 状态颜色要克制：成功、进行中、失败、隔离、维修五类即可
- 批量危险操作必须二次确认，并展示影响范围
- 所有任务都能点进明细，而不是只弹一个成功/失败提示

## 8. MVP 范围

第一版不要直接追求大而全，建议先做这些：

1. 清单导入与校验
2. 节点状态总览
3. 节点列表和详情
4. 阶段 1 结果接收和 `maas.csv` 导出
5. 分时独占服务模式页
6. 调用现有脚本完成 MAAS 导入、套盘、部署
7. 批量任务进度和日志
8. 异常码聚合

暂不做：

- 多租户
- 复杂 RBAC
- 同 VLAN MAC 分流 DHCP
- 自动交换机端口改 VLAN
- Web 终端

## 9. 与现有仓库的集成方式

现有脚本继续作为后端执行器：

- `docs/maas-offline-oneclick.sh`
- `docs/maas-control-plane-oneclick.sh`
- `docs/diskless-stage1-oneclick.sh`
- `docs/scripts/maas_pxe_mode.sh`
- `docs/scripts/stage1_collector.py`
- `docs/scripts/maas_bulk_import_and_tag.sh`
- `docs/scripts/maas_apply_storage_policy.py`
- `docs/scripts/maas_deploy_batch.sh`
- `docs/scripts/maas_install_curtin_login_template.py`
- `scripts/wipe-raid-and-disks-test.sh`

Web 后端负责：

- 生成输入文件
- 调用脚本
- 收集 stdout/stderr
- 解析结果并更新数据库状态
- 把失败转成标准错误码
- 在页面上展示下一步建议

## 10. 实施顺序

推荐顺序：

1. 先把阶段 1 服务端汇总器和节点执行器补齐
2. 增加 Web 后端数据库和状态模型
3. 接入清单导入、状态总览、节点列表
4. 接入分时独占服务模式控制
5. 接入现有 MAAS 批量脚本
6. 增加任务系统和异常中心
7. 再做权限、审计、策略管理

这样能先把现场最痛的“看不清状态、批量操作不可观测、失败原因难聚合”解决掉，再逐步补齐平台化能力。
