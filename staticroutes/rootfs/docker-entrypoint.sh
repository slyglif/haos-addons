#!/usr/bin/env bashio

bashio::log.info "Preparing to start..."

# Check if HA supervisor started
# Workaround for:
# - https://github.com/home-assistant/supervisor/issues/3884
bashio::config.require 'prune'

cd /app
exec python3 staticroutes.py