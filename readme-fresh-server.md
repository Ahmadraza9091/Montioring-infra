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
nano .env
```

`make check` also creates `.env` and generates fresh secrets if the file is absent. It does not choose hostnames or replace all example values for you. Keep `.env` private and do not commit it.

At minimum, check these values:

- `OPSKNIGHT_DOMAIN`, `NAGIOS_DOMAIN`: the two hostnames used by the reverse proxy.
- `OPSKNIGHT_PUBLIC_URL`, `NEXTAUTH_URL`: the exact TBCOps URL, including its scheme.
- `POSTGRES_PASSWORD`, `API_KEY_SECRET`, `NEXTAUTH_SECRET`, `ENCRYPTION_KEY`, `NAGIOS_ADMIN_PASSWORD`: unique secrets.
- `CERTBOT_EMAIL`: a monitored email for production certificates.
- `NGINX_ENABLE_SSL`: `true` for the production HTTPS setup; `false` for private HTTP testing.

The product branding is TBCOps, but keep the existing `OPSKNIGHT_*` environment variable names unchanged; deployment scripts use them as compatibility identifiers.

You can generate random secret values with `openssl rand -hex 32`. Do not change `ENCRYPTION_KEY` after storing encrypted application data unless the application supports re-encryption.

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

## Can I use ngrok?

Yes, ngrok can provide temporary internet access for a preview, but it is **not a drop-in replacement for the two real DNS records in the production `make up` flow**. The SSL setup checks that both names resolve directly to the server and requests certificates for them. An ngrok hostname points to ngrok, so `make configure-ssl` will fail that check.

For a TBCOps-only preview, you can tunnel directly to the app's loopback port, bypassing Nginx:

1. Complete the server installation with private `.test` names and `NGINX_ENABLE_SSL=false`, or install the required services in stages. Do not run `make configure-ssl` for an ngrok hostname.
2. Install and authenticate the ngrok agent on the Ubuntu server using ngrok's official instructions. Start an HTTP tunnel to the local app port:

   ```bash
   ngrok http 3001
   ```

3. Use the HTTPS hostname assigned by ngrok. If you change the app's public hostname, set the hostname (without scheme) and URLs in `.env`, for example:

   ```dotenv
   OPSKNIGHT_DOMAIN=<YOUR_NGROK_HOSTNAME>
   OPSKNIGHT_PUBLIC_URL=https://<YOUR_NGROK_HOSTNAME>
   NEXTAUTH_URL=https://<YOUR_NGROK_HOSTNAME>
   NGINX_ENABLE_SSL=true
   ```

   Keep `NAGIOS_DOMAIN` set to a different syntactically valid hostname, such as `nagios.test`. The repository's environment validator expects the TBCOps URL scheme to be HTTPS when `NGINX_ENABLE_SSL=true`.

4. Apply the updated app environment:

   ```bash
   sudo make check
   sudo make deploy
   ```

   `make deploy` does not run Certbot. Do not run `sudo make up` or `sudo make configure-ssl` with the ngrok hostname. Open the HTTPS URL shown by ngrok.

This is an advanced, app-only preview rather than a fully supported deployment mode. The repository's health check also tests Nagios at `NAGIOS_DOMAIN` when SSL is enabled, so that public Nagios check will not pass with the example `nagios.test`. NextAuth/cookie behavior should be verified through ngrok; a changing ngrok hostname must be written to `.env` and the app redeployed. If authentication or callbacks do not work correctly, use a real DNS hostname with the normal HTTPS setup instead.

Ngrok makes the service reachable by anyone who has the tunnel URL unless you configure ngrok access controls. Do not tunnel a production instance or expose Nagios/admin interfaces without appropriate access restrictions.

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
