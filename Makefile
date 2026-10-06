.PHONY: start stop logs status

start:
	./start_up.sh

stop:
	docker compose stop

logs:
	docker compose logs --follow

status:
	docker compose ps --all
