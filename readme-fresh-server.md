# Fresh Server Setup and Domain-Free Testing

This guide covers a new Ubuntu server, private testing without owning a domain, and an optional ngrok preview. The supported production setup uses two real DNS names and HTTPS. The private test setup uses HTTP and must not be exposed to the public internet. The deployment automation is Python-based and uses only the Python standard library.

## What you need

- A clean Ubuntu 22.04 or newer server release that is still supported by Ubuntu and available in Docker's package repository (the deployment scripts do not support Windows).
- SSH access with a sudo-capable account.
- For a production deployment: two DNS A records pointing to the server's public IPv4 address and an email address for certificate notices.
- For private testing: no public DNS is required, but the client browser must resolve the two test hostnames to the server.

Keep a second SSH session open while installing or changing firewall rules.

## Get the project onto the server

Connect over SSH and install Git and Make if they are not already available:

```bash
sudo apt-get update
sudo apt-get install -y git make python3
git clone <YOUR_REPOSITORY_URL> tbcops-monitoring
cd tbcops-monitoring
```

Replace `<YOUR_REPOSITORY_URL>` with the repository URL you are authorized to use. If you have already copied the project to the server, `cd` into that project directory instead.

## Configure the environment

Create the private environment file and edit it before installing:

```bash
cp .env.example .env
chmod 600 .env
```

Replace the sample secrets before first deployment:

```bash
sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(openssl rand -hex 32)|" .env
sed -i "s|^API_KEY_SECRET=.*|API_KEY_SECRET=$(openssl rand -hex 32)|" .env
sed -i "s|^NEXTAUTH_SECRET=.*|NEXTAUTH_SECRET=$(openssl rand -hex 32)|" .env
sed -i "s|^ENCRYPTION_KEY=.*|ENCRYPTION_KEY=$(openssl rand -hex 32)|" .env
sed -i "s|^NAGIOS_ADMIN_PASSWORD=.*|NAGIOS_ADMIN_PASSWORD=$(openssl rand -hex 32)|" .env
```

Optionally generate a token if you use Prometheus scraping:

```bash
sed -i "s|^PROMETHEUS_SCRAPE_TOKEN=.*|PROMETHEUS_SCRAPE_TOKEN=$(openssl rand -hex 32)|" .env
```

`make check` creates `.env` with fresh secrets only if `.env` does not already exist. Since this guide copies the template first, you must replace its `CHANGE_ME` values as shown above. Keep `.env` private and do not commit or share it.

Now edit the domains/URLs and select the HTTP test configuration or HTTPS production configuration shown below:

```bash
nano .env
```

At minimum, check these values:

- `OPSKNIGHT_DOMAIN`, `NAGIOS_DOMAIN`: the two hostnames used by the reverse proxy.
- `OPSKNIGHT_PUBLIC_URL`, `NEXTAUTH_URL`: the exact TBCOps URL, including its scheme.
- `POSTGRES_PASSWORD`, `API_KEY_SECRET`, `NEXTAUTH_SECRET`, `ENCRYPTION_KEY`, `NAGIOS_ADMIN_PASSWORD`: unique secrets.
- `CERTBOT_EMAIL`: a monitored email for production certificates.
- `NGINX_ENABLE_SSL`: `true` for the production HTTPS setup; `false` for private HTTP testing.

The product branding is TBCOps, but keep the existing `OPSKNIGHT_*` environment variable names unchanged; deployment scripts use them as compatibility identifiers.

Do not change `ENCRYPTION_KEY` after storing encrypted application data unless the application supports re-encryption. Changing `POSTGRES_PASSWORD` after PostgreSQL has initialized also requires updating the database user's password inside PostgreSQL.

## Private test without buying a domain

The project validates hostname syntax but does not require public DNS when SSL is disabled. Use reserved `.test` names, which are only for local/private use:

```dotenv
OPSKNIGHT_DOMAIN=tbcops.test
NAGIOS_DOMAIN=nagios.test
OPSKNIGHT_PUBLIC_URL=http://tbcops.test
NEXTAUTH_URL=http://tbcops.test
NGINX_ENABLE_SSL=false
```

Keep the hostnames distinct. Do not use `localhost` as either hostname: the project requires two valid hostnames and Nginx routes requests using the hostname.

Do not put the server's IP address in `OPSKNIGHT_DOMAIN` or `NAGIOS_DOMAIN`. The deployment validator expects hostnames, and Nginx uses those names to choose the right site. Typing only the server IP in a browser is not the supported way to open these virtual hosts.

After configuring `.env`, one command installs and starts all components:

```bash
sudo make up
```

When `NGINX_ENABLE_SSL=false`, the SSL step deliberately skips Certbot. Nginx serves the application and Nagios over HTTP. The app can also be checked locally on the server:

```bash
curl -fsS 'http://127.0.0.1:3001/api/health?mode=readiness'
curl -I http://127.0.0.1:8080/nagios/
sudo make status
```

Those `127.0.0.1` addresses work only when run on the server itself. From your workstation, `127.0.0.1` refers to your workstation, not the server. To use the full browser login flow, make the client resolve `tbcops.test` and `nagios.test` to the server:

