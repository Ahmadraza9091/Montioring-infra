import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.manage import (
    DeploymentError,
    add_client,
    append_inventory_host,
    canonical_client_address,
    inventory_hosts,
    valid_client_address,
    remove_config_directives,
    render_nginx,
    set_config_directives,
    ubuntu_version_supported,
    validate_env,
)


def valid_test_environment():
    return {
        "PROJECT_NAME": "tbcops-monitoring",
        "TIMEZONE": "Asia/Karachi",
        "ADMIN_EMAIL": "admin@example.net",
        "OPSKNIGHT_DOMAIN": "tbcops.test",
        "NAGIOS_DOMAIN": "nagios.test",
        "POSTGRES_USER": "opsknight",
        "POSTGRES_PASSWORD": "safe-test-password",
        "POSTGRES_DB": "opsknight_db",
        "API_KEY_SECRET": "api-secret",
        "NEXTAUTH_SECRET": "auth-secret",
        "ENCRYPTION_KEY": "encryption-secret",
        "NAGIOS_ADMIN_USER": "nagiosadmin",
        "NAGIOS_ADMIN_PASSWORD": "nagios-password",
        "NGINX_ENABLE_SSL": "false",
        "OPSKNIGHT_PUBLIC_URL": "http://tbcops.test",
        "NEXTAUTH_URL": "http://tbcops.test",
        "APP_PORT": "3001",
        "POSTGRES_PORT": "5432",
        "NAGIOS_HTTP_PORT": "8080",
        "ALLOW_SSH_PORT": "22",
        "ALLOW_HTTP_PORT": "80",
        "ALLOW_HTTPS_PORT": "443",
        "NAGIOS_VERSION": "4.5.9",
        "NAGIOS_PLUGINS_VERSION": "2.4.12",
        "BACKUP_RETENTION_DAYS": "7",
        "FIREWALL_ENABLE": "false",
    }


class EnvironmentValidationTests(unittest.TestCase):
    def test_domain_free_test_hostnames_are_valid(self):
        validate_env(valid_test_environment())

    def test_http_test_url_is_rejected_when_ssl_is_enabled(self):
        values = valid_test_environment()
        values["NGINX_ENABLE_SSL"] = "true"
        with self.assertRaises(DeploymentError):
            validate_env(values)

    def test_ubuntu_versions_22_04_and_newer_are_allowed(self):
        for version in ("22.04", "22.10", "24.04", "26.04"):
            with self.subTest(version=version):
                self.assertTrue(ubuntu_version_supported(version))

    def test_ubuntu_versions_older_than_22_04_are_rejected(self):
        for version in ("20.04", "21.10", "not-a-version"):
            with self.subTest(version=version):
                self.assertFalse(ubuntu_version_supported(version))

    def test_optional_ansible_settings_are_validated(self):
        values = valid_test_environment()
        values["NRPE_PORT"] = "5666"
        values["NRPE_VERSION"] = "4.1.3"
        values["NAGIOS_SERVER_IP"] = "10.0.0.10"
        values["ANSIBLE_USER"] = "ubuntu"
        values["NRPE_CLIENT_SERVICES"] = "nginx,sshd"
        validate_env(values)

    def test_invalid_ansible_service_name_is_rejected(self):
        values = valid_test_environment()
        values["NRPE_CLIENT_SERVICES"] = "nginx; touch /tmp/pwned"
        with self.assertRaises(DeploymentError):
            validate_env(values)

    def test_nagios_server_must_be_an_ip_address(self):
        values = valid_test_environment()
        values["NAGIOS_SERVER_IP"] = "nagios.internal"
        with self.assertRaises(DeploymentError):
            validate_env(values)


