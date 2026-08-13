# Ubuntu 22.04.3 / kernel 5.15.0-78 MAAS image

This procedure deliberately uses Canonical's `packer-maas` Ubuntu flat-image template. Uploading the Ubuntu ISO itself is not sufficient: MAAS needs a deployable image with cloud-init and curtin hooks.

## 1. Download and verify the exact ISO

Ubuntu stores the 22.04.3 amd64 server ISO in the `22.04.4` old-release directory.

```bash
mkdir -p ~/maas-image-build/iso
cd ~/maas-image-build/iso

wget -c https://old-releases.ubuntu.com/releases/22.04.4/ubuntu-22.04.3-live-server-amd64.iso
printf '%s  %s\n' \
  a4acfda10b18da50e2ec50ccaf860d7f20b389df8765611142305c0e911d16fd \
  ubuntu-22.04.3-live-server-amd64.iso | sha256sum -c -
```

## 2. Prepare packer-maas

Run this on an amd64 Ubuntu host with KVM enabled.

```bash
sudo apt-get update
sudo apt-get install -y \
  git make wget qemu-utils qemu-system-x86 ovmf cloud-image-utils \
  libnbd-bin nbdkit fuse2fs parted

cd ~/maas-image-build
git clone https://github.com/canonical/packer-maas.git
cd packer-maas/ubuntu
packer version
```

Install Packer 1.11 or newer before continuing if `packer version` is unavailable or too old.

## 3. Prepare exact kernel packages

Use a Jammy apt environment and place all five packages in the template's `packages` directory. The ABI-specific package names prevent a newer generic kernel from being selected accidentally.

```bash
cd ~/maas-image-build/packer-maas/ubuntu/packages
apt-get download \
  linux-image-5.15.0-78-generic \
  linux-modules-5.15.0-78-generic \
  linux-modules-extra-5.15.0-78-generic \
  linux-headers-5.15.0-78 \
  linux-headers-5.15.0-78-generic

dpkg-deb -f linux-image-5.15.0-78-generic_*_amd64.deb Package Version
```

Expected kernel package version is `5.15.0-78.85`. If the configured apt mirror no longer indexes these ABI packages, download the same five Jammy packages from an approved Ubuntu archive/snapshot and put the `.deb` files in this directory.

## 4. Pin the kernel in the curtin image

Copy the supplied helper into the Packer template and add it to the flat template's provisioning scripts.

```bash
cd ~/maas-image-build/packer-maas/ubuntu
cp /path/to/MAAS/docs/image-build/pin-kernel-5.15.0-78.sh scripts/
chmod 0755 scripts/pin-kernel-5.15.0-78.sh
```

In `ubuntu-flat.pkr.hcl`, add this entry after `scripts/curtin.sh` in the shell provisioner's `scripts` list:

```hcl
"${path.root}/scripts/pin-kernel-5.15.0-78.sh",
```

The generated `/curtin/CUSTOM_KERNEL` prevents MAAS from installing a different kernel during deployment. The five `.deb` files are installed by packer-maas's `install-custom-packages` curtin hook.

## 5. Serve the exact ISO and build

The current Makefile discovers an ISO from `URL/SERIES/SHA256SUMS`. Create a minimal local tree so it selects 22.04.3 exactly.

Terminal 1:

```bash
mkdir -p ~/maas-image-build/iso-mirror/22.04.3
cp ~/maas-image-build/iso/ubuntu-22.04.3-live-server-amd64.iso \
  ~/maas-image-build/iso-mirror/22.04.3/
printf '%s *%s\n' \
  a4acfda10b18da50e2ec50ccaf860d7f20b389df8765611142305c0e911d16fd \
  ubuntu-22.04.3-live-server-amd64.iso \
  > ~/maas-image-build/iso-mirror/22.04.3/SHA256SUMS
python3 -m http.server 18080 --directory ~/maas-image-build/iso-mirror
```

Terminal 2:

```bash
cd ~/maas-image-build/packer-maas/ubuntu
make custom-ubuntu.tar.gz \
  SERIES=22.04.3 \
  ARCH=amd64 \
  URL=http://127.0.0.1:18080 \
  TIMEOUT=2h
sha256sum custom-ubuntu.tar.gz
```

## 6. Upload and select the image

Run on the MAAS controller after logging in with the configured CLI profile:

```bash
maas admin boot-resources create \
  name='custom/ubuntu-22043-k515-78' \
  title='Ubuntu 22.04.3 kernel 5.15.0-78' \
  architecture='amd64/generic' \
  filetype='tgz' \
  base_image='ubuntu/jammy' \
  content@=custom-ubuntu.tar.gz

maas admin boot-resources read | jq '.[] | {name, title, architecture, status}'
```

In the web console configuration set:

```text
MAAS OS type: custom
Deployment image identifier: ubuntu-22043-k515-78
```

Deploy one test node first. After SSH passes, use a project Ansible check bundle to verify `lsb_release -ds`, `uname -r`, partitions, and any project software. Those checks remain node-acceptance evidence and are not generic platform gates. A successful non-check-mode Ansible run moves the node to the final `Node acceptance / Ready` stage.
