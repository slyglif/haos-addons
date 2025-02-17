#!/usr/bin/env bashio

bashio::log.info "Preparing to start..."

# Check if HA supervisor started
# Workaround for:
# - https://github.com/home-assistant/supervisor/issues/3884
# - https://github.com/zigbee2mqtt/hassio-zigbee2mqtt/issues/387
bashio::config.require 'data_path'

#if bashio::config.has_value 'watchdog'; then
#    export POWERWALL3MQTT_WATCHDOG="$(bashio::config 'watchdog')"
#    bashio::log.info "Enabled POWERWALL3MQTT watchdog with value '$POWERWALL3MQTT_WATCHDOG'"
#fi

# Expose addon configuration through environment variables.
function export_config() {
    local key=${1}
    local subkey

    if bashio::config.is_empty "${key}"; then
        return
    fi

    for subkey in $(bashio::jq "$(bashio::config "${key}")" 'keys[]'); do
        export "POWERWALL3MQTT_CONFIG_$(bashio::string.upper "${key}")_$(bashio::string.upper "${subkey}")=$(bashio::config "${key}.${subkey}")"
    done
}

export_config 'tedapi'
export_config 'mqtt'

if (bashio::config.is_empty 'mqtt' || ! (bashio::config.has_value 'mqtt.server' || bashio::config.has_value 'mqtt.user' || bashio::config.has_value 'mqtt.password')) && bashio::var.has_value "$(bashio::services 'mqtt')"; then
    export POWERWALL3MQTT_CONFIG_MQTT_BROKER="$(bashio::services 'mqtt' 'host')"
    export POWERWALL3MQTT_CONFIG_MQTT_PORT="$(bashio::services 'mqtt' 'port')"
    export POWERWALL3MQTT_CONFIG_MQTT_USER="$(bashio::services 'mqtt' 'username')"
    export POWERWALL3MQTT_CONFIG_MQTT_PASSWORD="$(bashio::services 'mqtt' 'password')"
fi

bashio::log.info "Starting POWERWALL3MQTT..."
cd /app
exec python3 powerwall3mqtt.py