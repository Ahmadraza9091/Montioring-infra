# Automated TBCOps + Nagios Deployment

This project deploys TBCOps with PostgreSQL in Docker and Nagios Core natively on Ubuntu 22.04 or newer. Nginx owns public ports 80/443 and proxies TBCOps and Nagios; Apache listens only on `127.0.0.1:8080`.

## Architecture

- TBCOps container: `127.0.0.1:<APP_PORT>` (3001 by default)
- PostgreSQL container: `127.0.0.1:<POSTGRES_PORT>` (5432 by default)
- Nagios Apache/CGI: `127.0.0.1:<NAGIOS_HTTP_PORT>` (8080 by default)
- Nginx: public ports 80/443
- Backups: `BACKUP_DIR/<UTC timestamp>/`

PostgreSQL data is stored in the `opsknight_postgres_data` Docker volume.

## Project files: what each one does

- `.env.example` is the configuration template. Copy it to `.env`; put server-specific domains and secrets in `.env`, never in the template.
- `.env` is read by the Python runner and Docker Compose. It contains passwords and application keys, is restricted to owner access, and is excluded from Git.
- `Makefile` provides short commands such as `make up`, `make health`, and `make backup`. Each target calls the Python deployment runner.
- `scripts/manage.py` is the Python orchestration entry point. It validates `.env`, installs Ubuntu packages and Docker, builds Nagios, writes Nginx/Apache configuration, manages containers, runs health checks, and handles backup/restore.
- `docker-compose.yml` runs TBCOps and PostgreSQL. Both container ports are bound to `127.0.0.1`; Nginx is the public HTTP/HTTPS entry point. PostgreSQL data persists in a named Docker volume.
- `templates/opsknight.nginx.conf.template` and `templates/nagios.nginx.conf.template` are rendered into Nginx sites for the app and Nagios.
- `templates/nagios-apache.conf.template` configures Apache's private Nagios CGI endpoint.
- `configs/nagios/local.cfg` is the example Nagios host/service monitoring configuration.
- `tests/test_manage.py` checks Python configuration, Ubuntu version gating, and private HTTP Nginx rendering. Run it with `python3 -m unittest discover -s tests -v`.
- `backups/` contains only a placeholder in the repository. Real backups are written to `BACKUP_DIR` from `.env` (default: `/opt/backups/opsknight-monitoring`).
- `.gitignore` keeps `.env`, backups, build scratch, and Python bytecode out of version control.
- `readme.md` is this full deployment reference. `readme-fresh-server.md` gives the fresh-server, private testing, and ngrok walkthrough.

The `opsknight-*` Compose, volume, file, and `OPSKNIGHT_*` environment names are retained as technical compatibility identifiers. Keep those names unchanged even though the product branding in the documentation is TBCOps.

## Prerequisites

- Ubuntu 22.04 or newer, on a release still supported by Ubuntu and available in Docker's package repository. The installer uses the release codename for Docker's apt repository.
- A sudo-capable account and SSH access. Keep a second SSH session open while setup changes firewall rules.
- Working internet access to Ubuntu, Docker, Nagios, GitHub, and the configured container image registry.
- For production HTTPS only: two DNS A records pointing to this server and a monitored certificate email.
- For private testing: no public DNS is required; use `.test` hostnames and resolve them from the client computer.
- For Proxmox: a VM is the simplest Docker host. Docker inside an LXC may require nesting/keyctl and suitable container permissions; ensure the CT has a reachable bridged/LAN IP.

Ports 22, 80, and 443 must be reachable as appropriate for your network. Docker group membership is not enabled because it grants root-equivalent privileges. Do not expose the HTTP-only test setup to the public internet.

## Configuration

Create a private `.env` from the template:

```bash
cp .env.example .env
chmod 600 .env
```

Generate strong random secrets immediately after copying the template. The commands below replace the `CHANGE_ME` values with random hexadecimal values:

```bash
sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(openssl rand -hex 32)|" .env
sed -i "s|^API_KEY_SECRET=.*|API_KEY_SECRET=$(openssl rand -hex 32)|" .env
sed -i "s|^NEXTAUTH_SECRET=.*|NEXTAUTH_SECRET=$(openssl rand -hex 32)|" .env
sed -i "s|^ENCRYPTION_KEY=.*|ENCRYPTION_KEY=$(openssl rand -hex 32)|" .env
sed -i "s|^NAGIOS_ADMIN_PASSWORD=.*|NAGIOS_ADMIN_PASSWORD=$(openssl rand -hex 32)|" .env
sed -i "s|^PROMETHEUS_SCRAPE_TOKEN=.*|PROMETHEUS_SCRAPE_TOKEN=$(openssl rand -hex 32)|" .env
```

`PROMETHEUS_SCRAPE_TOKEN` is optional; generate it only if you use that integration. These are one-time Linux shell commands for preparing `.env`; deployment and management scripts themselves are Python. The runner also generates its required secrets automatically if `.env` does not exist, but it does not overwrite or repair an existing `.env`.

Now edit the hostnames and URLs:

```bash
nano .env
```

Choose **one** configuration mode:

**Private/LAN testing without a domain** (HTTP only):