class AnsibleInventoryTests(unittest.TestCase):
    def test_client_address_accepts_ip_and_hostname_but_not_shell_syntax(self):
        for value in ("192.0.2.10", "2001:db8::10", "web01.internal"):
            with self.subTest(value=value):
                self.assertTrue(valid_client_address(value))
        for value in ("", "web01;id", "bad..host", "not a host"):
            with self.subTest(value=value):
                self.assertFalse(valid_client_address(value))

    def test_ip_address_canonicalization_detects_equivalent_ipv6_forms(self):
        self.assertEqual(
            canonical_client_address("2001:0db8:0:0::10"),
            canonical_client_address("2001:db8::10"),
        )

    def test_inventory_insert_preserves_group_vars_and_parses_quoted_path(self):
        content = "[nrpe_clients]\n\n[nrpe_clients:vars]\nansible_user=ubuntu\n"
        updated = append_inventory_host(
            content,
            "web01 ansible_host=192.0.2.10 ansible_ssh_private_key_file='/tmp/ssh key'",
        )

        self.assertIn("web01 ansible_host=192.0.2.10", updated)
        self.assertLess(updated.index("web01"), updated.index("[nrpe_clients:vars]"))
        self.assertEqual(
            inventory_hosts(updated),
            [("web01", {"ansible_host": "192.0.2.10", "ansible_ssh_private_key_file": "/tmp/ssh key"})],
        )

    def test_add_client_writes_validated_entry_and_rejects_duplicate_address(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory = Path(directory) / "hosts.ini"
            values = {
                "ANSIBLE_INVENTORY": str(inventory),
                "ANSIBLE_SSH_KEY_PATH": str(Path(directory) / "private key"),
                "ANSIBLE_USER": "ubuntu",
            }
            with patch.dict(
                os.environ,
                {"IP": "192.0.2.10", "NAME": "web01", "CLIENT_USER": "ubuntu"},
            ):
                add_client(values)
                self.assertEqual(inventory_hosts(inventory.read_text(encoding="utf-8"))[0][0], "web01")
                with self.assertRaises(DeploymentError):
                    add_client({**values, "ANSIBLE_USER": "deploy"})


class NginxRenderingTests(unittest.TestCase):
    def test_http_test_mode_proxies_without_tls_redirect(self):
        values = valid_test_environment()
        template = Path("templates/opsknight.nginx.conf.template")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "opsknight.conf"
            render_nginx(template, output, values, tls=False)
            rendered = output.read_text(encoding="utf-8")

        self.assertIn("server_name tbcops.test;", rendered)
        self.assertIn("proxy_pass http://127.0.0.1:3001;", rendered)
        self.assertNotIn("return 301 https://", rendered)
        self.assertNotIn("listen 443", rendered)

    def test_http_test_mode_keeps_nagios_on_its_private_upstream(self):
        values = valid_test_environment()
        template = Path("templates/nagios.nginx.conf.template")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "nagios.conf"
            render_nginx(template, output, values, tls=False)
            rendered = output.read_text(encoding="utf-8")

        self.assertIn("server_name nagios.test;", rendered)
        self.assertIn("proxy_pass http://127.0.0.1:8080/nagios/;", rendered)
        self.assertNotIn("return 301 https://", rendered)
        self.assertNotIn("listen 443", rendered)


class NagiosConfigTests(unittest.TestCase):
    def test_cgi_auth_directives_are_removed_from_main_config(self):
        main_config = (
            "log_file=/var/log/nagios/nagios.log\n"
            "use_authentication=1\n"
            "authorized_for_all_hosts=nagiosadmin\n"
            "cfg_dir=/usr/local/nagios/etc/servers\n"
        )

        updated = remove_config_directives(
            main_config,
            ("use_authentication", "authorized_for_all_hosts"),
        )

        self.assertNotIn("use_authentication=", updated)
        self.assertNotIn("authorized_for_all_hosts=", updated)
        self.assertIn("log_file=/var/log/nagios/nagios.log", updated)
        self.assertIn("cfg_dir=/usr/local/nagios/etc/servers", updated)

    def test_cgi_auth_directives_are_written_to_cgi_config(self):
        cgi_config = "use_authentication=0\nauthorized_for_all_hosts=*\n"

        updated = set_config_directives(
            cgi_config,
            {"use_authentication": "1", "authorized_for_all_hosts": "nagiosadmin"},
        )

        self.assertIn("use_authentication=1", updated)
        self.assertIn("authorized_for_all_hosts=nagiosadmin", updated)
        self.assertEqual(updated.count("use_authentication="), 1)


if __name__ == "__main__":
    unittest.main()
