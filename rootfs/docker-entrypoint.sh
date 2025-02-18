#!/usr/bin/env bashio

bashio::log.info "Preparing to start..."

# Check if HA supervisor started
# Workaround for:
# - https://github.com/home-assistant/supervisor/issues/3884
bashio::config.require 'tedapi_password'

#if bashio::config.has_value 'watchdog'; then
#    export POWERWALL3MQTT_WATCHDOG="$(bashio::config 'watchdog')"
#    bashio::log.info "Enabled POWERWALL3MQTT watchdog with value '$POWERWALL3MQTT_WATCHDOG'"
#fi

export_if_not_set() {
    if ( ! (bashio::config.has_value "${1}_${2}") && bashio::var.has_value "$(bashio::services ${1})" ); then
        export POWERWALL3MQTT_CONFIG_${1^^}_${2^^}="$(bashio::services ${1} ${2})"
    fi
}

# Expose addon configuration through environment variables.
export_if_not_set mqtt host
export_if_not_set mqtt port
export_if_not_set mqtt ssl
export_if_not_set mqtt username
export_if_not_set mqtt password

bashio::log.info "Starting Powerwall3MQTT..."
cd /app
exec python3 powerwall3mqtt.py