- If the browser is running on the server, map the names to `127.0.0.1`.
- If the browser is on another device on the same private network, map the names to the server's **private/LAN IP**.

For a one-off test, add the appropriate entries to that computer's hosts file (administrator access is usually required):

```text
<SERVER_PRIVATE_IP> tbcops.test nagios.test
```

For a browser running on the server itself, use this instead:

```text
127.0.0.1 tbcops.test nagios.test
```

On Windows, edit `C:\Windows\System32\drivers\etc\hosts` as Administrator. On Linux and macOS, edit `/etc/hosts`.

Then visit:

- `http://tbcops.test`
- `http://nagios.test/nagios/`

The hosts-file entries only affect that workstation; they do not create public DNS. Restrict HTTP access to your test network in both the server and cloud firewalls. This project's default UFW setup permits the configured HTTP port. Review it with `sudo ufw status numbered`; if necessary, remove the broad HTTP allow rule and allow only your test client's IP:

```bash
sudo ufw delete allow 80/tcp
sudo ufw allow from <YOUR_CLIENT_IP> to any port 80 proto tcp
```

Never use this HTTP mode for real credentials or production data over an untrusted network.

## Production setup with a domain

Create two DNS A records pointing to the server's public IPv4 address before running setup:

```text
tbcops.example.net    -> <SERVER_PUBLIC_IP>
nagios.example.net    -> <SERVER_PUBLIC_IP>
```

Replace the example hostnames with your own and set:

```dotenv
OPSKNIGHT_DOMAIN=tbcops.example.net
NAGIOS_DOMAIN=nagios.example.net
OPSKNIGHT_PUBLIC_URL=https://tbcops.example.net
NEXTAUTH_URL=https://tbcops.example.net
NGINX_ENABLE_SSL=true
CERTBOT_EMAIL=<YOUR_MONITORED_EMAIL>
```

Use real values for the remaining passwords/secrets too. Confirm ports 22, 80, and 443 are reachable, then:

```bash
sudo make up
sudo make health
```

`sudo make up` installs system packages, Docker and Compose, Nagios Core and plugins, configures Nginx, requests and validates both Let's Encrypt certificates, and deploys the containers. `sudo make setup` is retained as a backward-compatible alias. Do not run `make configure-ssl` until both DNS names resolve to this server.

After setup, Nagios is available at `https://<NAGIOS_DOMAIN>/nagios/`; sign in with `NAGIOS_ADMIN_USER` and `NAGIOS_ADMIN_PASSWORD` from `.env`. TBCOps is at `https://<OPSKNIGHT_DOMAIN>` and uses the account/login flow provided by the configured application image.

## Add monitored client servers

After `sudo make up` has installed Nagios on this fresh server, you can use it as the Ansible controller to add NRPE-monitored Linux clients. Clients must be Debian/Ubuntu or RHEL/Rocky/Alma, reachable over SSH, and have a sudo-capable login. Set the Nagios server's client-reachable IP in `.env` before provisioning:

```dotenv
NAGIOS_SERVER_IP=<NAGIOS_SERVER_PRIVATE_OR_MONITORING_IP>
ANSIBLE_USER=ubuntu
ANSIBLE_SSH_KEY_PATH=~/.ssh/tbcops_ansible
ANSIBLE_INVENTORY=ansible/inventory/hosts.ini
NRPE_PORT=5666
NRPE_CLIENT_SERVICES=nginx,docker,sshd
```

Use the address the clients will see as the source for NRPE traffic; allow TCP/5666 from that address in any cloud or network ACL. The Ansible role adds a scoped UFW/firewalld rule only if that firewall is already active. It does not enable an inactive firewall.

Install the pinned Ansible tools and collections, then add a client to the inventory:

```bash
sudo make install-ansible
sudo make add-client IP=10.0.0.5 NAME=web01 USER=ubuntu
```

Generate the controller key, copy its public half to the client, and check SSH. On first key copy, enter the client SSH password and sudo/become password when prompted; neither is saved:

```bash
sudo make ansible-ssh-key
sudo make ansible-copy-key
sudo make ansible-ping
```

Preview the changes before applying them, then verify NRPE end-to-end:

```bash
sudo make ansible-nrpe LIMIT=web01 CHECK=1
sudo make ansible-nrpe LIMIT=web01
sudo make ansible-verify
```

The playbook installs NRPE and plugins on the client, detects installed packages/services, registers the host and checks in Nagios, validates configuration before reload, and prints a host/OS/NRPE/services summary. Re-running `ansible-nrpe` should converge with no changes. Remove a client with `sudo make remove-client NAME=web01`. For details and troubleshooting, see [docs/ANSIBLE_NRPE.md](./docs/ANSIBLE_NRPE.md).

To include client provisioning in a future fresh `make up`, set `ENABLE_ANSIBLE_CLIENTS=true` in `.env` and ensure the Nagios IP and client inventory are already populated. Leave it `false` (the default) to preserve the normal fresh-server setup.

## Useful operations

Run these commands from the project directory:

```bash
sudo make status
sudo make health
sudo make logs
sudo make backup
sudo make restart
```

Before updates, take a backup. Normal deploy/update operations preserve the PostgreSQL Docker volume. See [readme.md](./readme.md) for restore, password rotation, rollback, and troubleshooting instructions.