```dotenv
OPSKNIGHT_DOMAIN=tbcops.test
NAGIOS_DOMAIN=nagios.test
OPSKNIGHT_PUBLIC_URL=http://tbcops.test
NEXTAUTH_URL=http://tbcops.test
NGINX_ENABLE_SSL=false
```

**Production with your own DNS names** (HTTPS):

```dotenv
OPSKNIGHT_DOMAIN=tbcops.yourdomain.net
NAGIOS_DOMAIN=nagios.yourdomain.net
OPSKNIGHT_PUBLIC_URL=https://tbcops.yourdomain.net
NEXTAUTH_URL=https://tbcops.yourdomain.net
NGINX_ENABLE_SSL=true
CERTBOT_EMAIL=you@example.net
ADMIN_EMAIL=you@example.net
```

Replace `yourdomain.net` and the email placeholders with real values. Point both A records to the server before running `make up`; the SSL step checks DNS and requests both certificates. For private testing, keep the `.test` names distinct and do not request certificates.

Other useful `.env` settings:

- `OPSKNIGHT_IMAGE`: application image/tag to deploy.
- `POSTGRES_USER`, `POSTGRES_DB`, `POSTGRES_PORT`, `APP_PORT`: database identity and loopback ports. Do not change database identity after creating a database without a migration plan.
- `NAGIOS_VERSION`, `NAGIOS_PLUGINS_VERSION`: versions downloaded and compiled during installation. SHA-256 fields are optional; setting them enables independent checksum verification.
- `NAGIOS_ADMIN_USER`, `NAGIOS_ADMIN_PASSWORD`: Nagios web login.
- `FIREWALL_ENABLE` and `ALLOW_*_PORT`: whether the installer configures UFW and which ports it allows. Also check Proxmox/network firewall rules.
- `BACKUP_DIR`, `BACKUP_RETENTION_DAYS`: backup location and automatic retention period.
- `PROMETHEUS_SCRAPE_TOKEN`: optional; leave blank if that integration is unused.

Do not post or commit `.env`, and do not reuse the sample `CHANGE_ME` values. If an `ENCRYPTION_KEY` has already protected stored data, do not rotate it unless the application supports re-encryption. Changing `POSTGRES_PASSWORD` after PostgreSQL initialized also requires changing the password inside PostgreSQL; changing `.env` alone does not rotate an existing database password.

For compatibility, keep the existing `OPSKNIGHT_*` environment variable names and Compose identifiers such as `opsknight-app` unchanged; TBCOps is the product branding.

For detailed hosts-file and Proxmox networking instructions for private testing, see [readme-fresh-server.md](./readme-fresh-server.md).

## DNS

Create two A records before SSL setup:

```text
tbcops.example.com     -> server public IPv4
nagios.example.com     -> server public IPv4
```

The SSL configuration step stops with a clear error if either record is missing or resolves elsewhere.

## Fresh installation

### From a new Ubuntu server

Connect over SSH and install the small bootstrap tools needed to clone and run this project:

```bash
sudo apt-get update
sudo apt-get install -y git make python3 openssl
git clone <YOUR_REPOSITORY_URL> tbcops-monitoring
cd tbcops-monitoring
```

Replace `<YOUR_REPOSITORY_URL>` with your repository's clone URL. Follow the [Configuration](#configuration) section to create `.env`, generate secrets, and choose private-test or production settings. Do not copy real secrets into this README or share them in terminal screenshots.

The server automation is Python and uses only the standard library; no `pip install` is needed. Validate the settings, then run the full installation:

```bash
sudo make check
sudo make up
```

If `make check` created `.env` automatically, it generated secrets but still requires you to set the correct hostnames/URLs before it will pass validation.

### What `make up` does

`make up` performs these stages in order and stops with the failed command if a stage errors:

1. Validates Ubuntu, `.env`, and available prerequisites.
2. Installs build tools, Apache, Nginx, Certbot, and firewall rules.
3. Installs Docker Engine and Compose.
4. Downloads and compiles Nagios Core and plugins, configures its CGI authentication, and validates Nagios/Apache.
5. Renders Nginx sites and validates the Nginx configuration.
6. If SSL is enabled, verifies DNS, obtains certificates, tests renewal, and enables HTTPS. If SSL is disabled, it skips Certbot.
7. Pulls and starts TBCOps and PostgreSQL, then waits for their health checks.

The first install takes several minutes while Nagios is compiled. `make setup` is a backward-compatible alias for `make up`. If a step fails, correct the reported cause and run the relevant `make` target rather than repeating completed installation stages unnecessarily.

After setup, verify the services:

```bash
sudo make status
sudo make health
```

For HTTP-only private testing, add the hostnames to the client computer's hosts file, pointing them to the server's private IP:

```text
<SERVER_LAN_IP> tbcops.test nagios.test
```

On Windows edit `C:\Windows\System32\drivers\etc\hosts` as Administrator; Linux/macOS use `/etc/hosts`. Then visit `http://tbcops.test` and `http://nagios.test/nagios/`. Typing only the IP does not select the correct Nginx virtual host. From inside the server, `127.0.0.1:3001` and `127.0.0.1:8080` are local-only checks; from another computer, `127.0.0.1` means that computer, not the server.

