# MAAS 端到端交付推进计划

## 1. 总目标

把当前脚本型离线交付工具推进成完整交付平台，覆盖：

- MAAS 服务端相关服务部署与检查
- 无盘采集相关服务部署与切换
- 节点接入、SN 认领、BMC 配置、PXE MAC 采集
- MAAS 纳管、清盘、套盘、部署
- 部署后测试和验收
- 批量操作、异常节点处理、观测统计
- Web 控制台统一操作和审计

短期采用分时独占：同一二层网段里，无盘 DHCP/TFTP 和 MAAS DHCP/TFTP 不同时对同一批节点生效。

## 2. 当前已具备能力

仓库已经具备：

- 离线 HTTP 资源服务归并和拉起
- MAAS 控制端离线安装、初始化、资源配置的一键入口
- 无盘 Stage1 采集服务的一键入口
- PXE 分时独占模式切换脚本
- MAAS boot-source 和 package repository 标准化配置说明
- CSV 批量纳管和打标签
- MAAS testing 清盘和 RAID 初始化脚本
- storage policy 批量套盘
- deploy policy 批量部署
- curtin 安装期登录注入
- install-safe 策略规避首启切网导致的部署异常

## 3. 本轮补齐的 Stage1 起点

新增 Stage1 汇总器和手动测试样例：

- 汇总器：[scripts/stage1_collector.py](./scripts/stage1_collector.py)
- 无盘服务一键入口：[diskless-stage1-oneclick.sh](./diskless-stage1-oneclick.sh)
- PXE 模式切换：[scripts/maas_pxe_mode.sh](./scripts/maas_pxe_mode.sh)
- 手动测试：[stage1/Stage1_Diskless_Manual_Test.md](./stage1/Stage1_Diskless_Manual_Test.md)
- 样例清单：[stage1/inventory.example.csv](./stage1/inventory.example.csv)
- 默认配置：[stage1/defaults.example.yaml](./stage1/defaults.example.yaml)
- 上报样例：[stage1/report.example.json](./stage1/report.example.json)

当前 Stage1 汇总器已经能完成：

- 加载 `inventory.csv`
- 加载 `defaults.yaml`
- 按 SN 查询节点目标配置
- 接收节点执行结果上报
- 校验 BMC IP、BMC 可达、BMC 账号、PXE MAC 格式和重复
- 导出 `maas.csv`
- 导出 `stage1-status.csv`
- 导出 `stage1-errors.csv`

当前还未实现：

- 节点侧自动执行器
- BMC/IPMI/Redfish 真实配置动作
- Web 控制台

## 4. 推荐实施阶段

### 阶段 A：Stage1 手动闭环

目标：先证明数据链路正确。

动作：

1. 准备真实 `inventory.csv`
2. 准备真实 `defaults.yaml`
3. 启动 Stage1 汇总器
4. 人工或临时脚本在节点上完成：
   - 读取 SN
   - 配置 BMC IP
   - 按 `nxdx` 到 `nxdx9` 策略确认可用账号
   - 识别用于 PXE 的第一张业务网卡 MAC
5. 把结果上报给汇总器
6. 导出 `maas.csv` 和异常表

验收：

- `maas.csv` 只包含通过验证的节点
- 异常节点全部出现在 `stage1-errors.csv`
- `stage1-status.csv` 能解释每台机器当前卡在哪一步

### 阶段 B：Stage1 节点执行器

目标：减少人工操作，形成无盘自动上报。

动作：

1. 节点启动无盘系统后自动读取 DMI SN
2. 按 SN 从汇总器拉取目标配置
3. 自动配置 BMC 网络
4. 自动按 `nxdx` 到 `nxdx9` 策略确认或创建管理员账号
5. 自动验证 BMC IP 和账号
6. 自动采集 PXE 业务口 MAC
7. 自动上报执行证据

验收：

- 单节点完整自动通过
- 故障节点能返回标准错误码
- 重复执行不会破坏已成功节点

### 阶段 C：分时独占服务控制

目标：把“无盘模式”和“MAAS 模式”的切换标准化。

动作：

1. 定义无盘相关服务清单
2. 定义 MAAS 相关服务清单
3. 使用 `maas_pxe_mode.sh status` 检查模式
4. 使用 `maas_pxe_mode.sh diskless_stage1` 切到无盘模式
5. 使用 `maas_pxe_mode.sh maas_provision` 切到 MAAS 模式
6. 使用 `maas_pxe_mode.sh maintenance_locked` 切到维护锁定模式

验收：

- 任意时刻能清楚知道当前模式
- 不允许两个 PXE 控制面同时对目标网段生效
- 切换失败有明确原因和日志摘要

### 阶段 D：端到端流程验证

目标：从 Stage1 到 MAAS deploy 完整跑通。

最短链路：

1. 切到无盘模式
2. 完成 Stage1 采集并导出 `maas.csv`
3. 切到 MAAS 模式
4. 批量导入 MAAS
5. 执行清盘/RAID 初始化
6. 套 storage policy
7. 安装 curtin 登录模板
8. 批量 deploy
9. 验收 SSH、主机名、SN、系统盘、25G 配置

验收：

- 单机端到端通过
- 小批量通过
- 批量失败能归因到标准错误码
- 失败节点能重试或隔离

### 阶段 E：Web 控制台 MVP

目标：把脚本能力封装成可视化操作台。

MVP 功能：

- 集群总览
- 节点列表
- 节点详情
- Stage1 状态和导出
- 服务模式切换
- 批量任务
- 异常中心
- 操作审计

技术建议见 [MAAS_Web_Console_Design.md](./MAAS_Web_Console_Design.md)。

## 5. 批量操作原则

- 所有批量操作必须先 dry-run
- 所有批量任务必须有任务 ID
- 每台节点必须有子任务状态
- 批量失败不应中断全部结果汇总
- 重试必须幂等
- 高风险节点必须支持隔离，隔离后不参与批量操作

## 6. 异常节点处理原则

异常节点不直接人工删改最终 CSV，而是进入异常表：

- `SN_NOT_FOUND`
- `SN_DUPLICATED`
- `BMC_IP_MISMATCH`
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
- `MAAS_DIRTY_RECORD`
- `TESTING_TIMEOUT`
- `STORAGE_APPLY_FAILED`
- `DEPLOY_FAILED`
- `CLOUD_INIT_FINAL_FAILED`

Web 控制台后续应按错误码聚合异常，给出下一步建议。

## 7. 下一步开发顺序

推荐继续按这个顺序推进：

1. 用真实清单验证 Stage1 汇总器
2. 增加节点侧最小执行器
3. 用 `diskless-stage1-oneclick.sh` 验证无盘服务部署
4. 用 `maas-control-plane-oneclick.sh` 验证 MAAS 控制端部署
5. 做单机端到端验证
6. 做小批量端到端验证
7. 起 Web 后端和数据库状态模型
8. 起 Web 前端 MVP
9. 把现有脚本逐步接入任务系统
