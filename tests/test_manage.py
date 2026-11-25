import tempfile
import unittest
from pathlib import Path

from scripts.manage import (
    DeploymentError,
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
