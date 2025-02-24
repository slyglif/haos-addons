#!/usr/bin/python3

import json
import logging
import logging.config
import os
import random
import signal
import socket
import threading
import time
import traceback
import yaml

from paho.mqtt import client as mqtt_client
from selectors import DefaultSelector, EVENT_READ
from threading import Condition, RLock, Thread

import hamqtt.devices
import pytedapi
import pytedapi.exceptions

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

        logger.debug(f"Runtime config:")
        for key in sorted(config.keys()):
            logger.debug(f"config['{key}'] = {getattr(self, key)}")


    def catch(self, signum, frame):
        self._shutdown[1].send(b'\0')


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
                logger.error("Failed to connect, return code = %s", rc.getName())

        client = mqtt_client.Client(client_id=mqtt_id, callback_api_version=mqtt_client.CallbackAPIVersion.VERSION2)
        client.on_connect = on_connect
        client.user_data_set(self)
        client.will_set(hamqtt.devices.will_topic, offline)
        logger.debug("MQTT will set on '%s' to '%s'" % (hamqtt.devices.will_topic, offline))
        if self.mqtt_ssl:
            client.tls_set(ca_certs=self.mqtt_ca, certfile=self.mqtt_cert, keyfile=self.mqtt_key)
            client.tls_insecure_set(self.mqtt_verify_tls)
        client.username_pw_set(self.mqtt_username, self.mqtt_password)
        client.connect(self.mqtt_host, self.mqtt_port)
        self.mqtt = client


    def getPause(self):
        with self._runLock:
            return self._pause


    def getRunning(self):
        with self._runLock:
            return self._running


    def setPause(self, pause):
        with self._runLock:
            self._pause = pause
            if not pause:
                self._loopWait.notify()


    def setRunning(self, running):
        with self._runLock:
            self._running = running
            if not running:
                self._loopWait.notify()


    def discover(self):
        discovery = self.tesla.getDiscoveryMessages()
        # Send Discovery
        for message in discovery:
            result = self.mqtt.publish(message['topic'], json.dumps(message['payload']))
            if result[0] == 0:
                logger.info("Discovery sent to '%s'" % message['topic'])
                logger.debug("message = %s", json.dumps(message['payload']))
                logger.info("Sleeping 0.5s to allow HA to process discovery")
            else:
                logger.warn("Failed to send '%s' to '%s'" % (message['topic'], message['payload']))
        time.sleep(0.5)


    def main_loop(self):
        sel = DefaultSelector()
        sel.register(self._shutdown[0], EVENT_READ)
        sel.register(self._ha_status[0], EVENT_READ)
        sel.register(self._update_loop[0], EVENT_READ)

        while True:
            for key, _ in sel.select():
                try:
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
                            self.setPause(False)
                        else:
                            logger.info("Received ha_status offline")
                            self.setPause(True)
                    elif key.fileobj == self._update_loop[0]:
                        self._update_loop[0].recv(1)
                        logger.info("Processing update from timing_loop")
                        self.update(True)
                except tedapi.TEDAPIRateLimitingException as e:
                    self.tedapi_poll_interval += 1
                    logger.warning(e, exc_info=True)
                    logger.warning(f"Increasing poll interval by 1 to {self.tedapi_poll_interval}")
                except TimeoutException as e:
                    # Likely lock timeout, skip interval
                    logger.warning(e, exc_info=True)
                except tedapi.TEDAPIException as e:
                    # Likely fatal, bail out
                    self.setRunning(False)
                    logger.critical(e, exc_info=True)
                    return
                except Exception as e:
                    logger.exception(e)


    def run(self):
        # Setup signal handling
        signal.signal(signal.SIGINT, self.catch)
        signal.signal(signal.SIGTERM, self.catch)

        # Connect to remote services
        self.connect_mqtt()
        self.tedapi = pytedapi.TEDAPI(
            self.tedapi_password,
            cacheexpire=4,
            configexpire=29)
        if not self.tedapi.pw3:
            raise Exception("Powerwall appears to be older than Powerwall 3")

        # Populate Tesla info
        self.tesla = hamqtt.devices.TeslaSystem(self.mqtt_base_topic, self.tedapi, self.tedapi_report_vitals)
        # TODO: Check for commission date being valid

        # TODO: use qos=1 or 2 for initial / unpause, 0 for normal
        self.mqtt.loop_start()
        try:
            timer = threading.Thread(target=self.timing_loop)
            timer.start()
            try:
                self.discover()
                self.update()
                self.main_loop()
            finally:
                self.setRunning(False)
                timer.join()
        except Exception as e:
            logger.exception(e)
        finally:
            self.mqtt.loop_stop()
        return 0


    def timing_loop(self):
        with self._loopWait:
            while self.getRunning():
                self._loopWait.wait(self.tedapi_poll_interval)
                if not self.getPause():
                    self._update_loop[1].send(b'\1')


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



if __name__ == '__main__':
    try:
        app = powerwall3mqtt()
        app.run()
    except Exception as e:
        logger.exception(e)
        exit(1)
    finally:
        logging.shutdown()
