SHELL := /bin/sh
.DEFAULT_GOAL := help
.PHONY: help check install-dependencies install-docker install-nagios configure \
	configure-nginx configure-nagios configure-ssl deploy start stop restart status \
	logs health backup restore update rotate-opsknight-secrets rotate-nagios-password \
	clean setup up rotate-tbcops-secrets

export BACKUP CONFIRM_RESTORE

help: ## Show available commands
	python3 scripts/manage.py help

check: ## Initialize .env if needed and validate local prerequisites/configuration
	python3 scripts/manage.py check

install-dependencies: ## Install Ubuntu build, web, and firewall dependencies
	python3 scripts/manage.py install-dependencies

install-docker: ## Install Docker Engine and Compose from the official repository
	python3 scripts/manage.py install-docker

install-nagios: ## Build and install Nagios Core and official plugins
	python3 scripts/manage.py install-nagios

configure: ## Configure Nagios and Nginx
	python3 scripts/manage.py configure

configure-nginx: ## Render and validate Nginx reverse-proxy sites
	python3 scripts/manage.py configure-nginx

configure-nagios: ## Configure Nagios authentication, Apache, and monitoring
	python3 scripts/manage.py configure-nagios

configure-ssl: ## Validate DNS and obtain/enable Let's Encrypt certificates
	python3 scripts/manage.py configure-ssl

deploy: ## Pull and start application containers without deleting volumes
	python3 scripts/manage.py deploy

start: ## Start managed services and containers
	python3 scripts/manage.py start

stop: ## Stop managed services and containers (data volumes remain)
	python3 scripts/manage.py stop

restart: ## Restart managed services and containers
	python3 scripts/manage.py restart

status: ## Show service and container status
	python3 scripts/manage.py status

logs: ## Follow TBCOps and PostgreSQL container logs
	python3 scripts/manage.py logs

health: ## Run required local and configured public health checks
	python3 scripts/manage.py health

backup: ## Create and verify a timestamped database/configuration backup
	python3 scripts/manage.py backup

restore: ## Restore selected backup; requires BACKUP=... CONFIRM_RESTORE=YES
	python3 scripts/manage.py restore

update: ## Pull and roll out the configured TBCOps image
	python3 scripts/manage.py update

rotate-tbcops-secrets: ## Rotate TBCOps API and session secrets (not encryption key)
	python3 scripts/manage.py rotate-opsknight-secrets

rotate-opsknight-secrets: rotate-tbcops-secrets ## Legacy alias for rotate-tbcops-secrets

rotate-nagios-password: ## Prompt for and rotate the Nagios administrator password
	python3 scripts/manage.py rotate-nagios-password

clean: ## Remove only abandoned project-local build scratch (never production data)
	python3 scripts/manage.py clean

up: ## Install and start all components on a fresh supported Ubuntu server
	python3 scripts/manage.py up

setup: up ## Backward-compatible alias for up
