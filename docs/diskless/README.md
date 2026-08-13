# Stage1 无盘资源目录

该目录保存无盘采集模式的模板文件。实际生产资源需要放到控制机的：

```text
/srv/maas-offline/diskless/ubuntu-22.04/
```

默认约定文件：

- `vmlinuz`
- `initrd`
- `rootfs.squashfs`（可选兼容资源）
- `stage1.ipxe`
- `nocloud/meta-data`
- `nocloud/user-data`
- `nocloud/stage1-report.py`
- `/srv/maas-offline/iso/ubuntu-22.04*live-server-amd64.iso`

`stage1.ipxe` 会被一键脚本渲染到目标目录。内核参数会带上：

```text
boot=casper netboot=url url=http://<server-ip>:8083/iso/ubuntu-22.04.4-live-server-amd64.iso autoinstall
ds=nocloud-net;s=http://<server-ip>:8083/diskless/ubuntu-22.04/nocloud/
stage1.collector=http://<server-ip>:8091
```

节点侧执行器后续从该参数或固定配置里读取汇总器地址。

`docs/diskless-stage1-oneclick.sh` 会优先从 `/srv/maas-offline/iso/casper/` 自动补齐 `vmlinuz`、`initrd`、`rootfs.squashfs`，并使用 `/srv/maas-offline/iso/` 下的 Ubuntu 22.04 live-server ISO 作为 casper 网络启动介质。Stage1 通过 autoinstall early-command 执行采集脚本并关机，不进入交互式安装界面。启用 DHCP/TFTP 时会补齐 `ipxe.efi`、`undionly.kpxe`。
