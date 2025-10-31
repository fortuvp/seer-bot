# Variables (override via environment):
#   IMAGE, CONTAINER, ENV_FILE
image := env_var_or_default('IMAGE', 'seer-bot')
container := env_var_or_default('CONTAINER', 'seer-bot')
env_file := env_var_or_default('ENV_FILE', '.env')

# Show usage and examples
help:
    @echo "Seer Bot - just commands"
    @echo ""
    @echo "Variables (override via ENV_FILE=/path/to/.env IMAGE=name CONTAINER=name):"
    @echo "  ENV_FILE  = {{env_file}}"
    @echo "  IMAGE     = {{image}}"
    @echo "  CONTAINER = {{container}}"
    @echo ""
    @echo "Recipes:"
    @echo "  just pull      # git pull only"
    @echo "  just rebuild   # git pull, remove old image/container, build, run"
    @echo "  just restart   # restart container with new ENV_FILE (no rebuild)"
    @echo ""
    @echo "Examples:"
    @echo "  just rebuild ENV_FILE=/abs/path/to/.env"

# Pull latest changes only
pull:
    git pull


# Rebuild and run: pull latest, clean old artifacts, build, and start detached
rebuild:
    git pull
    sudo docker rm -f {{container}} || true
    sudo docker rmi -f {{image}} || true
    sudo docker build -t {{image}} .
    sudo docker run -d --restart unless-stopped --name {{container}} --env-file "{{env_file}}" {{image}}
# Restart container with new env file without rebuilding
restart:
    sudo docker rm -f {{container}} || true
    sudo docker run -d --restart unless-stopped --name {{container}} --env-file "{{env_file}}" {{image}}


# (end)
