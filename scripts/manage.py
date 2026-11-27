#!/usr/bin/env python3
"""Python entry point for server setup and operations."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import hashlib
import ipaddress
import json
import os
import platform
import re
import secrets
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any


ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = Path(os.environ.get("ENV_FILE", ROOT / ".env")).expanduser().absolute()
NAGIOS_ETC = Path("/usr/local/nagios/etc")
NAGIOS_BIN = Path("/usr/local/nagios/bin/nagios")
NAGIOS_PLUGINS = Path("/usr/local/nagios/libexec")
ANSIBLE_DIR = ROOT / "ansible"
ANSIBLE_VENV = Path("/opt/tbcops-ansible")
ANSIBLE_BIN = ANSIBLE_VENV / "bin"
SYSTEM_CONFIG_PATHS = (
    NAGIOS_ETC,
    NAGIOS_ETC / "servers",
    Path("/etc/apache2/conf-available/nagios.conf"),
    Path("/etc/apache2/ports.conf"),
    Path("/etc/nginx/sites-available/opsknight-monitoring.conf"),
    Path("/etc/nginx/sites-available/nagios-monitoring.conf"),
    Path("/etc/nginx/conf.d/opsknight-connection-map.conf"),
    ROOT / "ansible",
)
ACCEPTED_HTTP_CODES = {200, 301, 302, 401, 403}


class DeploymentError(Exception):
    """An expected, user-actionable deployment failure."""


def log(message: str) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{timestamp}] {message}", flush=True)


def warn(message: str) -> None:
    log(f"WARNING: {message}")


def die(message: str) -> None:
    raise DeploymentError(message)


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    input_text: str | None = None,
    stdout: IO[Any] | int | None = None,
    stderr: IO[Any] | int | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            env=env,
            check=False,
            input=input_text,
            text=True,
            stdout=stdout,
            stderr=stderr,
        )
    except FileNotFoundError:
        die(f"Required command not found: {args[0]}")
    if check and result.returncode:
        die(f"Command failed with exit code {result.returncode}: {' '.join(args)}")
    return result


def capture(args: list[str], *, check: bool = True) -> str:
    result = run(args, check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        return ""
    return result.stdout.strip()


def require_root(action: str) -> None:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        die(f"Run this command as root (for example: sudo make {action}).")


def require_command(name: str) -> None:
    if shutil.which(name) is None:
        die(f"Required command not found: {name}")


def backup_existing(path: Path) -> None:
    if path.exists() or path.is_symlink():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        shutil.copy2(path, Path(f"{path}.bak.{stamp}"))


def atomic_write(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(content)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except OSError:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def init_env() -> dict[str, str]:
    example = ROOT / ".env.example"
    if not ENV_FILE.exists():
        if not example.is_file():
            die("Missing .env.example.")
        values = {
            "POSTGRES_PASSWORD": secrets.token_hex(24),
            "API_KEY_SECRET": secrets.token_hex(32),
            "NEXTAUTH_SECRET": secrets.token_hex(32),
            "ENCRYPTION_KEY": secrets.token_hex(32),
            "NAGIOS_ADMIN_PASSWORD": secrets.token_hex(20),
        }
        content = example.read_text(encoding="utf-8")
        for key, value in values.items():
            content = re.sub(rf"(?m)^{re.escape(key)}=.*$", f"{key}={value}", content)
        atomic_write(ENV_FILE, content, 0o600)
        log(f"Created {ENV_FILE} with fresh secrets. Edit hostnames and URLs before setup.")
    if ENV_FILE.is_symlink() or not ENV_FILE.is_file():
        die(".env must be a regular file, not a symlink.")
    os.chmod(ENV_FILE, 0o600)
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(ENV_FILE.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            die(f"Invalid .env entry on line {line_number}; expected KEY=value.")
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            die(f"Invalid .env variable name on line {line_number}.")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def valid_port(value: str) -> bool:
    return value.isdigit() and 0 < int(value) <= 65535


def valid_client_address(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return bool(
            len(value) <= 253
            and re.fullmatch(
                r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*",
                value,
            )
        )


def canonical_client_address(value: str) -> str:
    try:
        return ipaddress.ip_address(value).compressed.lower()
    except ValueError:
        return value.lower()


def valid_domain(value: str) -> bool:
    return (
        bool(re.fullmatch(r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}", value))
        and not value.endswith(".example.com")
        and "CHANGE_ME" not in value
    )


def validate_env(values: dict[str, str]) -> None:
    required = (
        "PROJECT_NAME",
        "TIMEZONE",
        "ADMIN_EMAIL",
        "OPSKNIGHT_DOMAIN",
        "NAGIOS_DOMAIN",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
        "API_KEY_SECRET",
        "NEXTAUTH_SECRET",
        "ENCRYPTION_KEY",
        "NAGIOS_ADMIN_USER",
        "NAGIOS_ADMIN_PASSWORD",
    )
    for name in required:
        if not values.get(name) or values[name] == "CHANGE_ME":
            die(f"Set {name} in .env (value not shown).")
    for name in ("OPSKNIGHT_DOMAIN", "NAGIOS_DOMAIN"):
        if not valid_domain(values[name]):
            die(f"{name} must be a valid hostname, not an example value.")
    if values["OPSKNIGHT_DOMAIN"] == values["NAGIOS_DOMAIN"]:
        die("TBCOps and Nagios require different hostnames.")
    ssl_enabled = values.get("NGINX_ENABLE_SSL", "true").lower()
    if ssl_enabled not in {"true", "false"}:
        die("NGINX_ENABLE_SSL must be true or false.")
    scheme = "https" if ssl_enabled == "true" else "http"
    expected_url = f"{scheme}://{values['OPSKNIGHT_DOMAIN']}"
    for name in ("OPSKNIGHT_PUBLIC_URL", "NEXTAUTH_URL"):
        if values.get(name) != expected_url:
            die(f"{name} must match the TBCOps hostname and scheme: {expected_url}.")
    for name in (
        "APP_PORT",
        "POSTGRES_PORT",
        "NAGIOS_HTTP_PORT",
        "ALLOW_SSH_PORT",
        "ALLOW_HTTP_PORT",
        "ALLOW_HTTPS_PORT",
    ):
        if not valid_port(values.get(name, "")):
            die(f"{name} must be a valid TCP port.")
    if values["APP_PORT"] == values["POSTGRES_PORT"]:
        die("APP_PORT and POSTGRES_PORT must differ.")
    for name in ("NAGIOS_VERSION", "NAGIOS_PLUGINS_VERSION"):
        if not re.fullmatch(r"\d+\.\d+\.\d+", values.get(name, "")):
            die(f"{name} must be a numeric x.y.z version.")
    if not values.get("BACKUP_RETENTION_DAYS", "").isdigit() or int(values["BACKUP_RETENTION_DAYS"]) < 1:
        die("BACKUP_RETENTION_DAYS must be a positive integer.")
    if values.get("FIREWALL_ENABLE", "true").lower() not in {"true", "false"}:
        die("FIREWALL_ENABLE must be true or false.")
    if values.get("ENABLE_ANSIBLE_CLIENTS", "false").lower() not in {"true", "false"}:
        die("ENABLE_ANSIBLE_CLIENTS must be true or false.")
    if values.get("ENABLE_ANSIBLE_CLIENTS", "false").lower() == "true" and not values.get("NAGIOS_SERVER_IP"):
        die("Set NAGIOS_SERVER_IP when ENABLE_ANSIBLE_CLIENTS=true.")
    if values.get("NRPE_PORT") and not valid_port(values["NRPE_PORT"]):
        die("NRPE_PORT must be a valid TCP port.")
    if values.get("NRPE_VERSION") and not re.fullmatch(r"\d+\.\d+\.\d+", values["NRPE_VERSION"]):
        die("NRPE_VERSION must be a numeric x.y.z version.")
    if values.get("NAGIOS_SERVER_IP"):
        try:
            ipaddress.ip_address(values["NAGIOS_SERVER_IP"])
        except ValueError:
            die("NAGIOS_SERVER_IP must be a valid IPv4 or IPv6 address.")
    if values.get("ANSIBLE_USER") and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*[$]?", values["ANSIBLE_USER"]):
        die("ANSIBLE_USER contains unsupported characters.")
    if values.get("ANSIBLE_SSH_KEY_PATH") and re.search(r"[\r\n]", values["ANSIBLE_SSH_KEY_PATH"]):
        die("ANSIBLE_SSH_KEY_PATH must not contain newlines.")
    if values.get("ANSIBLE_INVENTORY") and re.search(r"[\r\n]", values["ANSIBLE_INVENTORY"]):
        die("ANSIBLE_INVENTORY must not contain newlines.")
    services = [item.strip() for item in values.get("NRPE_CLIENT_SERVICES", "").split(",") if item.strip()]
    if any(not re.fullmatch(r"[A-Za-z0-9_.@-]+", item) for item in services):
        die("NRPE_CLIENT_SERVICES must contain comma-separated service names.")
    if values.get("NAGIOS_NRPE_PLUGIN_SHA256") and not re.fullmatch(
        r"[0-9a-fA-F]{64}", values["NAGIOS_NRPE_PLUGIN_SHA256"]
    ):
        die("NAGIOS_NRPE_PLUGIN_SHA256 must be a 64-character SHA-256.")
    identifier = r"[A-Za-z_][A-Za-z0-9_]*"
    if not re.fullmatch(identifier, values["POSTGRES_USER"]) or not re.fullmatch(identifier, values["POSTGRES_DB"]):
        die("Database user/name must use letters, digits and underscores.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", values["NAGIOS_ADMIN_USER"]):
        die("NAGIOS_ADMIN_USER contains unsupported characters.")


def ready_env() -> dict[str, str]:
    values = init_env()
    validate_env(values)
    return values


def ansible_inventory_path(values: dict[str, str]) -> Path:
    configured = Path(values.get("ANSIBLE_INVENTORY") or "ansible/inventory/hosts.ini").expanduser()
    return configured if configured.is_absolute() else ROOT / configured


def ensure_ansible_inventory(values: dict[str, str]) -> Path:
    inventory = ansible_inventory_path(values)
    if inventory.is_symlink():
        die(f"Refusing to use a symlink as the Ansible inventory: {inventory}")
    if not inventory.exists():
        example = ANSIBLE_DIR / "inventory" / "hosts.ini.example"
        if not example.is_file():
            die(f"Missing Ansible inventory template: {example}")
        atomic_write(inventory, example.read_text(encoding="utf-8"), 0o640)
    if not inventory.is_file():
        die(f"Ansible inventory must be a regular file: {inventory}")
    return inventory


def inventory_hosts(content: str) -> list[tuple[str, dict[str, str]]]:
    hosts: list[tuple[str, dict[str, str]]] = []
    in_group = False
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_group = stripped == "[nrpe_clients]"
            continue
        if not in_group:
            continue
        try:
            fields = shlex.split(stripped, comments=True)
        except ValueError:
            die("Ansible inventory contains invalid quoting in [nrpe_clients].")
        if not fields:
            continue
        variables: dict[str, str] = {}
        for field in fields[1:]:
            if "=" in field:
                key, value = field.split("=", 1)
                variables[key] = value
        hosts.append((fields[0], variables))
    return hosts


def append_inventory_host(content: str, host_line: str) -> str:
    lines = content.splitlines()
    group_index = next((index for index, line in enumerate(lines) if line.strip() == "[nrpe_clients]"), None)
    if group_index is None:
        die("Ansible inventory is missing the [nrpe_clients] group.")
    insert_at = len(lines)
    for index in range(group_index + 1, len(lines)):
        if lines[index].strip().startswith("["):
            insert_at = index
            break
    lines.insert(insert_at, host_line)
    return "\n".join(lines).rstrip() + "\n"


def ssh_key_path(values: dict[str, str]) -> Path:
    configured = values.get("ANSIBLE_SSH_KEY_PATH") or "~/.ssh/tbcops_ansible"
    return Path(configured).expanduser().absolute()


def ansible_process_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["ANSIBLE_CONFIG"] = str(ANSIBLE_DIR / "ansible.cfg")
    environment["ANSIBLE_HOST_KEY_CHECKING"] = "True"
    environment["ANSIBLE_COLLECTIONS_PATHS"] = str(ANSIBLE_VENV / "collections")
    return environment


def ansible_executable(name: str) -> Path:
    executable = ANSIBLE_BIN / name
    if not executable.is_file():
        die(f"Ansible is not installed at {ANSIBLE_VENV}; run make install-ansible first.")
    return executable


def ansible_extra_vars(values: dict[str, str]) -> str:
    server_ip = values.get("NAGIOS_SERVER_IP", "")
    if not server_ip:
        die("Set NAGIOS_SERVER_IP in .env before running Ansible client commands.")
    return json.dumps(
        {
            "nagios_server_ip": server_ip,
            "nrpe_port": int(values.get("NRPE_PORT") or "5666"),
            "nrpe_monitored_services": [
                service.strip()
                for service in values.get("NRPE_CLIENT_SERVICES", "nginx,docker,sshd").split(",")
                if service.strip()
            ],
            "nagios_check_nrpe_path": str(NAGIOS_PLUGINS / "check_nrpe"),
            "nagios_nrpe_plugin_version": values.get("NRPE_VERSION") or "4.1.3",
            "nagios_nrpe_plugin_sha256": values.get("NAGIOS_NRPE_PLUGIN_SHA256", ""),
            "nagios_delegate_host": "localhost",
        }
    )


def inventory_host_lines(inventory: Path) -> list[tuple[str, str, int]]:
    entries: list[tuple[str, str, int]] = []
    for name, variables in inventory_hosts(inventory.read_text(encoding="utf-8")):
        address = variables.get("ansible_host", name)
        raw_port = variables.get("ansible_port", "22")
        if not valid_client_address(address):
            die(f"Invalid address for inventory host {name}.")
        if not valid_port(raw_port):
            die(f"Invalid SSH port for inventory host {name}.")
        port = int(raw_port)
        entries.append((name, address, port))
    return entries


def prepare_known_hosts(inventory: Path, values: dict[str, str]) -> None:
    known_hosts = Path.home() / ".ssh" / "known_hosts"
    known_hosts.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    hosts = inventory_host_lines(inventory)
    if not hosts:
        die("No clients are present in the [nrpe_clients] inventory group.")
    if known_hosts.is_symlink():
        die(f"Refusing to use a symlink as known_hosts: {known_hosts}")
    for _, address, port in hosts:
        known_host = address if port == 22 else f"[{address}]:{port}"
        if run(
            ["ssh-keygen", "-F", known_host, "-f", str(known_hosts)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0:
            continue
        result = run(
            ["ssh-keyscan", "-H", "-T", "10", "-p", str(port), address],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode or not result.stdout.strip():
            die(f"Could not scan SSH host key for {address}:{port}; verify network access and try again.")
        with known_hosts.open("a", encoding="utf-8") as file:
            file.write(result.stdout)
        os.chmod(known_hosts, 0o600)


def add_client(values: dict[str, str]) -> None:
    inventory = ensure_ansible_inventory(values)
    address = os.environ.get("IP") or input("Client IP or hostname: ").strip()
    name = os.environ.get("NAME") or input("Inventory name: ").strip()
    user = os.environ.get("CLIENT_USER") or values.get("ANSIBLE_USER") or input("SSH user: ").strip()
    if not valid_client_address(address):
        die("IP must be a valid IPv4/IPv6 address or hostname.")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}", name):
        die("NAME must be 1-63 letters, digits, dots, underscores or hyphens.")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*[$]?", user):
        die("USER contains unsupported characters.")
    existing = inventory_hosts(inventory.read_text(encoding="utf-8"))
    normalized_address = canonical_client_address(address)
    if any(
        host.lower() == name.lower()
        or canonical_client_address(vars_.get("ansible_host", host)) == normalized_address
        for host, vars_ in existing
    ):
        die("A client with this name or address is already present in the inventory.")
    key = str(ssh_key_path(values))
    host_line = (
        f"{name} ansible_host={shlex.quote(address)} ansible_user={shlex.quote(user)} "
        f"ansible_port=22 ansible_ssh_private_key_file={shlex.quote(key)} ansible_become=true"
    )
    updated = append_inventory_host(inventory.read_text(encoding="utf-8"), host_line)
    atomic_write(inventory, updated, 0o640)
    log(f"Added {name} ({address}) to the Ansible inventory.")


def generate_ansible_key(values: dict[str, str]) -> Path:
    key = ssh_key_path(values)
    key.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if key.parent.name == ".ssh":
        os.chmod(key.parent, 0o700)
    if key.is_symlink():
        die(f"Refusing to use a symlink as the Ansible private key: {key}")
    if not key.exists():
        require_command("ssh-keygen")
        run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-C", "tbcops-ansible"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    if not key.is_file() or not Path(f"{key}.pub").is_file():
        die(f"SSH key generation did not produce both key files at {key}.")
    os.chmod(key, 0o600)
    os.chmod(Path(f"{key}.pub"), 0o644)
    log(f"Ansible SSH key is ready at {key}; key material was not displayed.")
    return key


def install_ansible(values: dict[str, str]) -> None:
    require_root("install-ansible")
    require_command("apt-get")
    require_command("python3")
    environment = os.environ.copy()
    environment["DEBIAN_FRONTEND"] = "noninteractive"
    run(["apt-get", "update"], env=environment)
    run(
        ["apt-get", "install", "-y", "python3-venv", "python3-dev", "build-essential", "openssh-client"],
        env=environment,
    )
    ANSIBLE_VENV.parent.mkdir(parents=True, exist_ok=True)
    if not (ANSIBLE_VENV / "bin" / "python").is_file():
        run(["python3", "-m", "venv", str(ANSIBLE_VENV)])
    python = ANSIBLE_BIN / "python"
    run(
        [
            str(python), "-m", "pip", "install",
            "ansible-core==2.17.13", "ansible-lint==24.12.2", "yamllint==1.35.1",
        ]
    )
    run(
        [
            str(ANSIBLE_BIN / "ansible-galaxy"), "collection", "install",
            "-r", str(ANSIBLE_DIR / "requirements.yml"), "-p", str(ANSIBLE_VENV / "collections"),
        ],
        cwd=ANSIBLE_DIR,
    )
    log("Pinned Ansible core, lint tools, and collections installed in /opt/tbcops-ansible.")


def ansible_copy_key(values: dict[str, str]) -> None:
    executable = ansible_executable("ansible-playbook")
    inventory = ensure_ansible_inventory(values)
    key = generate_ansible_key(values)
    prepare_known_hosts(inventory, values)
    public_key = Path(f"{key}.pub")
    extra_vars = json.dumps(
        {
            "ansible_public_key_path": str(public_key),
            "ansible_remote_user": values.get("ANSIBLE_USER", "root"),
        }
    )
    args = [
        str(executable),
        "-i", str(inventory),
        "playbooks/copy_ssh_key.yml",
        "--extra-vars", extra_vars,
    ]
    if os.environ.get("CI", "").lower() not in {"1", "true", "yes"}:
        args.extend(["--ask-pass", "--ask-become-pass"])
    run(args, cwd=ANSIBLE_DIR, env=ansible_process_environment())


def run_ansible_playbook(values: dict[str, str], playbook: str, *, check_mode: bool = False) -> None:
    executable = ansible_executable("ansible-playbook")
    extra_vars = ansible_extra_vars(values)
    inventory = ensure_ansible_inventory(values)
    prepare_known_hosts(inventory, values)
    args = [
        str(executable),
        "-i", str(inventory),
        playbook,
        "--extra-vars", extra_vars,
    ]
    limit = os.environ.get("LIMIT", "").strip()
    if limit:
        if not re.fullmatch(r"[A-Za-z0-9_.,:-]+", limit):
            die("LIMIT contains unsupported characters.")
        args.extend(["--limit", limit])
    if check_mode:
        args.extend(["--check", "--diff"])
    if os.environ.get("CI", "").lower() not in {"1", "true", "yes"}:
        args.append("--ask-become-pass")
    run(args, cwd=ANSIBLE_DIR, env=ansible_process_environment())


def ansible_ping(values: dict[str, str]) -> None:
    executable = ansible_executable("ansible")
    inventory = ensure_ansible_inventory(values)
    prepare_known_hosts(inventory, values)
    run(
        [
            str(executable),
            "nrpe_clients",
            "-i", str(inventory),
            "-m", "ansible.builtin.ping",
        ],
        cwd=ANSIBLE_DIR,
        env=ansible_process_environment(),
    )


def ansible_syntax() -> None:
    run(
        [
            str(ansible_executable("ansible-playbook")),
            "-i", "inventory/hosts.ini.example",
            "playbooks/site.yml",
            "--syntax-check",
        ],
        cwd=ANSIBLE_DIR,
        env=ansible_process_environment(),
    )


def ansible_lint() -> None:
    run(
        [str(ansible_executable("ansible-lint")), "--profile", "production", "."],
        cwd=ANSIBLE_DIR,
        env=ansible_process_environment(),
    )


def yaml_lint() -> None:
    executable = ansible_executable("yamllint")
    run(
        [str(executable), "-c", str(ROOT / ".yamllint"), "ansible"],
        cwd=ROOT,
        env=ansible_process_environment(),
    )


def ansible_verify(values: dict[str, str]) -> None:
    inventory = ensure_ansible_inventory(values)
    check_nrpe = NAGIOS_PLUGINS / "check_nrpe"
    if not check_nrpe.is_file():
        die(f"check_nrpe is missing at {check_nrpe}; run make ansible-nrpe first.")
    port = values.get("NRPE_PORT", "5666")
    failures = 0
    print(f"{'HOST':<24} {'ADDRESS':<42} RESULT")
    hosts = inventory_host_lines(inventory)
    if not hosts:
        die("No clients are present in the [nrpe_clients] inventory group.")
    for name, address, _ in hosts:
        result = run(
            [str(check_nrpe), "-H", address, "-p", port, "-c", "check_load"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        passed = result.returncode == 0
        print(f"{name:<24} {address:<42} {'PASS' if passed else 'FAIL'}")
        if not passed and result.stdout.strip():
            print(f"  {result.stdout.strip()}")
        if not passed and result.stderr.strip():
            print(f"  {result.stderr.strip()}")
        failures += not passed
    if failures:
        die(f"NRPE verification failed for {failures} client(s).")
    log("NRPE verification passed for all inventory clients.")


def remove_client(values: dict[str, str]) -> None:
    name = os.environ.get("NAME", "").strip()
    if not name or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}", name):
        die("Usage: make remove-client NAME=<inventory-name>.")
    inventory = ensure_ansible_inventory(values)
    original_inventory = inventory.read_text(encoding="utf-8")
    hosts = inventory_hosts(original_inventory)
    if not any(host == name for host, _ in hosts):
        die(f"No inventory client named {name} was found.")
    lines = original_inventory.splitlines()
    output: list[str] = []
    in_group = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_group = stripped == "[nrpe_clients]"
        if in_group and stripped and not stripped.startswith(("#", ";")):
            try:
                fields = shlex.split(stripped, comments=True)
            except ValueError:
                die("Ansible inventory contains invalid quoting in [nrpe_clients].")
            if fields and fields[0] == name:
                continue
        output.append(line)
    cfg = NAGIOS_ETC / "servers" / "clients" / f"{name}.cfg"
    report = NAGIOS_ETC / "servers" / "inventory" / f"{name}.yml"
    for path in (cfg, report):
        if path.is_symlink():
            die(f"Refusing to remove a symlink: {path}")
    if not NAGIOS_BIN.is_file() or not (NAGIOS_ETC / "nagios.cfg").is_file():
        die("Nagios is not installed on this server; cannot safely validate client removal.")
    cfg_content = cfg.read_text(encoding="utf-8") if cfg.is_file() else None
    report_content = report.read_text(encoding="utf-8") if report.is_file() else None
    atomic_write(inventory, "\n".join(output).rstrip() + "\n", 0o640)
    for path in (cfg, report):
        if path.exists():
            path.unlink()
    validation = run([str(NAGIOS_BIN), "-v", str(NAGIOS_ETC / "nagios.cfg")], check=False)
    if validation.returncode:
        atomic_write(inventory, original_inventory, 0o640)
        if cfg_content is not None:
            atomic_write(cfg, cfg_content, 0o640)
        if report_content is not None:
            atomic_write(report, report_content, 0o640)
        die(f"Nagios validation failed; inventory and client files for {name} were restored.")
    run(["systemctl", "reload", "nagios.service"])
    log(f"Removed {name} from the inventory and Nagios configuration.")


def compose(values: dict[str, str], *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    require_command("docker")
    env = os.environ.copy()
    env.update(values)
    env["POSTGRES_PASSWORD_URLENCODED"] = urllib.parse.quote(values["POSTGRES_PASSWORD"], safe="")
    return run(
        [
            "docker",
            "compose",
            "--env-file",
            str(ENV_FILE),
            "-f",
            str(ROOT / "docker-compose.yml"),
            *args,
        ],
        env=env,
        **kwargs,
    )


def ubuntu_release() -> dict[str, str]:
    if not Path("/etc/os-release").is_file():
        die("Cannot identify the operating system.")
    release: dict[str, str] = {}
    for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        release[key] = value.strip('"')
    return release


def ubuntu_version_supported(version_id: str) -> bool:
    match = re.fullmatch(r"(\d+)\.(\d+)", version_id)
    if not match:
        return False
    return tuple(map(int, match.groups())) >= (22, 4)


def check_prerequisites() -> None:
    values = ready_env()
    if platform.system() != "Linux":
        die("Deployment scripts require Linux; Windows is supported only for editing and static checks.")
    release = ubuntu_release()
    if release.get("ID") != "ubuntu":
        die(f"Ubuntu is required (detected: {release.get('PRETTY_NAME', 'unknown')}).")
    if not ubuntu_version_supported(release.get("VERSION_ID", "")):
        die(f"Ubuntu 22.04 or newer is required (detected: {release.get('PRETTY_NAME', 'unknown')}).")
    for name in ("python3",):
        require_command(name)
    if shutil.which("docker"):
        if capture(["docker", "compose", "version"], check=False):
            compose(values, "config", "--quiet")
        else:
            warn("Docker Compose plugin is not installed yet; make up will install it.")
    log(f"Prerequisite and environment validation passed for {release.get('PRETTY_NAME', 'Ubuntu')}.")


def install_dependencies(values: dict[str, str]) -> None:
    require_root("install-dependencies")
    release = ubuntu_release()
    if release.get("ID") != "ubuntu":
        die("Ubuntu is required.")
    log("Installing Ubuntu build, web, and deployment dependencies.")
    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    run(["apt-get", "update"], env=env)
    packages = [
        "curl", "wget", "git", "unzip", "tar", "build-essential", "gcc", "make", "autoconf",
        "apache2", "apache2-utils", "php", "libapache2-mod-php", "php-gd", "libgd-dev", "libssl-dev",
        "libperl-dev", "libnet-snmp-perl", "gettext", "bc", "gawk", "dc", "openssl", "ca-certificates",
        "gnupg", "lsb-release", "ufw", "certbot", "postgresql-client", "python3-certbot-nginx", "nginx",
        "python3",
    ]
    run(["apt-get", "install", "-y", "--no-install-recommends", *packages], env=env)
    run(["systemctl", "enable", "apache2", "nginx"])
    if values.get("FIREWALL_ENABLE", "true").lower() == "true":
        require_command("ufw")
        run(["ufw", "allow", f"{values['ALLOW_SSH_PORT']}/tcp", "comment", "SSH access"])
        if shutil.which("ss"):
            listeners = capture(["ss", "-ltnp"], check=False)
            for match in re.finditer(r"sshd.*?:(\d+)\s", listeners):
                port = match.group(1)
                if valid_port(port):
                    run(["ufw", "allow", f"{port}/tcp", "comment", "Existing SSH listener"])
        run(["ufw", "allow", f"{values['ALLOW_HTTP_PORT']}/tcp", "comment", "HTTP for web and ACME"])
        run(["ufw", "allow", f"{values['ALLOW_HTTPS_PORT']}/tcp", "comment", "HTTPS web access"])
        status = capture(["ufw", "status"], check=False)
        if not status.startswith("Status: active"):
            warn("Enabling UFW after allowing configured SSH/HTTP/HTTPS ports; existing rules are preserved.")
            run(["ufw", "--force", "enable"])
    log("Dependencies installed.")


def download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "TBCOps-deployer/1.0"})
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as target:
                shutil.copyfileobj(response, target)
            return
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = error
            if attempt < 2:
                time.sleep(2**attempt)
    die(f"Could not download {url}: {last_error}")


def verify_sha256(path: Path, expected: str, label: str) -> None:
    if not expected:
        warn(f"No {label} checksum configured; TLS transport was verified, but a release checksum was not independently pinned.")
        return
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if not secrets.compare_digest(actual.lower(), expected.lower()):
        die(f"{label} checksum verification failed.")


def install_docker(values: dict[str, str]) -> None:
    require_root("install-docker")
    release = ubuntu_release()
    if release.get("ID") != "ubuntu":
        die("Official Docker repository setup supports Ubuntu only.")
    architecture = capture(["dpkg", "--print-architecture"])
    if architecture not in {"amd64", "arm64", "armhf", "ppc64el", "s390x"}:
        die(f"Unsupported Docker architecture: {architecture}")
    key_path = Path("/etc/apt/keyrings/docker.asc")
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if not key_path.is_file() or key_path.stat().st_size == 0:
        download("https://download.docker.com/linux/ubuntu/gpg", key_path)
        os.chmod(key_path, 0o644)
    codename = release.get("VERSION_CODENAME", "")
    if not codename:
        die("Ubuntu release codename is missing.")
    repository = (
        f"deb [arch={architecture} signed-by={key_path}] "
        f"https://download.docker.com/linux/ubuntu {codename} stable"
    )
    repo_path = Path("/etc/apt/sources.list.d/docker.list")
    if not repo_path.is_file() or repo_path.read_text(encoding="utf-8").strip() != repository:
        atomic_write(repo_path, repository + "\n", 0o644)
    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    run(["apt-get", "update"], env=env)
    run(
        [
            "apt-get", "install", "-y", "docker-ce", "docker-ce-cli", "containerd.io",
            "docker-buildx-plugin", "docker-compose-plugin",
        ],
        env=env,
    )
    run(["systemctl", "enable", "--now", "docker"])
    run(["docker", "compose", "version"])
    log("Docker Engine and Compose plugin are installed and enabled.")
    warn("Docker group membership grants root-equivalent privileges; this setup does not add users to that group.")


def install_nagios_plugins(values: dict[str, str], work: Path) -> None:
    version = values["NAGIOS_PLUGINS_VERSION"]
    archive = f"nagios-plugins-{version}.tar.gz"
    url = f"https://github.com/nagios-plugins/nagios-plugins/releases/download/release-{version}/{archive}"
    path = work / archive
    log(f"Downloading Nagios Plugins {version} from the official project release.")
    download(url, path)
    verify_sha256(path, values.get("NAGIOS_PLUGINS_SHA256", ""), "NAGIOS_PLUGINS")
    run(["tar", "-xzf", str(path), "-C", str(work)])
    source = work / f"nagios-plugins-{version}"
    run(
        [
            "./configure", "--prefix=/usr/local/nagios", "--with-nagios-user=nagios",
            "--with-nagios-group=nagios",
        ],
        cwd=source,
    )
    run(["make", f"-j{os.cpu_count() or 1}"], cwd=source)
    run(["make", "install"], cwd=source)
    if not (NAGIOS_PLUGINS / "check_ping").is_file() or not (NAGIOS_PLUGINS / "check_load").is_file():
        die("Expected Nagios plugins were not installed.")
    log(f"Nagios Plugins {version} installed.")


def remove_config_directives(content: str, keys: tuple[str, ...]) -> str:
    for key in keys:
        content = re.sub(
            rf"(?m)^[ \t]*{re.escape(key)}[ \t]*=.*(?:\r?\n|$)",
            "",
            content,
        )
    return content


def set_config_directives(content: str, directives: dict[str, str]) -> str:
    for key, value in directives.items():
        pattern = rf"(?m)^[ \t]*{re.escape(key)}[ \t]*=.*$"
        line = f"{key}={value}"
        if re.search(pattern, content):
            content = re.sub(pattern, line, content)
        else:
            content = content.rstrip() + f"\n{line}\n"
    return content


def configure_nagios(values: dict[str, str]) -> None:
    require_root("configure-nagios")
    require_command("htpasswd")
    if not NAGIOS_BIN.is_file():
        die("Nagios Core is not installed.")
    if not (NAGIOS_PLUGINS / "check_ping").is_file():
        die("Nagios Plugins are not installed.")
    ports_file = Path("/etc/apache2/ports.conf")
    if not ports_file.is_file():
        die("Apache ports.conf is missing.")
    ports_text = ports_file.read_text(encoding="utf-8")
    active_ports = re.findall(r"(?m)^\s*Listen\s+(\S+)", ports_text)
    allowed = {"80", "443", f"127.0.0.1:{values['NAGIOS_HTTP_PORT']}"}
    if any(item not in allowed for item in active_ports):
        die(f"Custom Apache Listen directives found in {ports_file}; resolve them manually before continuing.")
    expected = f"Listen 127.0.0.1:{values['NAGIOS_HTTP_PORT']}"
    if expected not in active_ports or any(item in {"80", "443"} for item in active_ports):
        backup_existing(ports_file)
        ports_text = re.sub(
            r"(?m)^\s*Listen\s+(?:80|443|\[::\]:80|\[::\]:443)\s*(?:#.*)?\n?",
            "",
            ports_text,
        )
        ports_text = re.sub(r"(?m)^\s*Listen\s+127\.0\.0\.1:\d+\s*(?:#.*)?\n?", "", ports_text)
        atomic_write(ports_file, ports_text.rstrip() + f"\n{expected}\n")
    apache_conf = Path("/etc/apache2/conf-available/nagios.conf")
    template = (ROOT / "templates" / "nagios-apache.conf.template").read_text(encoding="utf-8")
    if not apache_conf.is_file() or apache_conf.read_text(encoding="utf-8") != template:
        backup_existing(apache_conf)
        atomic_write(apache_conf, template)
    servers_dir = NAGIOS_ETC / "servers"
    servers_dir.mkdir(parents=True, exist_ok=True)
    local_config = servers_dir / "local.cfg"
    if not local_config.exists():
        shutil.copy2(ROOT / "configs" / "nagios" / "local.cfg", local_config)
    nagios_config = NAGIOS_ETC / "nagios.cfg"
    original_nagios_config = nagios_config.read_text(encoding="utf-8")
    content = original_nagios_config
    if "cfg_dir=/usr/local/nagios/etc/servers" not in content:
        content = content.rstrip() + "\ncfg_dir=/usr/local/nagios/etc/servers\n"
    authorization_keys = (
        "authorized_for_system_information",
        "authorized_for_configuration_information",
        "authorized_for_system_commands",
        "authorized_for_all_services",
        "authorized_for_all_hosts",
        "authorized_for_all_service_commands",
        "authorized_for_all_host_commands",
    )
    auth_keys = ("use_authentication", *authorization_keys)
    content = remove_config_directives(content, auth_keys)
    if content != original_nagios_config:
        backup_existing(nagios_config)
        atomic_write(nagios_config, content, nagios_config.stat().st_mode & 0o777)
    cgi_config = NAGIOS_ETC / "cgi.cfg"
    if not cgi_config.is_file():
        die(f"Nagios CGI configuration is missing: {cgi_config}.")
    cgi_content = cgi_config.read_text(encoding="utf-8")
    cgi_directives = {"use_authentication": "1"}
    cgi_directives.update({key: values["NAGIOS_ADMIN_USER"] for key in authorization_keys})
    updated_cgi_content = set_config_directives(cgi_content, cgi_directives)
    if updated_cgi_content != cgi_content:
        backup_existing(cgi_config)
        atomic_write(cgi_config, updated_cgi_content, cgi_config.stat().st_mode & 0o777)
    auth_file = NAGIOS_ETC / "htpasswd.users"
    auth_file.touch(exist_ok=True)
    run(["chown", "root:www-data", str(auth_file)])
    run(["chmod", "0640", str(auth_file)])
    run(
        ["htpasswd", "-iB", str(auth_file), values["NAGIOS_ADMIN_USER"]],
        input_text=values["NAGIOS_ADMIN_PASSWORD"] + "\n",
        stdout=subprocess.DEVNULL,
    )
    run(["chown", "nagios:nagios", str(local_config)])
    run(["chmod", "0640", str(local_config)])
    run(["a2enmod", "cgi", "auth_basic", "authn_file"], stdout=subprocess.DEVNULL)
    run(["a2enconf", "nagios"], stdout=subprocess.DEVNULL)
    run([str(NAGIOS_BIN), "-v", str(nagios_config)])
    run(["apache2ctl", "configtest"])
    run(["systemctl", "enable", "nagios.service", "apache2"])
    run(["systemctl", "restart", "apache2"])
    run(["systemctl", "restart", "nagios.service"])
    log(f"Nagios configured; Apache listens on 127.0.0.1:{values['NAGIOS_HTTP_PORT']}.")


def install_nagios(values: dict[str, str]) -> None:
    require_root("install-nagios")
    for command in ("curl", "tar", "make", "gcc", "autoconf"):
        require_command(command)
    if subprocess.run(["id", "nagios"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        run(["useradd", "--system", "--home-dir", "/usr/local/nagios", "--shell", "/usr/sbin/nologin", "nagios"])
    if subprocess.run(["getent", "group", "nagcmd"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        run(["groupadd", "--system", "nagcmd"])
    run(["usermod", "-a", "-G", "nagcmd", "nagios"])
    if subprocess.run(["getent", "group", "www-data"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        run(["usermod", "-a", "-G", "nagcmd", "www-data"])
    with tempfile.TemporaryDirectory(prefix=".work-nagios-", dir=ROOT) as temporary:
        work = Path(temporary)
        version = values["NAGIOS_VERSION"]
        archive = f"nagios-{version}.tar.gz"
        url = f"https://assets.nagios.com/downloads/nagioscore/releases/{archive}"
        path = work / archive
        log(f"Downloading Nagios Core {version} from the official Nagios release site.")
        download(url, path)
        verify_sha256(path, values.get("NAGIOS_CORE_SHA256", ""), "NAGIOS_CORE")
        run(["tar", "-xzf", str(path), "-C", str(work)])
        source = work / f"nagios-{version}"
        run(
            [
                "./configure", "--prefix=/usr/local/nagios", "--with-nagios-user=nagios",
                "--with-nagios-group=nagios", "--with-command-group=nagcmd",
                "--with-httpd-conf=/etc/apache2/conf-available",
            ],
            cwd=source,
        )
        run(["make", f"-j{os.cpu_count() or 1}", "all"], cwd=source)
        for target in (
            "install-groups-users",
            "install",
            "install-commandmode",
            "install-init",
            "install-config",
            "install-webconf",
        ):
            run(["make", target], cwd=source)
        run(["install", "-d", "-o", "nagios", "-g", "nagios", "-m", "0750", "/usr/local/nagios/var"])
        run(["install", "-d", "-o", "nagios", "-g", "nagios", "-m", "0750", "/usr/local/nagios/var/rw"])
        auth_file = NAGIOS_ETC / "htpasswd.users"
        if not auth_file.exists():
            run(["install", "-o", "root", "-g", "www-data", "-m", "0640", "/dev/null", str(auth_file)])
        install_nagios_plugins(values, work)
    configure_nagios(values)
    log("Nagios Core and plugins installed and configured.")


def render_nginx(template_path: Path, output_path: Path, values: dict[str, str], tls: bool) -> None:
    source = template_path.read_text(encoding="utf-8")
    output: list[str] = []
    skip = False
    for line in source.splitlines():
        if line == "# @TLS_BEGIN@":
            skip = not tls
            continue
        if line == "# @TLS_END@":
            skip = False
            continue
        if not skip:
            output.append(line)
    data = "\n".join(output) + "\n"
    replacements = {
        "__OPSKNIGHT_DOMAIN__": "OPSKNIGHT_DOMAIN",
        "__NAGIOS_DOMAIN__": "NAGIOS_DOMAIN",
        "__APP_PORT__": "APP_PORT",
        "__NAGIOS_HTTP_PORT__": "NAGIOS_HTTP_PORT",
        "__HTTP_PORT__": "ALLOW_HTTP_PORT",
        "__HTTPS_PORT__": "ALLOW_HTTPS_PORT",
    }
    for token, key in replacements.items():
        data = data.replace(token, values[key])
    if values.get("NGINX_ENABLE_SSL", "true").lower() == "false":
        if "opsknight.nginx" in template_path.name:
            data = data.replace(
                "location / { return 301 https://$host$request_uri; }",
                "location / { proxy_pass http://127.0.0.1:"
                + values["APP_PORT"]
                + "; proxy_http_version 1.1; proxy_set_header Host $host; "
                "proxy_set_header X-Real-IP $remote_addr; "
                "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for; "
                "proxy_set_header X-Forwarded-Proto http; proxy_set_header Upgrade $http_upgrade; "
                "proxy_set_header Connection $connection_upgrade; proxy_buffering off; }",
            )
        else:
            data = data.replace(
                "location / { return 301 https://$host$request_uri; }",
                "location / { return 302 /nagios/; }\n"
                "    location /nagios/ { proxy_pass http://127.0.0.1:"
                + values["NAGIOS_HTTP_PORT"]
                + "/nagios/; proxy_set_header Host $host; "
                "proxy_set_header X-Real-IP $remote_addr; "
                "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for; "
                "proxy_set_header X-Forwarded-Proto http; }",
            )
    atomic_write(output_path, data)


def configure_nginx(values: dict[str, str], tls: bool = False) -> None:
    require_root("configure-nginx")
    require_command("nginx")
    if tls and values.get("NGINX_ENABLE_SSL", "true").lower() != "true":
        die("Refusing to enable TLS while NGINX_ENABLE_SSL=false.")
    Path("/var/www/certbot").mkdir(parents=True, exist_ok=True)
    Path("/etc/nginx/sites-available").mkdir(parents=True, exist_ok=True)
    Path("/etc/nginx/sites-enabled").mkdir(parents=True, exist_ok=True)
    map_path = Path("/etc/nginx/conf.d/opsknight-connection-map.conf")
    map_text = "map $http_upgrade $connection_upgrade {\n    default upgrade;\n    '' close;\n}\n"
    if not map_path.is_file() or map_path.read_text(encoding="utf-8") != map_text:
        backup_existing(map_path)
        atomic_write(map_path, map_text)
    with tempfile.TemporaryDirectory(prefix=".work-nginx-", dir=ROOT) as temporary:
        temporary_path = Path(temporary)
        outputs = (
            (ROOT / "templates" / "opsknight.nginx.conf.template", temporary_path / "opsknight.conf",
             Path("/etc/nginx/sites-available/opsknight-monitoring.conf")),
            (ROOT / "templates" / "nagios.nginx.conf.template", temporary_path / "nagios.conf",
             Path("/etc/nginx/sites-available/nagios-monitoring.conf")),
        )
        for template, rendered, destination in outputs:
            render_nginx(template, rendered, values, tls)
            rendered_text = rendered.read_text(encoding="utf-8")
            if not destination.is_file() or destination.read_text(encoding="utf-8") != rendered_text:
                backup_existing(destination)
                shutil.copyfile(rendered, destination)
                os.chmod(destination, 0o644)
        for name in ("opsknight-monitoring.conf", "nagios-monitoring.conf"):
            link = Path("/etc/nginx/sites-enabled") / name
            target = Path("/etc/nginx/sites-available") / name
            if link.is_symlink() and link.resolve() == target:
                continue
            if link.exists() or link.is_symlink():
                die(f"Refusing to replace existing Nginx site: {link}")
            link.symlink_to(target)
    run(["nginx", "-t"])
    run(["systemctl", "enable", "nginx"])
    if run(["systemctl", "reload", "nginx"], check=False).returncode:
        run(["systemctl", "start", "nginx"])
    log(f"Nginx configuration validated and enabled (TLS blocks: {str(tls).lower()}).")
    if not tls and values.get("NGINX_ENABLE_SSL", "true").lower() == "true":
        log("HTTP bootstrap virtual hosts are active; run make configure-ssl after public DNS resolves.")


def configure_ssl(values: dict[str, str]) -> None:
    require_root("configure-ssl")
    if values.get("NGINX_ENABLE_SSL", "true").lower() != "true":
        log("SSL is disabled; retaining the configured private/test HTTP virtual hosts.")
        return
    for command in ("certbot", "curl"):
        require_command(command)
    email = values.get("CERTBOT_EMAIL", "")
    if not email or email == "admin@example.com":
        die("Set CERTBOT_EMAIL to an address monitored by an administrator.")
    try:
        request = urllib.request.Request("https://api.ipify.org", headers={"User-Agent": "TBCOps-deployer/1.0"})
        with urllib.request.urlopen(request, timeout=10) as response:
            public_ip = response.read().decode("ascii").strip()
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        die(f"Could not determine this server's public IPv4 address: {error}")
    for domain in (values["OPSKNIGHT_DOMAIN"], values["NAGIOS_DOMAIN"]):
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(domain, None, socket.AF_INET)}
        except socket.gaierror:
            addresses = set()
        if not addresses:
            die(f"DNS is not configured for {domain}. Add an A record pointing to {public_ip}.")
        if public_ip not in addresses:
            die(f"{domain} resolves to a different address; update its A record to {public_ip}.")
    configure_nginx(values)
    for domain in (values["OPSKNIGHT_DOMAIN"], values["NAGIOS_DOMAIN"]):
        run(
            [
                "certbot", "certonly", "--non-interactive", "--agree-tos", "--email", email,
                "--webroot", "-w", "/var/www/certbot", "--keep-until-expiring",
                "--cert-name", domain, "-d", domain,
            ]
        )
        for suffix in ("fullchain.pem", "privkey.pem"):
            if not (Path("/etc/letsencrypt/live") / domain / suffix).is_file():
                die(f"Certbot did not produce a valid certificate for {domain}.")
    run(["certbot", "renew", "--dry-run"])
    hook = Path("/etc/letsencrypt/renewal-hooks/deploy/reload-nginx")
    hook_content = (
        "#!/usr/bin/python3\n"
        "import subprocess\n"
        "subprocess.run(['/usr/bin/systemctl', 'reload', 'nginx'], check=True)\n"
    )
    hook.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(hook, hook_content, 0o755)
    if run(["systemctl", "enable", "--now", "certbot.timer"], check=False).returncode:
        warn("Could not enable certbot.timer; configure certificate renewal manually.")
    configure_nginx(values, tls=True)
    run(["nginx", "-t"])
    run(["systemctl", "reload", "nginx"])
    log("Certificates validated and HTTPS enabled for both hostnames.")


def deploy(values: dict[str, str], mode: str = "deploy") -> None:
    require_root(mode)
    if mode in {"deploy", "update"}:
        run(["docker", "compose", "version"])
        compose(values, "config", "--quiet")
        log("Pulling TBCOps and PostgreSQL images.")
        compose(values, "pull")
        compose(values, "up", "-d")
    elif mode == "start":
        run(["systemctl", "start", "nginx", "apache2", "nagios.service"])
        compose(values, "up", "-d")
    elif mode == "stop":
        compose(values, "stop")
        run(["systemctl", "stop", "nagios.service", "apache2", "nginx"])
    elif mode == "restart":
        compose(values, "restart")
        run(["systemctl", "restart", "nagios.service", "apache2", "nginx"])
    else:
        die(f"Unknown deployment mode: {mode}")
    if mode in {"deploy", "update"}:
        log("Waiting up to 180 seconds for TBCOps readiness.")
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            status = capture(["docker", "inspect", "--format", "{{.State.Health.Status}}", "opsknight-app"], check=False)
            if status == "healthy":
                break
            time.sleep(5)
        else:
            die("TBCOps did not become healthy. Inspect with make logs.")
        database_status = capture(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", "opsknight-db"],
            check=False,
        )
        if database_status != "healthy":
            die("PostgreSQL is not healthy.")
        log("Deployment completed; persistent volumes were left untouched.")


def start_service_status(name: str) -> bool:
    return run(["systemctl", "is-active", "--quiet", name], check=False).returncode == 0


def container_status(name: str) -> tuple[str, str]:
    if shutil.which("docker") is None:
        return "missing", "unknown"
    state = capture(["docker", "inspect", "--format", "{{.State.Status}}", name], check=False) or "missing"
    health = capture(
        ["docker", "inspect", "--format", "{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}", name],
        check=False,
    ) or "unknown"
    return state, health


def status() -> None:
    values = ready_env()
    failures = 0
    for service in ("nginx", "apache2", "nagios.service", "docker"):
        if start_service_status(service):
            print(f"ACTIVE  {service}")
        else:
            print(f"INACTIVE {service}")
            failures += 1
    for name in ("opsknight-db", "opsknight-app"):
        state, health = container_status(name)
        print(f"{state:<8} {name:<20} health={health}")
        if state != "running" or health == "unhealthy":
            failures += 1
    if failures:
        die(f"{failures} service/container check(s) failed.")


def http_status(url: str, verify_tls: bool) -> bool:
    context = None if verify_tls else ssl._create_unverified_context()
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=15, context=context) as response:
            return response.status in ACCEPTED_HTTP_CODES
    except urllib.error.HTTPError as error:
        return error.code in ACCEPTED_HTTP_CODES
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def command_succeeds(args: list[str]) -> bool:
    if shutil.which(args[0]) is None:
        return False
    return run(args, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def health(values: dict[str, str]) -> None:
    checks: list[tuple[str, bool]] = []
    checks.append(("Docker daemon active", start_service_status("docker")))
    for name, label in (("opsknight-db", "PostgreSQL"), ("opsknight-app", "TBCOps")):
        state, health_state = container_status(name)
        checks.append((f"{label} container running", state == "running"))
        checks.append((f"{label} healthcheck healthy", health_state == "healthy"))
    checks.extend(
        (
            ("Nagios service active", start_service_status("nagios.service")),
            ("Apache service active", start_service_status("apache2")),
            ("Nagios configuration valid", command_succeeds([str(NAGIOS_BIN), "-v", str(NAGIOS_ETC / "nagios.cfg")])),
            ("Nagios plugins executable", (NAGIOS_PLUGINS / "check_ping").is_file() and os.access(NAGIOS_PLUGINS / "check_ping", os.X_OK)),
            ("Nagios authentication file present", (NAGIOS_ETC / "htpasswd.users").is_file() and (NAGIOS_ETC / "htpasswd.users").stat().st_size > 0),
            ("Nginx service active", start_service_status("nginx")),
            ("Nginx configuration valid", command_succeeds(["nginx", "-t"])),
            (
                "TBCOps local readiness endpoint",
                http_status(f"http://127.0.0.1:{values['APP_PORT']}/api/health?mode=readiness", False),
            ),
            ("Nagios private HTTP endpoint", http_status(f"http://127.0.0.1:{values['NAGIOS_HTTP_PORT']}/nagios/", False)),
        )
    )
    if values.get("NGINX_ENABLE_SSL", "true").lower() == "true":
        checks.extend(
            (
                (
                    "TBCOps public HTTPS endpoint",
                    http_status(f"https://{values['OPSKNIGHT_DOMAIN']}/api/health?mode=readiness", True),
                ),
                ("Nagios public HTTPS endpoint", http_status(f"https://{values['NAGIOS_DOMAIN']}/nagios/", True)),
            )
        )
    failures = 0
    for label, passed in checks:
        print(f"{'PASS' if passed else 'FAIL'}  {label}")
        failures += not passed
    if failures:
        die(f"Health checks failed: {failures} check(s).")
    print("All required health checks passed.")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def backup(values: dict[str, str]) -> Path:
    require_root("backup")
    require_command("pg_restore")
    backup_root = Path(values["BACKUP_DIR"])
    destination = backup_root / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.mkdir(mode=0o700)
    os.chmod(backup_root, 0o700)
    os.chmod(destination, 0o700)
    try:
        dump_path = destination / "opsknight-postgres.dump"
        with dump_path.open("wb") as dump_file:
            compose(values, "exec", "-T", "opsknight-db", "pg_dump", "-U", values["POSTGRES_USER"], "-d", values["POSTGRES_DB"], "-Fc", stdout=dump_file)
        if dump_path.stat().st_size == 0:
            die("Database dump is empty.")
        run(["pg_restore", "--list", str(dump_path)], stdout=subprocess.DEVNULL)
        shutil.copy2(ENV_FILE, destination / ".env")
        shutil.copy2(ROOT / "docker-compose.yml", destination / "docker-compose.yml")
        with tarfile.open(destination / "project-config.tar.gz", "w:gz") as archive:
            for item in ("Makefile", "scripts", "templates", "configs/nagios", "ansible", "docs", ".yamllint", ".ansible-lint"):
                if not (ROOT / item).exists():
                    continue
                archive.add(
                    ROOT / item,
                    arcname=item,
                    filter=lambda info: None
                    if "__pycache__" in Path(info.name).parts or info.name.endswith(".pyc")
                    else info,
                )
        system_archive = destination / "system-config.tar"
        existing_paths = [path for path in SYSTEM_CONFIG_PATHS if path.exists()]
        if not existing_paths:
            die("No system configuration files were available for the backup.")
        with tarfile.open(system_archive, "w") as archive:
            for path in existing_paths:
                archive.add(path, arcname=str(path).lstrip("/"))
        for path in destination.iterdir():
            if path.is_file():
                os.chmod(path, 0o600)
        with (destination / "SHA256SUMS").open("w", encoding="ascii") as manifest:
            for name in (
                "opsknight-postgres.dump",
                ".env",
                "docker-compose.yml",
                "project-config.tar.gz",
                "system-config.tar",
            ):
                path = destination / name
                manifest.write(f"{sha256_file(path)}  {name}\n")
        os.chmod(destination / "SHA256SUMS", 0o600)
        with tarfile.open(destination / "project-config.tar.gz", "r:gz") as archive:
            archive.getmembers()
        with tarfile.open(system_archive, "r") as archive:
            archive.getmembers()
        verify_backup(destination)
    except (OSError, DeploymentError):
        warn(f"Backup incomplete; inspect restricted directory {destination}.")
        raise
    log(f"Verified backup created at {destination}.")
    cutoff = time.time() - int(values["BACKUP_RETENTION_DAYS"]) * 86400
    for old in backup_root.iterdir():
        if old.is_dir() and not old.is_symlink() and old.stat().st_mtime < cutoff:
            shutil.rmtree(old)
    log(f"Retention cleanup completed (older than {values['BACKUP_RETENTION_DAYS']} days).")
    return destination


def verify_backup(destination: Path) -> None:
    manifest_path = destination / "SHA256SUMS"
    if not manifest_path.is_file():
        die("Selected backup is missing its checksum manifest.")
    for line in manifest_path.read_text(encoding="ascii").splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64})  ([A-Za-z0-9._-]+)", line)
        if not match:
            die("Backup checksum manifest contains an invalid entry.")
        expected, name = match.groups()
        path = destination / name
        if not path.is_file() or not secrets.compare_digest(sha256_file(path), expected.lower()):
            die(f"Backup checksum verification failed for {name}.")


def restore(values: dict[str, str]) -> None:
    require_root("restore")
    backup_path = Path(os.environ.get("BACKUP", ""))
    if not os.environ.get("BACKUP"):
        die("Select a backup explicitly: sudo make restore BACKUP=/path/to/timestamped-backup CONFIRM_RESTORE=YES.")
    if not backup_path.is_absolute() or not backup_path.is_dir():
        die("BACKUP must be an existing absolute backup directory.")
    if os.environ.get("CONFIRM_RESTORE") != "YES":
        die("Restore is destructive. Re-run with CONFIRM_RESTORE=YES after reviewing the selected backup.")
    dump = backup_path / "opsknight-postgres.dump"
    if not dump.is_file() or dump.stat().st_size == 0:
        die("Selected backup is missing its database dump.")
    verify_backup(backup_path)
    run(["pg_restore", "--list", str(dump)], stdout=subprocess.DEVNULL)
    log("Creating a pre-restore backup before changing the database.")
    backup(values)
    compose(values, "stop", "opsknight-app")
    compose(values, "exec", "-T", "opsknight-db", "dropdb", "--if-exists", "-U", values["POSTGRES_USER"], values["POSTGRES_DB"])
    compose(values, "exec", "-T", "opsknight-db", "createdb", "-U", values["POSTGRES_USER"], values["POSTGRES_DB"])
    with dump.open("rb") as input_file:
        subprocess_result = subprocess.run(
            [
                "docker", "compose", "--env-file", str(ENV_FILE), "-f", str(ROOT / "docker-compose.yml"),
                "exec", "-T", "opsknight-db", "pg_restore", "-U", values["POSTGRES_USER"], "-d",
                values["POSTGRES_DB"], "--no-owner", "--exit-on-error",
            ],
            env={**os.environ, **values, "POSTGRES_PASSWORD_URLENCODED": urllib.parse.quote(values["POSTGRES_PASSWORD"], safe="")},
            stdin=input_file,
            check=False,
        )
    if subprocess_result.returncode:
        die(f"Database restore command failed with exit code {subprocess_result.returncode}.")
    compose(values, "up", "-d", "opsknight-app")
    log("Database restore completed. Configuration archives were preserved and not overwritten.")


def update_env_values(updates: dict[str, str]) -> None:
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    found: set[str] = set()
    output: list[str] = []
    for line in lines:
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match.group(1) in updates:
            key = match.group(1)
            output.append(f"{key}={updates[key]}")
            found.add(key)
        else:
            output.append(line)
    for key, value in updates.items():
        if key not in found:
            output.append(f"{key}={value}")
    atomic_write(ENV_FILE, "\n".join(output) + "\n", 0o600)


def rotate_password(values: dict[str, str], mode: str) -> None:
    require_root(f"rotate-{mode}-password")
    if mode == "nagios":
        auth = NAGIOS_ETC / "htpasswd.users"
        if not auth.is_file():
            die("Nagios authentication file is missing.")
        password = os.environ.get("NAGIOS_NEW_PASSWORD")
        if password is None:
            password = getpass.getpass("New Nagios password: ")
            confirmation = getpass.getpass("Confirm new Nagios password: ")
            if password != confirmation:
                die("Passwords did not match.")
        if len(password) < 16 or "\n" in password or "\r" in password:
            die("Nagios password must be at least 16 characters and contain no newline.")
        backup_existing(auth)
        run(["htpasswd", "-iB", str(auth), values["NAGIOS_ADMIN_USER"]], input_text=password + "\n", stdout=subprocess.DEVNULL)
        os.chmod(auth, 0o640)
        log("Nagios password rotated; password was not displayed.")
        return
    if mode != "opsknight":
        die("Usage: rotate-password nagios|opsknight")
    backup_existing(ENV_FILE)
    update_env_values({"API_KEY_SECRET": secrets.token_hex(32), "NEXTAUTH_SECRET": secrets.token_hex(32)})
    new_values = init_env()
    compose(new_values, "up", "-d", "--force-recreate", "opsknight-app")
    log("API_KEY_SECRET and NEXTAUTH_SECRET rotated; existing sessions/API clients may need re-authentication.")
    log("ENCRYPTION_KEY was not changed.")


def clean() -> None:
    for path in ROOT.glob(".work-*"):
        if path.is_dir() and path.parent == ROOT:
            print(f"Removing {path}")
            shutil.rmtree(path)
    print("No Docker volumes, databases, or backups were removed.")


def show_help() -> None:
    print(
        "Available commands:\n"
        "  make up                         Install and start all services\n"
        "  make setup                      Backward-compatible alias for up\n"
        "  make check                      Validate server and .env\n"
        "  make install-dependencies       Install Ubuntu dependencies\n"
        "  make install-docker             Install Docker Engine and Compose\n"
        "  make install-nagios             Build and install Nagios\n"
        "  make install-ansible            Install pinned Ansible and collections\n"
        "  make add-client IP=... NAME=... USER=...\n"
        "  make ansible-ssh-key | ansible-copy-key\n"
        "  make ansible-ping | ansible-nrpe [LIMIT=... CHECK=1]\n"
        "  make ansible-verify | remove-client NAME=...\n"
        "  make ansible-syntax | ansible-lint | yamllint\n"
        "  make configure                  Configure Nagios and Nginx\n"
        "  make configure-ssl              Obtain/renew HTTPS certificates\n"
        "  make deploy | update            Deploy or update containers\n"
        "  make status | health | logs     Inspect services\n"
        "  make start | stop | restart     Control services\n"
        "  make backup | restore           Back up or restore database\n"
        "  make clean                      Remove project-local build scratch\n"
        "  make rotate-nagios-password\n"
        "  make rotate-tbcops-secrets\n"
    )


def command_up() -> None:
    values = ready_env()
    require_root("up")
    check_prerequisites()
    install_dependencies(values)
    install_docker(values)
    install_nagios(values)
    configure_nginx(values)
    configure_ssl(values)
    deploy(ready_env(), "deploy")
    if values.get("ENABLE_ANSIBLE_CLIENTS", "false").lower() == "true":
        install_ansible(values)
        run_ansible_playbook(values, "playbooks/site.yml")
    log("All infrastructure components are installed and running.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Provision and manage TBCOps + Nagios.")
    parser.add_argument(
        "command",
        choices=(
            "help", "check", "install-dependencies", "install-docker", "install-nagios",
            "configure-nginx", "configure-nagios", "configure-ssl", "configure",
            "deploy", "start", "stop", "restart", "status", "logs", "health", "backup",
            "restore", "update", "rotate-nagios-password", "rotate-opsknight-secrets",
            "rotate-tbcops-secrets",
            "install-ansible", "ansible-ssh-key", "ansible-copy-key", "add-client",
            "ansible-ping", "ansible-nrpe", "ansible-verify", "remove-client",
            "ansible-syntax", "ansible-lint", "yamllint",
            "clean", "up", "setup",
        ),
    )
    args = parser.parse_args()
    command = args.command
    if command == "help":
        show_help()
        return 0
    if command == "clean":
        clean()
        return 0
    if command == "ansible-syntax":
        ansible_syntax()
        return 0
    if command == "ansible-lint":
        ansible_lint()
        return 0
    if command == "yamllint":
        yaml_lint()
        return 0
    values = ready_env() if command != "check" else init_env()
    if command == "check":
        validate_env(values)
        check_prerequisites()
    elif command == "install-dependencies":
        install_dependencies(values)
    elif command == "install-docker":
        install_docker(values)
    elif command == "install-nagios":
        install_nagios(values)
    elif command == "configure-nagios":
        configure_nagios(values)
    elif command == "configure-nginx":
        configure_nginx(values)
    elif command == "configure-ssl":
        configure_ssl(values)
    elif command == "configure":
        configure_nagios(values)
        configure_nginx(values)
    elif command in {"deploy", "update", "start", "stop", "restart"}:
        deploy(values, command)
    elif command == "status":
        status()
    elif command == "logs":
        require_root("logs")
        compose(values, "logs", "--tail=100", "-f", "opsknight-app", "opsknight-db")
    elif command == "health":
        health(values)
    elif command == "backup":
        backup(values)
    elif command == "restore":
        restore(values)
    elif command == "install-ansible":
        install_ansible(values)
    elif command == "ansible-ssh-key":
        generate_ansible_key(values)
    elif command == "ansible-copy-key":
        ansible_copy_key(values)
    elif command == "add-client":
        add_client(values)
    elif command == "ansible-ping":
        ansible_ping(values)
    elif command == "ansible-nrpe":
        run_ansible_playbook(
            values,
            "playbooks/site.yml",
            check_mode=os.environ.get("CHECK", "").lower() in {"1", "true", "yes"},
        )
    elif command == "ansible-verify":
        ansible_verify(values)
    elif command == "remove-client":
        remove_client(values)
    elif command == "rotate-nagios-password":
        rotate_password(values, "nagios")
    elif command in {"rotate-opsknight-secrets", "rotate-tbcops-secrets"}:
        rotate_password(values, "opsknight")
    elif command in {"up", "setup"}:
        command_up()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DeploymentError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
    except OSError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted; check service state before retrying.", file=sys.stderr)
        sys.exit(130)
