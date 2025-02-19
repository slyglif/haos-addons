#!/usr/bin/python3

import json
import logging
import logging.config
import os
import random
import signal
import socket
import threading
import traceback
import yaml

from paho.mqtt import client as mqtt_client
from pypowerwall.tedapi import TEDAPI
from selectors import DefaultSelector, EVENT_READ
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
        self._running = True
        self._runLock = RLock()
        self._loopWait = Condition(self._runLock)
        self._shutdown = socket.socketpair()
        self._ha_status = socket.socketpair()
        self._update_loop = socket.socketpair()

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

        # Use ENV vars for overrides
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


    def catch(self, signum, frame):
        self._shutdown[1].send(b'\0')


    def getPause(self):
        with self._runLock:
            return self._pause


    def setPause(self, pause):
        with self._runLock:
            self._pause = pause
            if not pause:
                self._loopWait.notify()


    def getRunning(self):
        with self._runLock:
            return self._running


    def setRunning(self, running):
        with self._runLock:
            self._running = running
            if not running:
                self._loopWait.notify()


    def connect_mqtt(self):
        def on_ha_status(client, userdata, message):
            if message.payload == b'online':
                userdata._ha_status[1].send(b'\1')
            else:
                userdata._ha_status[1].send(b'\0')

        def on_connect(client, userdata, flags, rc, properties):
            if rc == 0:
                logger.info("Connected to MQTT Broker '%s:%s'" % (userdata.mqtt_host, userdata.mqtt_port))
                topic = userdata.mqtt_base_topic + "/status"
                client.message_callback_add(topic, on_ha_status)
                client.subscribe(topic)
                logger.info("Subscribed to MQTT topic '%s'" % topic)
            else:
                logger.error("Failed to connect, return code %d", rc)

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


    def timing_loop(self):
        with self._loopWait:
            while self.getRunning():
                self._loopWait.wait(self.tedapi_poll_interval)
                if not self.getPause():
                    self._update_loop[1].send(b'\1')


    def main_loop(self):
        sel = DefaultSelector()
        sel.register(self._shutdown[0], EVENT_READ)
        sel.register(self._ha_status[0], EVENT_READ)
        sel.register(self._update_loop[0], EVENT_READ)

        while True:
            for key, _ in sel.select():
                if key.fileobj == self._shutdown[0]:
                    self._shutdown[0].recv(1)
                    logger.info("Received shutdown signal")
                    self.setRunning(False)
                    return
                elif key.fileobj == self._ha_status[0]:
                    cmd = self._ha_status[0].recv(1)
                    if cmd == b'\01':
                        logger.info("Received ha_status online")
                        self.discover()
                        # Wait a couple seconds for HA to process discovery
                        self.setPause(False)
                    else:
                        logger.info("Received ha_status offline")
                        self.setPause(True)
                elif key.fileobj == self._update_loop[0]:
                    self._update_loop[0].recv(1)
                    logger.info("Processing update from timing_loop")
                    self.update(True)


    def run(self):
        # Setup signal handling
        signal.signal(signal.SIGINT, self.catch)
        signal.signal(signal.SIGTERM, self.catch)

        # Connect to remote services
        self.connect_mqtt()
        self.tedapi = TEDAPI(self.tedapi_password)

        # Populate Tesla info
        self.tesla = hamqtt.devices.TeslaSystem(self.mqtt_base_topic, self.tedapi, self.tedapi_report_vitals)

        # TODO: use qos=1 or 2 for initial / unpause, 0 for normal
        self.mqtt.loop_start()
        try:
            timer = threading.Thread(target=self.timing_loop)
            timer.start()
            try:
                self.discover()
                # TODO: Add delay?
                self.update()
                self.main_loop()
            finally:
                timer.join()
        except Exception as e:
            logger.exception(e)
            traceback.print_exc()
        finally:
            self.mqtt.loop_stop()
        return 0



if __name__ == '__main__':
    try:
        app = powerwall3mqtt()
        app.run()
    except Exception as e:
        logger.exception(e)
        traceback.print_exc()
    logging.shutdown()