#!/usr/bin/python3

import json
import logging
import logging.config
import os
import random
import traceback
import yaml

from paho.mqtt import client as mqtt_client
from pypowerwall.tedapi import TEDAPI
from threading import Condition, RLock, Thread

from hamqtt.devices import TeslaSystem, offline, online, origin, will_topic

# Generate a Client ID with the publish prefix.
mqtt_id = f'powerwall3mqtt-{random.randint(0, 1000)}'
will_topic = "%s/will" % mqtt_id

origin['name'] = 'powerwall3mqtt'
#origin['sw'] = '0.0.0'
#origin['url'] = ''


with open("logger.yaml") as stream:
    try:
        logging.config.dictConfig(yaml.safe_load(stream))
    except yaml.YAMLError as exc:
        print(exc)
        exit(1)

# Setup logging for this module
logger = logging.getLogger(__name__)


class powerwall3mqtt:
    def __init__(self):
        self.mqtt = None
        self.tedapi = None
        self.tesla = None

        self._pause = False
        self._pauseLock = RLock()
        self._loopWait = Condition(self._pauseLock)

        # Logging level
        log_level = os.environ.get('POWERWALL3MQTT_CONFIG_LOGGING_LEVEL', 'WARNING')
        #logging.getHandlerByName('console').setLevel(log_level.upper())

        # TEDApi Info
        self.tedapi_password = os.environ.get('POWERWALL3MQTT_CONFIG_TEDAPI_PASSWORD')
        self.report_vitals = os.environ.get('POWERWALL3MQTT_CONFIG_TEDAPI_REPORT_VITALS', "true").lower() == "true"
        self.poll_interval = int(os.environ.get('POWERWALL3MQTT_CONFIG_TEDAPI_POLL_INTERVAL', 30))

        # MQTT Broker Info
        self.mqtt_broker = os.environ.get('POWERWALL3MQTT_CONFIG_MQTT_BROKER')
        self.mqtt_port = int(os.environ.get('POWERWALL3MQTT_CONFIG_MQTT_PORT'))
        self.mqtt_username = os.environ.get('POWERWALL3MQTT_CONFIG_MQTT_USER')
        self.mqtt_password = os.environ.get('POWERWALL3MQTT_CONFIG_MQTT_PASSWORD')

        # MQTT SSL Info
        self.mqtt_ca = os.environ.get('POWERWALL3MQTT_CONFIG_MQTT_CA')
        self.mqtt_cert = os.environ.get('POWERWALL3MQTT_CONFIG_MQTT_CERT')
        self.mqtt_key = os.environ.get('POWERWALL3MQTT_CONFIG_MQTT_KEY')
        self.mqtt_verify_tls = os.environ.get('POWERWALL3MQTT_CONFIG_MQTT_VERIFY_TLS', False)
        
        # HA discovery path
        self.discovery_prefix = os.environ.get('POWERWALL3MQTT_CONFIG_MQTT_BASE_TOPIC', 'homeassistant')

        if None in (self.tedapi_password, self.mqtt_broker, self.mqtt_port, self.mqtt_username, self.mqtt_password):
            raise Exception("Environment not set")
        if self.poll_interval < 5:
            raise Exception("Polling Interval must be >= 5")
        if self.mqtt_cert != None ^ self.mqtt_key != None:
            raise Exception("MQTT Certifcate and Key are both required")


    def getPause(self):
        with self._pauseLock:
            return self._pause


    def setPause(self, pause):
        with self._pauseLock:
            self._pause = pause


    def connect_mqtt(self):
        def on_ha_status(client, userdata, message):
            if message.payload == b'online':
                with userdata._pauseLock:
                    userdata.setPause(False)
                    userdata._loopWait.notify()
            else:
                userdata.setPause(True)

        def on_connect(client, userdata, flags, rc, properties):
            if rc == 0:
                logger.info("Connected to MQTT Broker!")
                client.message_callback_add(userdata.discovery_prefix + "status", on_ha_status)
                client.subscribe(userdata.discovery_prefix + "status")
            else:
                logger.error("Failed to connect, return code %d", rc)

        #@self.mqtt.topic_callback("homeassistant/status")
        client = mqtt_client.Client(client_id=mqtt_id, callback_api_version=mqtt_client.CallbackAPIVersion.VERSION2)
        client.on_connect = on_connect
        client.user_data_set(self)
        client.will_set(will_topic, offline)
        if self.mqtt_ca != None:
            client.tls_set(ca_certs=self.mqtt_ca, certfile=self.mqtt_cert, keyfile=self.mqtt_key)
            client.tls_insecure_set(self.mqtt_verify_tls)
        client.username_pw_set(self.mqtt_username, self.mqtt_password)
        client.connect(self.mqtt_broker, self.mqtt_port)
        self.mqtt = client

    def discover(self):
        discovery = self.tesla.getDiscoveryMessages()
        # Send Discovery
        for message in discovery:
            #print("Sending discovery. Topic = '%s', Payload = %r" % (message['topic'], message['payload']))
            result = self.mqtt.publish(message['topic'], json.dumps(message['payload']))
            if result[0] == 0:
                logger.info("Discovery sent to '%s'" % message['topic'])
            else:
                logger.warn("Failed to send '%s' to '%s'" % (message['topic'], message['payload']))


    def update(self, update=False):
        if update:
            self.tesla.update()
        sysstate = self.tesla.getStateMessages()
        for message in sysstate:
            result = self.mqtt.publish(message['topic'], json.dumps(message['payload']))
            if result[0] == 0:
                logger.info("Sent message to '%s'" % message['topic'])
            else:
                logger.warn("Failed to send '%s' to '%s'" % (message['topic'], message['payload']))


    def run(self):
        # Connect to remote services
        self.connect_mqtt()
        self.tedapi = TEDAPI(self.tedapi_password)

        # Populate Tesla info
        self.tesla = TeslaSystem(self.discovery_prefix, self.tedapi, self.report_vitals)

        # TODO: use qos=1 or 2 for initial / unpause, 0 for normal
        self.mqtt.loop_start()
        try:
            self.discover()
            self.update()

            self._loopWait.acquire()
            while True:
                if self._loopWait.wait(self.poll_interval):
                    if not self.getPause():
                        self.discover()
                if not self.getPause():
                    self.update(True)
            self._loopWait.release()
        except KeyboardInterrupt:
            logger.debug("Graceful shutdown")
        except Exception as e:
            traceback.print_exc()
            logger.exception(e)
        finally:
            self.mqtt.loop_stop()
        return 0



if __name__ == '__main__':
    app = powerwall3mqtt()
    app.run()
    logging.shutdown()