## Login

Nagios:

```text
https://nagios.example.com/nagios/
```

Use `NAGIOS_ADMIN_USER` and `NAGIOS_ADMIN_PASSWORD` from `.env`.

TBCOps:

```text
https://tbcops.example.com
```

Use the login/account process configured by the TBCOps image.

## SSL

With DNS configured:

```bash
sudo make configure-ssl
```

The script obtains separate certificates for both domains, verifies the certificates, runs a Certbot renewal dry run, and enables TLS after validation.

## Health and status

```bash
make status
make health
sudo make logs
```

Health output is PASS/FAIL. Public HTTPS checks validate the certificate chain and hostname.

## Add a monitored client in 3 steps

Configure `NAGIOS_SERVER_IP` and the Ansible settings in `.env`, then run these commands from the Nagios server:

```bash
sudo make add-client IP=10.0.0.5 NAME=web01 USER=ubuntu
sudo make ansible-copy-key
sudo make ansible-nrpe
```

`ansible-copy-key` prompts for the initial SSH and sudo passwords when needed; neither is saved. The full guide, including verification and removal, is [docs/ANSIBLE_NRPE.md](./docs/ANSIBLE_NRPE.md).

## Logs

```bash
sudo make logs
sudo journalctl -u nagios.service -n 200 --no-pager
sudo journalctl -u apache2 -n 200 --no-pager
sudo journalctl -u nginx -n 200 --no-pager
sudo docker compose logs --tail=200 opsknight-app
sudo docker compose logs --tail=200 opsknight-db
```

## Backups

Create a verified backup:

```bash
sudo make backup
```

Backups contain the PostgreSQL dump, `.env`, Compose file, project configuration, Nagios and Apache configuration, Nginx configuration, and SHA-256 checksums. Backup directories are mode `0700` and files are restricted.

## Restore

List timestamped backups, then restore one explicitly:

```bash
sudo find /opt/backups/opsknight-monitoring -mindepth 1 -maxdepth 1 -type d -printf '%p\n'
sudo make restore BACKUP=/opt/backups/opsknight-monitoring/20261124T000000Z CONFIRM_RESTORE=YES
```

The restore:

1. Verifies checksums and the database dump.
2. Creates a pre-restore backup.
3. Stops TBCOps.
4. Recreates the PostgreSQL database.
5. Restores the dump.
6. Starts TBCOps again.

Configuration archives are preserved for manual restoration and are not silently overwritten.

## Password rotation

Nagios password:

```bash
sudo make rotate-nagios-password
```

TBCOps application/session secrets:

```bash
sudo make rotate-tbcops-secrets
```

`ENCRYPTION_KEY` is deliberately not changed because changing it requires application-supported re-encryption.

## Updates

```bash
sudo make update
sudo make health
```

Normal deploy and update commands do not use `docker compose down -v` and do not delete the PostgreSQL volume.

## Rollback

Pin a known working image in `.env` and redeploy:

```bash
OPSKNIGHT_IMAGE=haideralimazari/tbc-ops:<known-working-tag>
sudo make deploy
```

For database rollback, restore a verified pre-update database backup using the restore procedure above.

## Service control

```bash
sudo make start
sudo make restart
sudo make stop
sudo make status
```

## Domain changes

1. Update `.env` domain and URL values.
2. Update DNS A records.
3. Run `sudo make configure-nginx`.
4. Run `sudo make configure-ssl`.
5. Redeploy with `sudo make deploy`.

For production, use real DNS hostnames; `.test` hostnames are only for private testing and cannot receive public certificates.

## Troubleshooting

### Nagios configuration failure

If validation reports `UNKNOWN VARIABLE` for `use_authentication` or an `authorized_for_*` setting, these are CGI directives and belong in `cgi.cfg`, not `nagios.cfg`. The Python configurator removes misplaced entries and writes them to the correct file; run `sudo make configure-nagios` after updating the project.

```bash
sudo /usr/local/nagios/bin/nagios -v /usr/local/nagios/etc/nagios.cfg
sudo journalctl -u nagios.service -n 200 --no-pager
```

### Nginx failure

```bash
sudo nginx -t
sudo nginx -T
sudo journalctl -u nginx -n 200 --no-pager
```

### Apache on the wrong port

```bash
sudo grep -R "Listen " /etc/apache2/ports.conf /etc/apache2/conf-enabled
sudo ss -ltnp | grep -E ':80|:443|:8080'
```

Apache for Nagios should listen only on `127.0.0.1:<NAGIOS_HTTP_PORT>`.

### TBCOps not healthy

```bash
sudo docker inspect opsknight-app --format '{{json .State.Health}}'
sudo docker compose logs --tail=200 opsknight-app
curl -v http://127.0.0.1:3001/api/health?mode=readiness
```

### SSL DNS failure

Verify public IP and DNS:

```bash
curl -4fsS https://api.ipify.org
getent ahostsv4 tbcops.example.com
getent ahostsv4 nagios.example.com
```

Replace the example domains with values from `.env`.
