SHELL := /bin/sh
.DEFAULT_GOAL := help
.PHONY: help check install-dependencies install-docker install-nagios install-ansible configure \
	configure-nginx configure-nagios configure-ssl deploy start stop restart status \
	logs health backup restore update rotate-opsknight-secrets rotate-nagios-password \
	clean setup up rotate-tbcops-secrets ansible-ssh-key ansible-copy-key add-client \
	ansible-ping ansible-nrpe ansible-verify remove-client ansible-syntax ansible-lint yamllint

export BACKUP CONFIRM_RESTORE IP NAME LIMIT CHECK
export CLIENT_USER = $(if $(filter command line,$(origin USER)),$(USER),)

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

install-ansible: ## Install pinned Ansible core, lint tools, and collections
	python3 scripts/manage.py install-ansible

ansible-ssh-key: ## Generate or secure the Ansible SSH key
	python3 scripts/manage.py ansible-ssh-key

ansible-copy-key: ## Copy the SSH key to inventory clients (interactive password prompts)
	python3 scripts/manage.py ansible-copy-key

add-client: ## Add a monitored client: make add-client IP=... NAME=... USER=...
	python3 scripts/manage.py add-client

ansible-ping: ## Ping inventory clients over SSH
	python3 scripts/manage.py ansible-ping

ansible-nrpe: ## Configure clients and register in Nagios; supports LIMIT= and CHECK=1
	python3 scripts/manage.py ansible-nrpe

ansible-verify: ## Verify check_nrpe from the Nagios server
	python3 scripts/manage.py ansible-verify

remove-client: ## Remove a client: make remove-client NAME=...
	python3 scripts/manage.py remove-client

ansible-syntax: ## Run Ansible playbook syntax-check
	python3 scripts/manage.py ansible-syntax

ansible-lint: ## Run ansible-lint production profile
	python3 scripts/manage.py ansible-lint

yamllint: ## Lint Ansible YAML
	python3 scripts/manage.py yamllint

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
