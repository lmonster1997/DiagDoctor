# ============================================================
# DiagDoctor — Makefile
# ============================================================
# Quick reference:
#   make up            Start all services
#   make down          Stop & remove all services
#   make logs          Tail logs of all services
#   make ps            Show service status
#   make demo-migrate  Run Alembic migrations in demo-backend
#   make demo-seed     Seed demo data (admin user, sample project)
# ============================================================

.PHONY: help up down logs ps demo-migrate demo-seed clean restart doctor-logs doctor-restart

# Default target
help:
	@echo "DiagDoctor — Development Commands"
	@echo "================================="
	@echo "  make up             Start all services (detached)"
	@echo "  make down           Stop & remove all services"
	@echo "  make logs           Tail logs of all services"
	@echo "  make ps             Show service status"
	@echo "  make restart        Restart all services"
	@echo "  make demo-migrate   Run Alembic migrations"
	@echo "  make demo-seed      Seed demo data"
	@echo "  make doctor-logs    Tail logs of doctor-api"
	@echo "  make doctor-restart Restart doctor-api service"
	@echo "  make clean          Stop services & remove volumes"

# ---- Lifecycle ----

up:
	docker compose up -d

down:
	docker compose down

restart:
	docker compose restart

logs:
	docker compose logs -f

ps:
	docker compose ps

clean:
	docker compose down -v

# ---- Demo App Commands ----

demo-migrate:
	docker compose exec demo-backend alembic upgrade head

demo-seed:
	docker compose exec demo-backend python scripts/seed.py

# ---- Doctor Agent Commands ----

doctor-logs:
	docker compose logs -f doctor-api

doctor-restart:
	docker compose restart doctor-api

# ---- Convenience: full setup from scratch ----
setup: up
	@echo "Waiting for postgres to be healthy..."
	@sleep 5
	$(MAKE) demo-migrate
	$(MAKE) demo-seed
	@echo "✅ Setup complete! Open http://localhost:3000"
