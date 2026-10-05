.PHONY: up stop logs status

up:
	./start_up.sh

stop:
	docker compose stop

logs:
	docker compose logs --follow

status:
	docker compose ps --all
