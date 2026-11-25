# Automated TBCOps + Nagios Deployment

This project deploys TBCOps with PostgreSQL in Docker and Nagios Core natively on an Ubuntu 22.04 or 24.04 server. Nginx owns public ports 80/443 and proxies TBCOps and Nagios; Apache listens only on `127.0.0.1:8080`.

## Architecture

- TBCOps container: `127.0.0.1:<APP_PORT>` (3001 by default)
- PostgreSQL container: `127.0.0.1:<POSTGRES_PORT>` (5432 by default)
- Nagios Apache/CGI: `127.0.0.1:<NAGIOS_HTTP_PORT>` (8080 by default)
- Nginx: public ports 80/443
- Backups: `BACKUP_DIR/<UTC timestamp>/`

PostgreSQL data is stored in the `opsknight_postgres_data` Docker volume.

## Prerequisites

- A fresh supported Ubuntu 22.04 or 24.04 server
- DNS A records for the two domains, each pointing to the server public IPv4 address
- SSH access retained as a separate fallback session
- A monitored address for `CERTBOT_EMAIL`
- Root or sudo access

Ports 22, 80, and 443 must remain reachable. Docker group membership is not enabled by this project because it grants root-equivalent privileges.

## Configuration

Copy and edit the environment file:

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

`make check` creates `.env` with generated secrets only when it does not already exist. Set at least:

- `OPSKNIGHT_DOMAIN`
- `NAGIOS_DOMAIN`
- `OPSKNIGHT_PUBLIC_URL=https://<tbcops-domain>`
- `NEXTAUTH_URL=https://<tbcops-domain>`
- `POSTGRES_PASSWORD`
- `API_KEY_SECRET`
- `NEXTAUTH_SECRET`
- `ENCRYPTION_KEY`
- `NAGIOS_ADMIN_PASSWORD`
- `CERTBOT_EMAIL`
- `NAGIOS_VERSION`
- `NAGIOS_PLUGINS_VERSION`

Do not commit `.env` or backup output.

For compatibility, keep the existing `OPSKNIGHT_*` environment variable names and Compose identifiers such as `opsknight-app` unchanged; TBCOps is the product branding.

For private testing without public DNS, use distinct local-only hostnames such as `tbcops.test` and `nagios.test`, set both app URLs to `http://tbcops.test`, and set `NGINX_ENABLE_SSL=false`. This mode is HTTP-only; keep it on a private network and do not expose it publicly. See [readme-fresh-server.md](./readme-fresh-server.md) for the full no-domain and ngrok instructions.

## DNS

Create two A records before SSL setup:

```text
tbcops.example.com     -> server public IPv4
nagios.example.com     -> server public IPv4
```

The SSL configuration step stops with a clear error if either record is missing or resolves elsewhere.

## Fresh installation

The server automation is implemented in Python and requires no pip packages. Configure `.env` once, then run this single command from an SSH session:

```bash
sudo make up
```

`make up` validates the environment, installs dependencies and Docker, installs Nagios Core and plugins, configures Nginx and SSL, and starts the containers. `make setup` remains available as a backward-compatible alias.

To perform setup in stages:

```bash
sudo make check
sudo make install-dependencies
sudo make install-docker
sudo make install-nagios
sudo make configure-nginx
sudo make configure-ssl
sudo make deploy
```

The setup may take several minutes while Nagios is compiled. If a step fails, Python reports the failed command and stops without continuing to later setup stages.

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
