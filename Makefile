# sermon.guide — top-level dev shortcuts.
#
# These targets wrap docker compose against infra/docker-compose.yml so the
# usual lifecycle (`make up`, `make down`, ...) works from the repo root
# without the contributor needing to remember the compose flags. See
# infra/AGENTS.md for what each service does and the env-var conventions.

COMPOSE := docker compose -f infra/docker-compose.yml --env-file infra/.env

.DEFAULT_GOAL := help
.PHONY: help up down logs ps nuke env backup restore

help:
	@echo "sermon.guide — make targets"
	@echo ""
	@echo "  up       Start all infra services (postgres, redis, milvus + deps)."
	@echo "           Blocks until every service reports healthy."
	@echo "  down     Stop all infra services. Volumes preserved."
	@echo "  logs     Tail logs from all services. Ctrl-C to exit."
	@echo "  ps       Show service status."
	@echo "  nuke     Stop services AND destroy all named volumes. Irreversible."
	@echo "  backup   Back up Postgres + Milvus + MinIO originals to BACKUP_DIR"
	@echo "           (host, default infra/backups/). Non-destructive."
	@echo "  restore  Restore the newest backup into a fresh/up stack."
	@echo "           Override with BACKUP=<timestamp>. See docs/BACKUP_RESTORE.md."
	@echo ""
	@echo "On first run, infra/.env is created from infra/.env.example."

env:
	@if [ ! -f infra/.env ]; then \
	  cp infra/.env.example infra/.env; \
	  echo "Created infra/.env from infra/.env.example"; \
	fi

up: env
	$(COMPOSE) up -d --wait

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f

ps:
	$(COMPOSE) ps

nuke:
	$(COMPOSE) down -v

# Backup/restore (Phase 28). The heavy lifting lives in infra/scripts/ so the
# Makefile stays a thin wrapper; both read creds from infra/.env and write to
# BACKUP_DIR (host path, default infra/backups/, gitignored). BACKUP_DIR and
# (for restore) BACKUP pass straight through the env. Runbook + restore drill:
# docs/BACKUP_RESTORE.md.
backup:
	infra/scripts/backup.sh

restore:
	infra/scripts/restore.sh
