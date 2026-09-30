.PHONY: install dev test backend-test frontend-build

install:
	pip install -r backend/requirements.txt
	cd frontend && npm install

dev:
	docker compose up --build

test: backend-test

backend-test:
	cd backend && python -m pytest -q

frontend-build:
	cd frontend && npm run build
