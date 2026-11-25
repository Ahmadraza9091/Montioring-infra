import tempfile
import unittest
from pathlib import Path

from scripts.manage import DeploymentError, render_nginx, validate_env


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


if __name__ == "__main__":
    unittest.main()
