# Ansible NRPE client management

This workflow configures Debian/Ubuntu and RHEL-family clients from the Nagios server. It installs NRPE and plugins on clients, registers their discovered services in Nagios, validates Nagios configuration before reload, and checks NRPE connectivity.

## Prerequisites

- TBCOps/Nagios has been installed on Ubuntu 22.04 or newer using `make up`.
- Ansible controller access is from the Nagios server. Provisioning commands that edit Nagios files should be run as root (`sudo make ...`).
- Clients use Ubuntu/Debian or RHEL/Rocky/Alma, have SSH and `systemd`, and the SSH user can use sudo.
- Set `NAGIOS_SERVER_IP` in `.env` to the address clients will use to reach the Nagios server. Ensure network ACLs allow client TCP/5666 from this address.
- Keep the client inventory free of passwords. SSH passwords and sudo passwords are prompt-only.

Install the pinned Ansible runtime and collections:

```bash
sudo make install-ansible
```

`ansible-core`, `ansible-lint`, `yamllint`, `ansible.posix`, and `community.general` versions are pinned by the installer and [ansible/requirements.yml](../ansible/requirements.yml).

## Add a client and distribute the key

Add a DNS name or IP (IPv4/IPv6) and a safe inventory name:

```bash
sudo make add-client IP=10.0.0.5 NAME=web01 USER=ubuntu
```

The command writes `ansible/inventory/hosts.ini`, rejects duplicate names/addresses, and never writes passwords. Set defaults in `.env` with `ANSIBLE_USER`, `ANSIBLE_SSH_KEY_PATH`, and `ANSIBLE_INVENTORY`; the key defaults to `~/.ssh/tbcops_ansible`.

Generate or secure the controller key and authorize it on clients:

```bash
sudo make ansible-ssh-key
sudo make ansible-copy-key
```

The key is Ed25519 with mode `0600`. On first use, `ansible-copy-key` prompts for the SSH login password and sudo/become password; they are not saved. It installs the public key using `ansible.posix.authorized_key` with `no_log`. CI skips prompts and requires passwordless access. SSH host-key checking stays enabled; the workflow pre-populates `known_hosts` using `ssh-keyscan`. For sensitive networks, independently verify each host-key fingerprint with the server console or a trusted inventory before proceeding.

Check SSH access:

```bash
sudo make ansible-ping
```

## Configure NRPE and register clients

Run the complete workflow:

```bash
sudo make ansible-nrpe
```

To operate on one inventory alias, use `LIMIT=web01`. For an Ansible dry run, use:

```bash
sudo make ansible-nrpe LIMIT=web01 CHECK=1
```

The playbook installs NRPE, configures `allowed_hosts` to localhost and `NAGIOS_SERVER_IP`, keeps command arguments disabled, applies a firewall rule only when UFW/firewalld is already active, detects installed packages and systemd services, and generates a Nagios host/service definition plus YAML inventory report. It does not enable an inactive firewall. `NRPE_CLIENT_SERVICES` provides additional explicit service units to check; the mapping in `ansible/group_vars/all.yml` controls package/service autodetection.

`NRPE_PORT` defaults to 5666. `NRPE_VERSION` pins the `check_nrpe` source version built on the Nagios server when the plugin is missing; optionally set `NAGIOS_NRPE_PLUGIN_SHA256` to enforce archive integrity. Client NRPE packages come from their operating-system repositories.

The per-client configuration is written to `/usr/local/nagios/etc/servers/clients/<name>.cfg`; package/service summary is `/usr/local/nagios/etc/servers/inventory/<name>.yml`. Nagios config is validated with `nagios -v` before a reload. Invalid host configuration is rolled back and provisioning fails.

Verify every inventory client from the Nagios server:

```bash
sudo make ansible-verify
```

The command runs `/usr/local/nagios/libexec/check_nrpe -H <address> -p <port> -c check_load` and reports a PASS/FAIL table. `make ansible-nrpe` also performs a delegated end-to-end check outside check mode. Summary output includes host, OS, NRPE version, detected services, and registered checks.

## Extend service detection or add a custom check

Add an entry under `nrpe_service_mapping` in `ansible/group_vars/all.yml` with package names, systemd service keys, a stable NRPE command name, and a service unit. Add a corresponding `check_service_<name>` to the registered service checks if the service needs monitoring. Service checks invoke the fixed local `check_systemd_service` wrapper, so NRPE command arguments remain disabled.

For a custom non-parameterized check, add a fixed mapping under `nrpe_custom_checks` in `ansible/group_vars/all.yml`, for example `check_example: /usr/lib/nagios/plugins/check_example -w 2 -c 5`. It is rendered as an NRPE command and registered with Nagios automatically. Do not enable NRPE command arguments for untrusted clients.

## Remove a client

Remove by inventory alias:

```bash
sudo make remove-client NAME=web01
```

The command removes the inventory entry, host config, and report; validates Nagios; and reloads only after validation succeeds. If validation fails, it restores the removed inventory/config/report. The optional `nagios_prune_removed_hosts` role setting defaults to `false`.

## Lint and syntax checks

```bash
make ansible-syntax
make ansible-lint
make yamllint
python3 -m py_compile scripts/manage.py
python3 -m unittest discover -s tests -v
```

## Troubleshooting

- **SSH unreachable / host-key error:** confirm TCP/22 and the inventory alias/address/user. Keep `host_key_checking` enabled; verify that the `ssh-keyscan` fingerprint matches the client before trusting it.
- **NRPE connection timeout:** confirm TCP/5666 from Nagios to client, the route/security-group ACL, and that UFW/firewalld permits only the Nagios source address. Check `systemctl status nagios-nrpe-server` (Debian) or `nrpe` (RHEL) and the daemon journal on the client.
- **NRPE rejects the Nagios server:** inspect the rendered `allowed_hosts` line in `/etc/nagios/nrpe.cfg` or `/etc/nrpe.cfg`; set `NAGIOS_SERVER_IP` to the source address seen by the client, then rerun the playbook.
- **`CHECK_NRPE: Socket timeout`:** verify the service is listening on the configured port (`ss -lntp | grep 5666`), check routes/firewalls, and run `make ansible-verify` again.
- **SSL/handshake errors or version mismatch:** make sure both sides use compatible NRPE releases and that the server `check_nrpe` plugin was built for the deployed client version. Set the intended source `NRPE_VERSION`, then rerun `make ansible-nrpe`.
- **Nagios validation failure:** run `sudo /usr/local/nagios/bin/nagios -v /usr/local/nagios/etc/nagios.cfg` and inspect the reported file/line. The generated host file is restored on validation errors.
- **No change on a second run:** package/service facts and generated files should converge. Investigate any repeated changes in `ansible-playbook` output; do not suppress them with `changed_when` until the underlying source of drift is fixed.
