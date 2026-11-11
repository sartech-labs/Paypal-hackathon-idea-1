# Docker workflows for the SARA demo and the transparent-edge gateway.
#
#   make demo-mode      rebuild the demo image and run it on http://127.0.0.1:8000
#   make gateway-mode   bring up the gateway from deploy/docker-compose.yml
#
# Both read the repository-root .env (copy .env.example to .env first). Gateway mode
# also needs deploy/.env with ORIGIN_URL and ADMIN_API_TOKEN.
#
# Override any variable on the command line, for example: make demo-mode PORT=8001

IMAGE        ?= sara:demo
CONTAINER    ?= sara-demo
VOLUME       ?= sara-storage
PORT         ?= 8000
ENV_FILE     ?= .env
COMPOSE_FILE ?= deploy/docker-compose.yml

.PHONY: demo-mode gateway-mode

# Demo mode: drop any previous demo container and image, rebuild, then run in the
# foreground (Ctrl-C stops it; --rm removes the container).
demo-mode:
	-docker rm -f $(CONTAINER)
	-docker rmi -f $(IMAGE)
	docker build -t $(IMAGE) .
	docker run --rm --name $(CONTAINER) -p $(PORT):8000 --env-file $(ENV_FILE) -v $(VOLUME):/app/storage $(IMAGE)

# Gateway mode: Docker Compose owns the build, port, env files, and volume.
gateway-mode:
	docker compose -f $(COMPOSE_FILE) up -d --build
