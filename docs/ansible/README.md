# Ansible bundle format

Upload a `.tar.gz` or `.tgz` whose root contains `manifest.yaml` and the playbook named by `playbook`.

```text
cluster-baseline/
  manifest.yaml
  site.yml
  roles/
  files/
```

`checks` are optional project-specific SSH checks. Supported operators are `equals`, `contains`, `regex`, and `min_bytes`. The platform does not hardcode OS, kernel, partition, GPU, OFED, or application requirements: put those assertions in the uploaded bundle and review them during node acceptance.

The generic gate only checks the target business IP, TCP/22, and SSH login using the account in `docs/cloud-init/default-user-data.yaml`. Ansible and project checks default to every deployed node that passed this gate; a UI selection narrows the target set. A successful non-check-mode Ansible run moves the node to the final `Node acceptance / Ready` stage. `--check` runs never mark configuration complete.

Bundles are extracted under `MAAS-sources/ansible/bundles`; job state remains under the project `.tmp/automation-jobs` directory.
