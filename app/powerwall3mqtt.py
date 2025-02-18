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

import hamqtt.devices
from hamqtt.devices import offline, online

# Generate a Client ID with the publish prefix.
mqtt_id = f'powerwall3mqtt-{random.randint(0, 1000)}'

hamqtt.devices.will_topic = "%s/will" % mqtt_id
hamqtt.devices.origin['name'] = 'powerwall3mqtt'
#hamqtt.devices.origin['sw'] = '0.0.0'
#hamqtt.devices.origin['url'] = ''


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

        # Parse the config file
        config = {
            'log_level': 'WARNING',
            'tedapi_report_vitals': False,
            'tedapi_poll_interval': 30,
            'tedapi_password': None,
            'mqtt_base_topic': 'homeassistant',
            'mqtt_host': None,
            'mqtt_port': 1883,
            'mqtt_username': None,
            'mqtt_password': None,
            'mqtt_verify_tls': False,
            'mqtt_ssl': False,
            'mqtt_ca': None,
            'mqtt_cert': None,
            'mqtt_key': None
        }
        try:
            config = config | json.load(open('/data/options.json', 'r'))
        except:
            pass

        for k in config.keys():
            if type(config[k]) is bool:
                setattr(self, k, bool(os.environ.get('POWERWALL3MQTT_CONFIG_%s' % k.upper(), config[k])))
            elif type(config[k]) is int:
                setattr(self, k, int(os.environ.get('POWERWALL3MQTT_CONFIG_%s' % k.upper(), config[k])))
            else:
                setattr(self, k, os.environ.get('POWERWALL3MQTT_CONFIG_%s' % k.upper(), config[k]))


        # Set the logging level
        logging.getHandlerByName('console').setLevel(self.log_level.upper())

        if None in (self.tedapi_password, self.mqtt_host, self.mqtt_port, self.mqtt_username, self.mqtt_password):
            raise Exception("Environment not set")
        if self.tedapi_poll_interval < 5:
            raise Exception("Polling Interval must be >= 5")
        if (self.mqtt_cert != None) ^ (self.mqtt_key != None):
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
                logger.info("Connected to MQTT Broker '%s:%s'" % (userdata.mqtt_host, userdata.mqtt_port))
                client.message_callback_add(userdata.mqtt_base_topic + "status", on_ha_status)
                client.subscribe(userdata.mqtt_base_topic + "status")
            else:
                logger.error("Failed to connect, return code %d", rc)

        #@self.mqtt.topic_callback("homeassistant/status")
        client = mqtt_client.Client(client_id=mqtt_id, callback_api_version=mqtt_client.CallbackAPIVersion.VERSION2)
        client.on_connect = on_connect
        client.user_data_set(self)
        client.will_set(hamqtt.devices.will_topic, offline)
        logger.debug("MQTT will set on '%s' to '%s'" % (hamqtt.devices.will_topic, offline))
        if self.mqtt_ssl:
            client.tls_set(ca_certs=self.mqtt_ca, certfile=self.mqtt_cert, keyfile=self.mqtt_key)
            client.tls_insecure_set(self.mqtt_verify_tls)
        client.username_pw_set(self.mqtt_username, self.mqtt_password)
        logger.debug("MQTT user set to '%s'" % self.mqtt_username)
        client.connect(self.mqtt_host, self.mqtt_port)
        self.mqtt = client

    def discover(self):
        discovery = self.tesla.getDiscoveryMessages()
        # Send Discovery
        for message in discovery:
            #print("Sending discovery. Topic = '%s', Payload = %r" % (message['topic'], message['payload']))
            result = self.mqtt.publish(message['topic'], json.dumps(message['payload']))
            if result[0] == 0:
                logger.info("Discovery sent to '%s'" % message['topic'])
                logger.debug("message = %s", json.dumps(message['payload']))
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
                logger.debug("message = %s", json.dumps(message['payload']))
            else:
                logger.warn("Failed to send '%s' to '%s'" % (message['topic'], message['payload']))


    def run(self):
        # Connect to remote services
        self.connect_mqtt()
        self.tedapi = TEDAPI(self.tedapi_password)

        # Populate Tesla info
        self.tesla = hamqtt.devices.TeslaSystem(self.mqtt_base_topic, self.tedapi, self.tedapi_report_vitals)

        # TODO: use qos=1 or 2 for initial / unpause, 0 for normal
        self.mqtt.loop_start()
        try:
            self.discover()
            self.update()

            self._loopWait.acquire()
            while True:
                if self._loopWait.wait(self.tedapi_poll_interval):
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