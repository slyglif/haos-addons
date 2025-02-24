# pyPowerWall - Tesla TEDAPI Class
# -*- coding: utf-8 -*-
"""
 Tesla TEADAPI Class
 
 This module allows you to access the Tesla Powerwall Gateway 
 TEDAPI on 192.168.91.1 as used by the Tesla One app.

 Class:
    TEDAPI(gw_pwd: str, pwcacheexpire: int = 5, timeout: int = 5,
              pwconfigexpire: int = 5, host: str = GW_IP) - Initialize TEDAPI
    
 Parameters:
    gw_pwd - Powerwall Gateway Password
    debug - Enable Debug Output
    pwcacheexpire - Cache Expiration in seconds
    timeout - API Timeout in seconds
    pwconfigexpire - Configuration Cache Expiration in seconds
    host - Powerwall Gateway IP Address (default: 192.168.91.1)

 Functions:
    get_din() - Get the DIN from the Powerwall Gateway
    get_config() - Get the Powerwall Gateway Configuration
    get_status() - Get the Powerwall Gateway Status
    connect() - Connect to the Powerwall Gateway
    backup_time_remaining() - Get the time remaining in hours
    battery_level() - Get the battery level as a percentage
    vitals() - Use tedapi data to create a vitals dictionary
    get_firmware_version() - Get the Powerwall Firmware Version
    get_battery_blocks() - Get list of Powerwall Battery Blocks
    get_components() - Get the Powerwall 3 Device Information
    get_battery_block(din) - Get the Powerwall 3 Battery Block Information
    get_pw3_vitals() - Get the Powerwall 3 Vitals Information
    get_device_controller() - Get the Powerwall Device Controller Status

 Note:
    This module requires access to the Powerwall Gateway. You can add a route to
    using the command: sudo route add -host 192.168.91.1 <Powerwall_IP>
    The Powerwall Gateway password is required to access the TEDAPI.

 Derivitate Author: Chris Giard
 Date: xx xxx 2025
 For more information see https://github.com/slyglif/haos-addons/powerwall3mqtt

 Original Author: Jason A. Cox
 Date: 1 Jun 2024
 For more information see https://github.com/jasonacox/pypowerwall
"""

# Imports
import logging
import math
import sys
import time
import json
import requests
from cachetools import cached, TTLCache
from requests.packages.urllib3.exceptions import InsecureRequestWarning
from threading import RLock
from . import exceptions
from . import tedapi_pb2

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

# TEDAPI Fixed Gateway IP Address
GW_IP = "192.168.91.1"

# Rate Limit Codes
BUSY_CODES = [429, 503]

# Setup Logging
logger = logging.getLogger(__name__)

# Utility Functions
def lookup(data, keylist):
    """
    Lookup a value in a nested dictionary or return None if not found.
    data - nested dictionary
    keylist - list of keys to traverse
    """
    for key in keylist:
        if isinstance(data, dict):
            data = data.get(key)
        else:
            return None
    return data


###
### TimeoutRLock class
###
class TimeoutRLock():
    """
    A wrapper around RLock that sets a default timeout
    for acquire() and 'with lock:' calls. Usage is
    identical to RLock, except the constructor takes
    an extra optional parameter of 'timeout'.
    """
    timeout = None
    lock    = None

    # Semi-transparent __init__ method
    def __init__(self, timeout=None, *args, **kwargs):
        self.timeout = timeout
        self.lock    = RLock(*args, **kwargs)

    # Context management protocol __enter__ method
    def __enter__(self, *args, **kwargs):
        rc = self.lock.acquire(timeout=self.timeout)
        if rc is False:
            raise TimeoutError(f"Could not acquire lock within " 
                               f"specified timeout of {self.timeout}s") 
        return rc

    def __exit__(self, *args, **kwargs):
        return self.lock.release()

    # Semi-transparent acquire call
    def acquire(self, *args, **kwargs):
        if 'timeout' not in kwargs.keys():
            kwargs['timeout'] = self.timeout
        return self.lock.acquire(*args, **kwargs)

    # Transparent method calls for rest of RLock's public methods
    def release(self, *args, **kwargs):
        return self.lock.release(*args, **kwargs)

    def locked(self, *args, **kwargs):
        return self.lock.locked(*args, **kwargs)


###
### TEDAPI class
###
class TEDAPI:
    """
    Parameters:
       gw_pwd - Powerwall Gateway Password
       host - Powerwall Gateway IP Address (default: 192.168.91.1)
       cacheexpire - Cache Expiration in seconds
       configexpire - Configuration Cache Expiration in seconds
       timeout - API Timeout in seconds
       cooldown - Time in seconds to suspend calls if the Powerwall returns a
                  BUSY code

    Functions:
       connect() - Connect to the Powerwall Gateway if not already connected
       reconnect() - Reconnect to the Powerwall Gateway
       request() - Send a simple GET request to the Powerwall Gateway
       post() - Send a POST to the Powerwall Gateway
       get_din() - Get the DIN from the Powerwall Gateway
       get_config() - Get the Powerwall Gateway Configuration
       get_status() - Get the Powerwall Gateway Status
       vitals() - Use tedapi data to create a vitals dictionary
       get_firmware_version() - Get the Powerwall Firmware Version
       get_battery_blocks() - Get list of Powerwall Battery Blocks
       get_components() - Get the Powerwall 3 Device Information
       get_battery_block(din) - Get the Powerwall 3 Battery Block Information
       get_pw3_vitals() - Get the Powerwall 3 Vitals Information
       get_device_controller() - Get the Powerwall Device Controller Status
       battery_level() - Get the battery level as a percentage

    Note:
       This module requires access to the Powerwall Gateway. You can add a route to
       using the command: sudo route add -host 192.168.91.1 <Powerwall_IP>
       The Powerwall Gateway password is required to access the TEDAPI.
    """
    def __init__(self,
            gw_pwd: str,
            host: str = GW_IP,
            cacheexpire: int = 5,
            configexpire: int = 5,
            timeout: int = 5,
            cooldown: int = 300) -> None:
        if not gw_pwd:
            raise ValueError("Missing gw_pwd")
        self.gw_pwd = gw_pwd
        self.gw_ip = host
        self.timeout = timeout
        self.cooldown = cooldown

        # _config used for get_config and get_firmware
        self._config = TTLCache(maxsize=4, ttl=configexpire)

        # _cache used for all other API calls except get_din
        self._cache = TTLCache(maxsize=16, ttl=cacheexpire)

        self.din = None
        self.pw3 = False # Powerwall 3 Gateway only supports TEDAPI
        self.pwcooldown = 0

        self._apiLock = TimeoutRLock(timeout)
        self._batteryBlockLock = TimeoutRLock(timeout)
        self._componentsLock = TimeoutRLock(timeout)
        self._configLock = TimeoutRLock(timeout)
        self._controllerLock = TimeoutRLock(timeout)
        self._dinLock = TimeoutRLock(timeout)
        self._firmwareLock = TimeoutRLock(timeout)
        self._statusLock = TimeoutRLock(timeout)
        self._vitalsLock = TimeoutRLock(timeout)

        # Connect to Powerwall Gateway
        self.connect()

    # TEDAPI Functions

    def connect(self):
        """
        Connect to the Powerwall Gateway if not already connected
        Parameters:
            None
        Returns:
            str: The Powerwall Gateway's DIN
        """
        if self.din == None:
            return self.reconnect()
        return self.din


    def reconnect(self):
        """
        Reconnect to the Powerwall Gateway
        Parameters:
            None
        Returns:
            str: The Powerwall Gateway's DIN
        Raises:
            Exception
        """
        # Test IP Connection to Powerwall Gateway
        logger.debug(f"Testing Connection to Powerwall Gateway: {self.gw_ip}")
        url = f'https://{self.gw_ip}'
        self.din = None
        try:
            resp = requests.get(url, verify=False, timeout=5)
            if resp.status_code != 200:
                # Connected but appears to be Powerwall 3
                logger.debug("Detected Powerwall 3 Gateway")
                self.pw3 = True
            return self.get_din()
        except Exception as e:
            logger.error(f"Unable to connect to Powerwall Gateway {self.gw_ip}")
            logger.error("Please verify your your host has a route to the Gateway.")
            logger.error(f"Error Details: {e}")
            raise


    def checkHttpResponse(self, r: requests.Response):
        match r.status_code:
            case 429 | 503:
                # Rate limited - Switch to cooldown mode
                self.pwcooldown = time.perf_counter() + self.cooldown
                raise TEDAPIRateLimitingException(self.pwcooldown)
            case 403:
                raise TEDAPIAccessDeniedException()
            case 200:
                pass
            case _:
                raise TEDAPIException(r.status_code)


    def request(self, path, force=False):
        """
        Make a simple HTTP GET request to the Powerwall Gateway, converting
        some HTTP status codes to exceptions
        Parameters:
            path (str): The URI path
            force (bool): Force a query from the API, default false
        Returns:
            requests.Response: The HTTP resposne
        Raises:
            TEDAPIRateLimitedException
            TEDAPIRateLimitingException
            TEDAPIAccessDeniedException
            TEDAPIException
        """
        if not force and self.pwcooldown > time.perf_counter():
            raise TEDAPIRateLimitedException()
        with self._apiLock:
            url = f'https://{self.gw_ip}/%s' % path
            r = requests.get(url,
                verify=False,
                auth=('Tesla_Energy_Device', self.gw_pwd),
                timeout=self.timeout)
            self.checkHttpResponse(r)
            return r


    def post(self, path, force=False, headers=None, data=None):
        """
        Make an HTTP POST request to the Powerwall Gateway, converting
        some HTTP status codes to exceptions
        Parameters:
            path (str): The URI path
            force (bool): Force a query from the API, default false
            headers (dict): Passed through to requests, default None
            data (dict): Passed through to requests, default None
        Returns:
            requests.Response: The HTTP resposne
        Raises:
            TEDAPIRateLimitedException
            TEDAPIRateLimitingException
            TEDAPIAccessDeniedException
            TEDAPIException
        """
        if not force and self.pwcooldown > time.perf_counter():
            raise TEDAPIRateLimitedException()
        with self._apiLock:
            url = f'https://{self.gw_ip}/%s' % path
            r = requests.post(url,
                verify=False,
                auth=('Tesla_Energy_Device', self.gw_pwd),
                headers=headers,
                data=data,
                timeout=self.timeout)
            self.checkHttpResponse(r)
            self.pwcooldown = time.perf_counter()
            return r


    def get_din(self, force=False):
        """
        Get the DIN of the Powerwall Gateway
        Parameters:
            force (bool): Force a query of the API, default False
        Returns:
            str: The Powerwall Gateway's DIN
        Raises:
            TEDAPIException
        """
        with self._dinLock:
            if not force and self.din != None:
                logger.debug("Using Cached din")
                return self.din
            logger.debug("Fetching din from Powerwall...")
            r = self.request("tedapi/din", force=force)
            if self.din not in (None, r.text):
                raise TEDAPIException(f"DIN changed from '{self.din}' to '{r.text}'")
            self.din = r.text
            return self.din


    def get_config(self,force=False):
        """
        Get the Powerwall Gateway Configuration
        Parameters:
            force (bool): Force a query of the API, default False
        Returns:
            dict: Raw dictionary from Powerwall Gateway:
                auto_meter_update (bool)
                battery_blocks (list of dicts)
                bridge_inverter (dict)
                client_protocols (dict)
                credentials (list)
                customer (dict)
                default_real_mode (string)
                dio (dict)
                enable_inverter_meter_readings (bool)
                freq_shift_load_shed (dict)
                freq_support_parameters (dict)
                industrial_networks (dict)
                installer (dict)
                island_config (dict)
                island_contactor_controller (dict)
                logging (dict)
                meters (list of dicts)
                site_info (dict)
                solar (dict)
                solars (list)
                strategy (dict)
                test_timers (dict)
                vin (string): "__MODEL__--__SERIAL__"
        Raises:
            json.JSONDecodeError
            TEDAPIException
        """
        with self._configLock:
            if not force:
                try:
                    value = self._config["config"]
                    logger.debug("Using Cached config")
                    return value
                except:
                    pass

            # Check Connection
            self.connect()

            # Fetch Configuration from Powerwall
            logger.debug("Get Configuration from Powerwall")

            # Build Protobuf to fetch config
            pb = tedapi_pb2.Message()
            pb.message.deliveryChannel = 1
            pb.message.sender.local = 1
            pb.message.recipient.din = self.din  # DIN of Powerwall
            pb.message.config.send.num = 1
            pb.message.config.send.file = "config.json"
            pb.tail.value = 1

            r = self.post(
                "tedapi/v1",
                headers={'Content-type': 'application/octet-string'},
                data=pb.SerializeToString())

            # Decode response
            tedapi = tedapi_pb2.Message()
            tedapi.ParseFromString(r.content)
            payload = tedapi.message.config.recv.file.text
            data = json.loads(payload)
            logger.debug(f"Configuration: {data}")
            self._config["config"] = data
            return data


    def get_status(self, force=False):
        """
        Get the Powerwall Gateway Status
        Parameters:
            force (bool): Force a query of the API, default False
        Returns:
            dict: Raw dictionary from Powerwall Gateway:
                control (dict)
                    alerts (dict)
                    batteryBlocks (list of dicts)
                    islanding (dict)
                    meterAggregates (list of dicts)
                    pvInverters (list)
                    siteShutdown (dict)
                    systemStatus (dict)
                esCan (dict)
                    bus (dict)
                        ISLANDER (dict)
                        MSA (dict)
                        PINV (list of dicts)
                        POD (list of dicts)
                        PVAC (list of dicts)
                        PVS (list of dicts)
                        SYNC (dict)
                        THC (list of dicts)
                    enumeration (None)
                    firmwareUpdate (dict)
                        isUpdating (bool)
                        msa (None)
                        powerwalls (None)
                        pvInverters (None)
                        syn (None)
                    inverterSelfTests (None)
                    phaseDetection (None)
                neurio (dict)
                    isDetectingWiredMeters (bool)
                    pairings (list)
                    readings (list)
                pw3Can (dict)
                    firmwareUpdate (dict)
                        isUpdating (bool)
                        progress (None)
                system (dict)
                    sitemanagerStatus (dict)
                        isRunning (bool)
                    time (timestamp)
                    updateUrgencyCheck (None)
        Raises:
            json.JSONDecodeError
            TEDAPIException
        """
        with self._statusLock:
            if not force:
                try:
                    value = self._cache["status"]
                    logger.debug("Using Cached status")
                    return value
                except:
                    pass

            # Check Connection
            self.connect()

            # Fetch Current Status from Powerwall
            logger.debug("Get Status from Powerwall")

            # Build Protobuf to fetch status
            pb = tedapi_pb2.Message()
            pb.message.deliveryChannel = 1
            pb.message.sender.local = 1
            pb.message.recipient.din = self.din  # DIN of Powerwall
            pb.message.payload.send.num = 2
            pb.message.payload.send.payload.value = 1
            pb.message.payload.send.payload.text = " query DeviceControllerQuery {\n  control {\n    systemStatus {\n        nominalFullPackEnergyWh\n        nominalEnergyRemainingWh\n    }\n    islanding {\n        customerIslandMode\n        contactorClosed\n        microGridOK\n        gridOK\n    }\n    meterAggregates {\n      location\n      realPowerW\n    }\n    alerts {\n      active\n    },\n    siteShutdown {\n      isShutDown\n      reasons\n    }\n    batteryBlocks {\n      din\n      disableReasons\n    }\n    pvInverters {\n      din\n      disableReasons\n    }\n  }\n  system {\n    time\n    sitemanagerStatus {\n      isRunning\n    }\n    updateUrgencyCheck  {\n      urgency\n      version {\n        version\n        gitHash\n      }\n      timestamp\n    }\n  }\n  neurio {\n    isDetectingWiredMeters\n    readings {\n      serial\n      dataRead {\n        voltageV\n        realPowerW\n        reactivePowerVAR\n        currentA\n      }\n      timestamp\n    }\n    pairings {\n      serial\n      shortId\n      status\n      errors\n      macAddress\n      isWired\n      modbusPort\n      modbusId\n      lastUpdateTimestamp\n    }\n  }\n  pw3Can {\n    firmwareUpdate {\n      isUpdating\n      progress {\n         updating\n         numSteps\n         currentStep\n         currentStepProgress\n         progress\n      }\n    }\n  }\n  esCan {\n    bus {\n      PVAC {\n        packagePartNumber\n        packageSerialNumber\n        subPackagePartNumber\n        subPackageSerialNumber\n        PVAC_Status {\n          isMIA\n          PVAC_Pout\n          PVAC_State\n          PVAC_Vout\n          PVAC_Fout\n        }\n        PVAC_InfoMsg {\n          PVAC_appGitHash\n        }\n        PVAC_Logging {\n          isMIA\n          PVAC_PVCurrent_A\n          PVAC_PVCurrent_B\n          PVAC_PVCurrent_C\n          PVAC_PVCurrent_D\n          PVAC_PVMeasuredVoltage_A\n          PVAC_PVMeasuredVoltage_B\n          PVAC_PVMeasuredVoltage_C\n          PVAC_PVMeasuredVoltage_D\n          PVAC_VL1Ground\n          PVAC_VL2Ground\n        }\n        alerts {\n          isComplete\n          isMIA\n          active\n        }\n      }\n      PINV {\n        PINV_Status {\n          isMIA\n          PINV_Fout\n          PINV_Pout\n          PINV_Vout\n          PINV_State\n          PINV_GridState\n        }\n        PINV_AcMeasurements {\n          isMIA\n          PINV_VSplit1\n          PINV_VSplit2\n        }\n        PINV_PowerCapability {\n          isComplete\n          isMIA\n          PINV_Pnom\n        }\n        alerts {\n          isComplete\n          isMIA\n          active\n        }\n      }\n      PVS {\n        PVS_Status {\n          isMIA\n          PVS_State\n          PVS_vLL\n          PVS_StringA_Connected\n          PVS_StringB_Connected\n          PVS_StringC_Connected\n          PVS_StringD_Connected\n          PVS_SelfTestState\n        }\n        alerts {\n          isComplete\n          isMIA\n          active\n        }\n      }\n      THC {\n        packagePartNumber\n        packageSerialNumber\n        THC_InfoMsg {\n          isComplete\n          isMIA\n          THC_appGitHash\n        }\n        THC_Logging {\n          THC_LOG_PW_2_0_EnableLineState\n        }\n      }\n      POD {\n        POD_EnergyStatus {\n          isMIA\n          POD_nom_energy_remaining\n          POD_nom_full_pack_energy\n        }\n        POD_InfoMsg {\n            POD_appGitHash\n        }\n      }\n      MSA {\n        packagePartNumber\n        packageSerialNumber\n        MSA_InfoMsg {\n          isMIA\n          MSA_appGitHash\n          MSA_assemblyId\n        }\n        METER_Z_AcMeasurements {\n          isMIA\n          lastRxTime\n          METER_Z_CTA_InstRealPower\n          METER_Z_CTA_InstReactivePower\n          METER_Z_CTA_I\n          METER_Z_VL1G\n          METER_Z_CTB_InstRealPower\n          METER_Z_CTB_InstReactivePower\n          METER_Z_CTB_I\n          METER_Z_VL2G\n        }\n        MSA_Status {\n          lastRxTime\n        }\n      }\n      SYNC {\n        packagePartNumber\n        packageSerialNumber\n        SYNC_InfoMsg {\n          isMIA\n          SYNC_appGitHash\n        }\n        METER_X_AcMeasurements {\n          isMIA\n          isComplete\n          lastRxTime\n          METER_X_CTA_InstRealPower\n          METER_X_CTA_InstReactivePower\n          METER_X_CTA_I\n          METER_X_VL1N\n          METER_X_CTB_InstRealPower\n          METER_X_CTB_InstReactivePower\n          METER_X_CTB_I\n          METER_X_VL2N\n          METER_X_CTC_InstRealPower\n          METER_X_CTC_InstReactivePower\n          METER_X_CTC_I\n          METER_X_VL3N\n        }\n        METER_Y_AcMeasurements {\n          isMIA\n          isComplete\n          lastRxTime\n          METER_Y_CTA_InstRealPower\n          METER_Y_CTA_InstReactivePower\n          METER_Y_CTA_I\n          METER_Y_VL1N\n          METER_Y_CTB_InstRealPower\n          METER_Y_CTB_InstReactivePower\n          METER_Y_CTB_I\n          METER_Y_VL2N\n          METER_Y_CTC_InstRealPower\n          METER_Y_CTC_InstReactivePower\n          METER_Y_CTC_I\n          METER_Y_VL3N\n        }\n        SYNC_Status {\n          lastRxTime\n        }\n      }\n      ISLANDER {\n        ISLAND_GridConnection {\n          ISLAND_GridConnected\n          isComplete\n        }\n        ISLAND_AcMeasurements {\n          ISLAND_VL1N_Main\n          ISLAND_FreqL1_Main\n          ISLAND_VL2N_Main\n          ISLAND_FreqL2_Main\n          ISLAND_VL3N_Main\n          ISLAND_FreqL3_Main\n          ISLAND_VL1N_Load\n          ISLAND_FreqL1_Load\n          ISLAND_VL2N_Load\n          ISLAND_FreqL2_Load\n          ISLAND_VL3N_Load\n          ISLAND_FreqL3_Load\n          ISLAND_GridState\n          lastRxTime\n          isComplete\n          isMIA\n        }\n      }\n    }\n    enumeration {\n      inProgress\n      numACPW\n      numPVI\n    }\n    firmwareUpdate {\n      isUpdating\n      powerwalls {\n        updating\n        numSteps\n        currentStep\n        currentStepProgress\n        progress\n      }\n      msa {\n        updating\n        numSteps\n        currentStep\n        currentStepProgress\n        progress\n      }\n      sync {\n        updating\n        numSteps\n        currentStep\n        currentStepProgress\n        progress\n      }\n      pvInverters {\n        updating\n        numSteps\n        currentStep\n        currentStepProgress\n        progress\n      }\n    }\n    phaseDetection {\n      inProgress\n      lastUpdateTimestamp\n      powerwalls {\n        din\n        progress\n        phase\n      }\n    }\n    inverterSelfTests {\n      isRunning\n      isCanceled\n      pinvSelfTestsResults {\n        din\n        overall {\n          status\n          test\n          summary\n          setMagnitude\n          setTime\n          tripMagnitude\n          tripTime\n          accuracyMagnitude\n          accuracyTime\n          currentMagnitude\n          timestamp\n          lastError\n        }\n        testResults {\n          status\n          test\n          summary\n          setMagnitude\n          setTime\n          tripMagnitude\n          tripTime\n          accuracyMagnitude\n          accuracyTime\n          currentMagnitude\n          timestamp\n          lastError\n        }\n      }\n    }\n  }\n}\n"
            pb.message.payload.send.code = b'0\201\206\002A\024\261\227\245\177\255\265\272\321r\032\250\275j\305\030\2300\266\022B\242\264pO\262\024vd\267\316\032\f\376\322V\001\f\177*\366\345\333g_/`\v\026\225_qc\023$\323\216y\276~\335A1\022x\002Ap\a_\264\037]\304>\362\356\005\245V\301\177*\b\307\016\246]\037\202\242\353I~\332\317\021\336\006\033q\317\311\264\315\374\036\365s\272\225\215#o!\315z\353\345z\226\365\341\f\265\256r\373\313/\027\037'
            pb.message.payload.send.b.value = "{}"
            pb.tail.value = 1

            r = self.post(
                "tedapi/v1",
                headers={'Content-type': 'application/octet-string'},
                data=pb.SerializeToString())

            # Decode response
            tedapi = tedapi_pb2.Message()
            tedapi.ParseFromString(r.content)
            payload = tedapi.message.payload.recv.text
            data = json.loads(payload)
            logger.debug(f"Status: {data}")
            self._cache["status"] = data
            return data


    def get_device_controller(self, force=False):
        """
        Get the Powerwall Gateway Controller info, which is similar
        to Status but with additional information
        Parameters:
            force (bool): Force a query of the API, default False
        Returns:
            dict: Raw dictionary from Powerwall Gateway:
                control (dict) // From Status
                esCan (dict) // From Status
                neurio (dict) // From Status
                pw3Can (dict) // From Status
                system (dict) // From Status
                components (dict)
                ieee20305 (dict)
                teslaRemoteMete (dict)
        Raises:
            json.JSONDecodeError
            TEDAPIException
        """
        with self._controllerLock:
            if not force:
                try:
                    value = self._cache["controller"]
                    logger.debug("Using Cached controller")
                    return value
                except:
                    pass

            # Check Connection
            self.connect()

            # Fetch Current Status from Powerwall
            logger.debug("Get controller data from Powerwall")

            # Build Protobuf to fetch controller data
            pb = tedapi_pb2.Message()
            pb.message.deliveryChannel = 1
            pb.message.sender.local = 1
            pb.message.recipient.din = self.din  # DIN of Powerwall
            pb.message.payload.send.num = 2
            pb.message.payload.send.payload.value = 1
            pb.message.payload.send.payload.text = 'query DeviceControllerQuery($msaComp:ComponentFilter$msaSignals:[String!]){control{systemStatus{nominalFullPackEnergyWh nominalEnergyRemainingWh}islanding{customerIslandMode contactorClosed microGridOK gridOK disableReasons}meterAggregates{location realPowerW}alerts{active}siteShutdown{isShutDown reasons}batteryBlocks{din disableReasons}pvInverters{din disableReasons}}system{time supportMode{remoteService{isEnabled expiryTime sessionId}}sitemanagerStatus{isRunning}updateUrgencyCheck{urgency version{version gitHash}timestamp}}neurio{isDetectingWiredMeters readings{firmwareVersion serial dataRead{voltageV realPowerW reactivePowerVAR currentA}timestamp}pairings{serial shortId status errors macAddress hostname isWired modbusPort modbusId lastUpdateTimestamp}}teslaRemoteMeter{meters{din reading{timestamp firmwareVersion ctReadings{voltageV realPowerW reactivePowerVAR energyExportedWs energyImportedWs currentA}}firmwareUpdate{updating numSteps currentStep currentStepProgress progress}}detectedWired{din serialPort}}pw3Can{firmwareUpdate{isUpdating progress{updating numSteps currentStep currentStepProgress progress}}enumeration{inProgress}}esCan{bus{PVAC{packagePartNumber packageSerialNumber subPackagePartNumber subPackageSerialNumber PVAC_Status{isMIA PVAC_Pout PVAC_State PVAC_Vout PVAC_Fout}PVAC_InfoMsg{PVAC_appGitHash}PVAC_Logging{isMIA PVAC_PVCurrent_A PVAC_PVCurrent_B PVAC_PVCurrent_C PVAC_PVCurrent_D PVAC_PVMeasuredVoltage_A PVAC_PVMeasuredVoltage_B PVAC_PVMeasuredVoltage_C PVAC_PVMeasuredVoltage_D PVAC_VL1Ground PVAC_VL2Ground}alerts{isComplete isMIA active}}PINV{PINV_Status{isMIA PINV_Fout PINV_Pout PINV_Vout PINV_State PINV_GridState}PINV_AcMeasurements{isMIA PINV_VSplit1 PINV_VSplit2}PINV_PowerCapability{isComplete isMIA PINV_Pnom}alerts{isComplete isMIA active}}PVS{PVS_Status{isMIA PVS_State PVS_vLL PVS_StringA_Connected PVS_StringB_Connected PVS_StringC_Connected PVS_StringD_Connected PVS_SelfTestState}PVS_Logging{PVS_numStringsLockoutBits PVS_sbsComplete}alerts{isComplete isMIA active}}THC{packagePartNumber packageSerialNumber THC_InfoMsg{isComplete isMIA THC_appGitHash}THC_Logging{THC_LOG_PW_2_0_EnableLineState}}POD{POD_EnergyStatus{isMIA POD_nom_energy_remaining POD_nom_full_pack_energy}POD_InfoMsg{POD_appGitHash}}SYNC{packagePartNumber packageSerialNumber SYNC_InfoMsg{isMIA SYNC_appGitHash SYNC_assemblyId}METER_X_AcMeasurements{isMIA isComplete METER_X_CTA_InstRealPower METER_X_CTA_InstReactivePower METER_X_CTA_I METER_X_VL1N METER_X_CTB_InstRealPower METER_X_CTB_InstReactivePower METER_X_CTB_I METER_X_VL2N METER_X_CTC_InstRealPower METER_X_CTC_InstReactivePower METER_X_CTC_I METER_X_VL3N}METER_Y_AcMeasurements{isMIA isComplete METER_Y_CTA_InstRealPower METER_Y_CTA_InstReactivePower METER_Y_CTA_I METER_Y_VL1N METER_Y_CTB_InstRealPower METER_Y_CTB_InstReactivePower METER_Y_CTB_I METER_Y_VL2N METER_Y_CTC_InstRealPower METER_Y_CTC_InstReactivePower METER_Y_CTC_I METER_Y_VL3N}}ISLANDER{ISLAND_GridConnection{ISLAND_GridConnected isComplete}ISLAND_AcMeasurements{ISLAND_VL1N_Main ISLAND_FreqL1_Main ISLAND_VL2N_Main ISLAND_FreqL2_Main ISLAND_VL3N_Main ISLAND_FreqL3_Main ISLAND_VL1N_Load ISLAND_FreqL1_Load ISLAND_VL2N_Load ISLAND_FreqL2_Load ISLAND_VL3N_Load ISLAND_FreqL3_Load ISLAND_GridState isComplete isMIA}}}enumeration{inProgress numACPW numPVI}firmwareUpdate{isUpdating powerwalls{updating numSteps currentStep currentStepProgress progress}msa{updating numSteps currentStep currentStepProgress progress}msa1{updating numSteps currentStep currentStepProgress progress}sync{updating numSteps currentStep currentStepProgress progress}pvInverters{updating numSteps currentStep currentStepProgress progress}}phaseDetection{inProgress lastUpdateTimestamp powerwalls{din progress phase}}inverterSelfTests{isRunning isCanceled pinvSelfTestsResults{din overall{status test summary setMagnitude setTime tripMagnitude tripTime accuracyMagnitude accuracyTime currentMagnitude timestamp lastError}testResults{status test summary setMagnitude setTime tripMagnitude tripTime accuracyMagnitude accuracyTime currentMagnitude timestamp lastError}}}}components{msa:components(filter:$msaComp){partNumber serialNumber signals(names:$msaSignals){name value textValue boolValue timestamp}activeAlerts{name}}}ieee20305{longFormDeviceID polledResources{url name pollRateSeconds lastPolledTimestamp}controls{defaultControl{mRID setGradW opModEnergize opModMaxLimW opModImpLimW opModExpLimW opModGenLimW opModLoadLimW}activeControls{opModEnergize opModMaxLimW opModImpLimW opModExpLimW opModGenLimW opModLoadLimW}}registration{dateTimeRegistered pin}}}'
            pb.message.payload.send.code = b'0\x81\x87\x02B\x01A\x95\x12\xe3B\xd1\xca\x1a\xd3\x00\xf6}\x0bE@/\x9a\x9f\xc0\r\x06%\xac,\x0ej!)\nd\xef\xe67\x8b\xafb\xd7\xf8&\x0b.\xc1\xac\xd9!\x1f\xd6\x83\xffkIm\xf3\\J\xd8\xeeiTY\xde\x7f\xc5xR\x02A\x1dC\x03H\xfb8"\xb0\xe4\xd6\x18\xde\x11\xc45\xb2\xa9VB\xa6J\x8f\x08\x9d\xba\x86\xf1 W\xcdJ\x8c\x02*\x05\x12\xcb{<\x9b\xc8g\xc9\x9d9\x8bR\xb3\x89\xb8\xf1\xf1\x0f\x0e\x16E\xed\xd7\xbf\xd5&)\x92.\x12'
            pb.message.payload.send.b.value = '{"msaComp":{"types" :["PVS","PVAC", "TESYNC", "TEPINV", "TETHC", "STSTSM",  "TEMSA", "TEPINV" ]},\n\t"msaSignals":[\n\t"MSA_pcbaId",\n\t"MSA_usageId",\n\t"MSA_appGitHash",\n\t"MSA_HeatingRateOccurred",\n\t"THC_AmbientTemp",\n\t"METER_Z_CTA_InstRealPower",\n\t"METER_Z_CTA_InstReactivePower",\n\t"METER_Z_CTA_I",\n\t"METER_Z_VL1G",\n\t"METER_Z_CTB_InstRealPower",\n\t"METER_Z_CTB_InstReactivePower",\n\t"METER_Z_CTB_I",\n\t"METER_Z_VL2G"]}'
            pb.tail.value = 1

            r = self.post(
                "tedapi/v1",
                headers={'Content-type': 'application/octet-string'},
                data=pb.SerializeToString())

            # Decode response
            tedapi = tedapi_pb2.Message()
            tedapi.ParseFromString(r.content)
            payload = tedapi.message.payload.recv.text
            data = json.loads(payload)
            logger.debug(f"Controller: {data}")
            self._cache["controller"] = data
            return data


    def get_firmware_version(self, force=False, details=False):
        """
        Get the Powerwall Firmware version info
        Parameters:
            force (bool): Force a query of the API, default False
            details (bool): Return additional system information including
                            gateway part number, serial number, and wireless
                            devices
        Returns:
            str: Version string
            dict:
                gateway (dict)
                    partNumber (str)
                    serialNumber (str)
                din (str)
                version (dict)
                    text (str)
                    githash (str)
                five (str)
                six (str)
                wireless (dict)
                    device (list of dicts)
                        company (str)
                        model (str)
                        fcc_id (str)
                        ic (str)
        Raises:
            json.JSONDecodeError
            TEDAPIException
        """
        with self._firmwareLock:
            payload = None

            if not force:
                try:
                    payload = self._config["firmware"]
                    logger.debug("Using Cached firmware")
                except:
                    pass

            if payload == None:
                # Check Connection
                self.connect()

                # Fetch Current Status from Powerwall
                logger.debug("Get Firmware Version from Powerwall")

                # Build Protobuf to fetch status
                pb = tedapi_pb2.Message()
                pb.message.deliveryChannel = 1
                pb.message.sender.local = 1
                pb.message.recipient.din = self.din  # DIN of Powerwall
                pb.message.firmware.request = ""
                pb.tail.value = 1

                r = self.post(
                    "tedapi/v1",
                    headers={'Content-type': 'application/octet-string'},
                    data=pb.SerializeToString())

                # Decode response
                tedapi = tedapi_pb2.Message()
                tedapi.ParseFromString(r.content)
                payload = {
                    "gateway": {
                        "partNumber": tedapi.message.firmware.system.gateway.partNumber,
                        "serialNumber": tedapi.message.firmware.system.gateway.serialNumber
                    },
                    "din": tedapi.message.firmware.system.din,
                    "version": {
                        "text": tedapi.message.firmware.system.version.text,
                        "githash": tedapi.message.firmware.system.version.githash
                    },
                    "five": tedapi.message.firmware.system.five,
                    "six": tedapi.message.firmware.system.six,
                    "wireless": {
                        "device": []
                    }
                }
                try:
                    for device in tedapi.message.firmware.system.wireless.device:
                        payload["wireless"]["device"].append({
                            "company": device.company.value,
                            "model": device.model.value,
                            "fcc_id": device.fcc_id.value,
                            "ic": device.ic.value
                        })
                except Exception as e:
                    logger.debug(f"Error parsing wireless devices: {e}")
                logger.debug(f"Firmware Version: {payload}")
                self._config["firmware"] = payload

            if details:
                return payload
            else:
                return payload["version"]["text"]


    def get_components(self, force=False):
        """
        Get the Powerwall 3 Device Information
        Parameters:
            force (bool): Force a query of the API, default False
        Returns:
            str: Version string
            dict: Raw dictionary from Powerwall Gateway:
                gateway (dict)
                    partNumber (str)
                    serialNumber (str)
                din (str)
                version (dict)
                    text (str)
                    githash (str)
                five (str)
                six (str)
                wireless (dict)
                    device (list of dicts)
                        company (str)
                        model (str)
                        fcc_id (str)
                        ic (str)
        Raises:
            json.JSONDecodeError
            TEDAPIPowerwallVersionException
            TEDAPIException

        Note: Raises exception on previous Powerwall versions
        """
        if not self.pw3:
            raise TEDAPIPowerwallVersionException()

        with self._componentsLock:
            if not force:
                try:
                    value = self._cache["components"]
                    logger.debug("Using Cached compopnents")
                    return value
                except:
                    pass

            # Check Connection
            self.connect()

            # Fetch Configuration from Powerwall
            logger.debug("Get PW3 Components from Powerwall")

            # Build Protobuf to fetch config
            pb = tedapi_pb2.Message()
            pb.message.deliveryChannel = 1
            pb.message.sender.local = 1
            pb.message.recipient.din = self.din  # DIN of Powerwall
            pb.message.payload.send.num = 2
            pb.message.payload.send.payload.value = 1
            pb.message.payload.send.payload.text = " query ComponentsQuery (\n  $pchComponentsFilter: ComponentFilter,\n  $pchSignalNames: [String!],\n  $pwsComponentsFilter: ComponentFilter,\n  $pwsSignalNames: [String!],\n  $bmsComponentsFilter: ComponentFilter,\n  $bmsSignalNames: [String!],\n  $hvpComponentsFilter: ComponentFilter,\n  $hvpSignalNames: [String!],\n  $baggrComponentsFilter: ComponentFilter,\n  $baggrSignalNames: [String!],\n  ) {\n  # TODO STST-57686: Introduce GraphQL fragments to shorten\n  pw3Can {\n    firmwareUpdate {\n      isUpdating\n      progress {\n         updating\n         numSteps\n         currentStep\n         currentStepProgress\n         progress\n      }\n    }\n  }\n  components {\n    pws: components(filter: $pwsComponentsFilter) {\n      signals(names: $pwsSignalNames) {\n        name\n        value\n        textValue\n        boolValue\n        timestamp\n      }\n      activeAlerts {\n        name\n      }\n    }\n    pch: components(filter: $pchComponentsFilter) {\n      signals(names: $pchSignalNames) {\n        name\n        value\n        textValue\n        boolValue\n        timestamp\n      }\n      activeAlerts {\n        name\n      }\n    }\n    bms: components(filter: $bmsComponentsFilter) {\n      signals(names: $bmsSignalNames) {\n        name\n        value\n        textValue\n        boolValue\n        timestamp\n      }\n      activeAlerts {\n        name\n      }\n    }\n    hvp: components(filter: $hvpComponentsFilter) {\n      partNumber\n      serialNumber\n      signals(names: $hvpSignalNames) {\n        name\n        value\n        textValue\n        boolValue\n        timestamp\n      }\n      activeAlerts {\n        name\n      }\n    }\n    baggr: components(filter: $baggrComponentsFilter) {\n      signals(names: $baggrSignalNames) {\n        name\n        value\n        textValue\n        boolValue\n        timestamp\n      }\n      activeAlerts {\n        name\n      }\n    }\n  }\n}\n"
            pb.message.payload.send.code = b'0\201\210\002B\000\270q\354>\243m\325p\371S\253\231\346~:\032\216~\242\263\207\017L\273O\203u\241\270\333w\233\354\276\246h\262\243\255\261\007\202D\277\353x\023O\022\303\216\264\010-\'i6\360>B\237\236\304\244m\002B\001\023Pk\033)\277\236\342R\264\247g\260u\036\023\3662\354\242\353\035\221\234\027\245\321J\342\345\037q\262O\3446-\353\315m1\237zai0\341\207C4\307\300Z\177@h\335\327\0239\252f\n\206W'
            pb.message.payload.send.b.value = "{\"pwsComponentsFilter\":{\"types\":[\"PW3SAF\"]},\"pwsSignalNames\":[\"PWS_SelfTest\",\"PWS_PeImpTestState\",\"PWS_PvIsoTestState\",\"PWS_RelaySelfTest_State\",\"PWS_MciTestState\",\"PWS_appGitHash\",\"PWS_ProdSwitch_State\"],\"pchComponentsFilter\":{\"types\":[\"PCH\"]},\"pchSignalNames\":[\"PCH_State\",\"PCH_PvState_A\",\"PCH_PvState_B\",\"PCH_PvState_C\",\"PCH_PvState_D\",\"PCH_PvState_E\",\"PCH_PvState_F\",\"PCH_AcFrequency\",\"PCH_AcVoltageAB\",\"PCH_AcVoltageAN\",\"PCH_AcVoltageBN\",\"PCH_packagePartNumber_1_7\",\"PCH_packagePartNumber_8_14\",\"PCH_packagePartNumber_15_20\",\"PCH_packageSerialNumber_1_7\",\"PCH_packageSerialNumber_8_14\",\"PCH_PvVoltageA\",\"PCH_PvVoltageB\",\"PCH_PvVoltageC\",\"PCH_PvVoltageD\",\"PCH_PvVoltageE\",\"PCH_PvVoltageF\",\"PCH_PvCurrentA\",\"PCH_PvCurrentB\",\"PCH_PvCurrentC\",\"PCH_PvCurrentD\",\"PCH_PvCurrentE\",\"PCH_PvCurrentF\",\"PCH_BatteryPower\",\"PCH_AcRealPowerAB\",\"PCH_SlowPvPowerSum\",\"PCH_AcMode\",\"PCH_AcFrequency\",\"PCH_DcdcState_A\",\"PCH_DcdcState_B\",\"PCH_appGitHash\"],\"bmsComponentsFilter\":{\"types\":[\"PW3BMS\"]},\"bmsSignalNames\":[\"BMS_nominalEnergyRemaining\",\"BMS_nominalFullPackEnergy\",\"BMS_appGitHash\"],\"hvpComponentsFilter\":{\"types\":[\"PW3HVP\"]},\"hvpSignalNames\":[\"HVP_State\",\"HVP_appGitHash\"],\"baggrComponentsFilter\":{\"types\":[\"BAGGR\"]},\"baggrSignalNames\":[\"BAGGR_State\",\"BAGGR_OperationRequest\",\"BAGGR_NumBatteriesConnected\",\"BAGGR_NumBatteriesPresent\",\"BAGGR_NumBatteriesExpected\",\"BAGGR_LOG_BattConnectionStatus0\",\"BAGGR_LOG_BattConnectionStatus1\",\"BAGGR_LOG_BattConnectionStatus2\",\"BAGGR_LOG_BattConnectionStatus3\"]}"
            pb.tail.value = 1

            r = self.post("tedapi/v1",
                headers={'Content-type': 'application/octet-string'},
                data=pb.SerializeToString())

            # Decode response
            tedapi = tedapi_pb2.Message()
            tedapi.ParseFromString(r.content)
            payload = tedapi.message.payload.recv.text
            components = json.loads(payload)
            logger.debug(f"Components: {components}")
            self._cache["components"] = components
            return components


    def get_battery_block(self, din, force=False):
        """
        Get the Powerwall 3 Battery Block Information

        Args:
            din (str): DIN of Powerwall 3 to query
            force (bool): Force a refresh of the battery block

        Note: Raises exception on previous Powerwall versions
        """
        if not self.pw3:
            raise TEDAPIPowerwallVersionException()

        key = "%s-block" % din

        with self._batteryBlockLock:
            if not force:
                try:
                    value = self._cache[key]
                    logger.debug("Using Cached battery_block")
                    return value
                except:
                    pass

            # Fetch Battery Block from Powerwall
            logger.debug(f"Get Battery Block from Powerwall ({din})")

            # Build Protobuf to fetch config
            pb = tedapi_pb2.Message()
            pb.message.deliveryChannel = 1
            pb.message.sender.local = 1
            pb.message.sender.din = self.din # DIN of Primary Powerwall 3 / System
            pb.message.recipient.din = din   # DIN of Powerwall of Interest
            pb.message.payload.send.num = 2
            pb.message.payload.send.payload.value = 1
            pb.message.payload.send.payload.text = " query ComponentsQuery (\n  $pchComponentsFilter: ComponentFilter,\n  $pchSignalNames: [String!],\n  $pwsComponentsFilter: ComponentFilter,\n  $pwsSignalNames: [String!],\n  $bmsComponentsFilter: ComponentFilter,\n  $bmsSignalNames: [String!],\n  $hvpComponentsFilter: ComponentFilter,\n  $hvpSignalNames: [String!],\n  $baggrComponentsFilter: ComponentFilter,\n  $baggrSignalNames: [String!],\n  ) {\n  # TODO STST-57686: Introduce GraphQL fragments to shorten\n  pw3Can {\n    firmwareUpdate {\n      isUpdating\n      progress {\n         updating\n         numSteps\n         currentStep\n         currentStepProgress\n         progress\n      }\n    }\n  }\n  components {\n    pws: components(filter: $pwsComponentsFilter) {\n      signals(names: $pwsSignalNames) {\n        name\n        value\n        textValue\n        boolValue\n        timestamp\n      }\n      activeAlerts {\n        name\n      }\n    }\n    pch: components(filter: $pchComponentsFilter) {\n      signals(names: $pchSignalNames) {\n        name\n        value\n        textValue\n        boolValue\n        timestamp\n      }\n      activeAlerts {\n        name\n      }\n    }\n    bms: components(filter: $bmsComponentsFilter) {\n      signals(names: $bmsSignalNames) {\n        name\n        value\n        textValue\n        boolValue\n        timestamp\n      }\n      activeAlerts {\n        name\n      }\n    }\n    hvp: components(filter: $hvpComponentsFilter) {\n      partNumber\n      serialNumber\n      signals(names: $hvpSignalNames) {\n        name\n        value\n        textValue\n        boolValue\n        timestamp\n      }\n      activeAlerts {\n        name\n      }\n    }\n    baggr: components(filter: $baggrComponentsFilter) {\n      signals(names: $baggrSignalNames) {\n        name\n        value\n        textValue\n        boolValue\n        timestamp\n      }\n      activeAlerts {\n        name\n      }\n    }\n  }\n}\n"
            pb.message.payload.send.code = b'0\201\210\002B\000\270q\354>\243m\325p\371S\253\231\346~:\032\216~\242\263\207\017L\273O\203u\241\270\333w\233\354\276\246h\262\243\255\261\007\202D\277\353x\023O\022\303\216\264\010-\'i6\360>B\237\236\304\244m\002B\001\023Pk\033)\277\236\342R\264\247g\260u\036\023\3662\354\242\353\035\221\234\027\245\321J\342\345\037q\262O\3446-\353\315m1\237zai0\341\207C4\307\300Z\177@h\335\327\0239\252f\n\206W'
            pb.message.payload.send.b.value = "{\"pwsComponentsFilter\":{\"types\":[\"PW3SAF\"]},\"pwsSignalNames\":[\"PWS_SelfTest\",\"PWS_PeImpTestState\",\"PWS_PvIsoTestState\",\"PWS_RelaySelfTest_State\",\"PWS_MciTestState\",\"PWS_appGitHash\",\"PWS_ProdSwitch_State\"],\"pchComponentsFilter\":{\"types\":[\"PCH\"]},\"pchSignalNames\":[\"PCH_State\",\"PCH_PvState_A\",\"PCH_PvState_B\",\"PCH_PvState_C\",\"PCH_PvState_D\",\"PCH_PvState_E\",\"PCH_PvState_F\",\"PCH_AcFrequency\",\"PCH_AcVoltageAB\",\"PCH_AcVoltageAN\",\"PCH_AcVoltageBN\",\"PCH_packagePartNumber_1_7\",\"PCH_packagePartNumber_8_14\",\"PCH_packagePartNumber_15_20\",\"PCH_packageSerialNumber_1_7\",\"PCH_packageSerialNumber_8_14\",\"PCH_PvVoltageA\",\"PCH_PvVoltageB\",\"PCH_PvVoltageC\",\"PCH_PvVoltageD\",\"PCH_PvVoltageE\",\"PCH_PvVoltageF\",\"PCH_PvCurrentA\",\"PCH_PvCurrentB\",\"PCH_PvCurrentC\",\"PCH_PvCurrentD\",\"PCH_PvCurrentE\",\"PCH_PvCurrentF\",\"PCH_BatteryPower\",\"PCH_AcRealPowerAB\",\"PCH_SlowPvPowerSum\",\"PCH_AcMode\",\"PCH_AcFrequency\",\"PCH_DcdcState_A\",\"PCH_DcdcState_B\",\"PCH_appGitHash\"],\"bmsComponentsFilter\":{\"types\":[\"PW3BMS\"]},\"bmsSignalNames\":[\"BMS_nominalEnergyRemaining\",\"BMS_nominalFullPackEnergy\",\"BMS_appGitHash\"],\"hvpComponentsFilter\":{\"types\":[\"PW3HVP\"]},\"hvpSignalNames\":[\"HVP_State\",\"HVP_appGitHash\"],\"baggrComponentsFilter\":{\"types\":[\"BAGGR\"]},\"baggrSignalNames\":[\"BAGGR_State\",\"BAGGR_OperationRequest\",\"BAGGR_NumBatteriesConnected\",\"BAGGR_NumBatteriesPresent\",\"BAGGR_NumBatteriesExpected\",\"BAGGR_LOG_BattConnectionStatus0\",\"BAGGR_LOG_BattConnectionStatus1\",\"BAGGR_LOG_BattConnectionStatus2\",\"BAGGR_LOG_BattConnectionStatus3\"]}"
            pb.tail.value = 2   

            r = self.post(
                "tedapi/device/%s/v1" % din,
                headers={'Content-type': 'application/octet-string'},
                data=pb.SerializeToString())

            # Decode response
            tedapi = tedapi_pb2.Message()
            tedapi.ParseFromString(r.content)
            payload = tedapi.message.config.recv.file.text
            data = json.loads(payload)
            logger.debug(f"Configuration: {data}")
            self._cache[key] = data
            return data


    def get_pw_vitals(self, din, force=False):
        """
        Get Powerwall 3 Battery Vitals Data
        """
        key = "%s-vitals" % din

        with self._vitalsLock:
            if not force:
                try:
                    value = self._cache[key]
                    logger.debug("Using Cached pw_vitals")
                    return value
                except:
                    pass

            # Check Connection
            self.connect()

            # Fetch Device ComponentsQuery from each Powerwall
            pb = tedapi_pb2.Message()
            pb.message.deliveryChannel = 1
            pb.message.sender.local = 1
            pb.message.sender.din = self.din # DIN of Primary Powerwall 3 / System
            pb.message.recipient.din = din   # DIN of Powerwall of Interest
            pb.message.payload.send.num = 2
            pb.message.payload.send.payload.value = 1
            pb.message.payload.send.payload.text = " query ComponentsQuery (\n  $pchComponentsFilter: ComponentFilter,\n  $pchSignalNames: [String!],\n  $pwsComponentsFilter: ComponentFilter,\n  $pwsSignalNames: [String!],\n  $bmsComponentsFilter: ComponentFilter,\n  $bmsSignalNames: [String!],\n  $hvpComponentsFilter: ComponentFilter,\n  $hvpSignalNames: [String!],\n  $baggrComponentsFilter: ComponentFilter,\n  $baggrSignalNames: [String!],\n  ) {\n  # TODO STST-57686: Introduce GraphQL fragments to shorten\n  pw3Can {\n    firmwareUpdate {\n      isUpdating\n      progress {\n         updating\n         numSteps\n         currentStep\n         currentStepProgress\n         progress\n      }\n    }\n  }\n  components {\n    pws: components(filter: $pwsComponentsFilter) {\n      signals(names: $pwsSignalNames) {\n        name\n        value\n        textValue\n        boolValue\n        timestamp\n      }\n      activeAlerts {\n        name\n      }\n    }\n    pch: components(filter: $pchComponentsFilter) {\n      signals(names: $pchSignalNames) {\n        name\n        value\n        textValue\n        boolValue\n        timestamp\n      }\n      activeAlerts {\n        name\n      }\n    }\n    bms: components(filter: $bmsComponentsFilter) {\n      signals(names: $bmsSignalNames) {\n        name\n        value\n        textValue\n        boolValue\n        timestamp\n      }\n      activeAlerts {\n        name\n      }\n    }\n    hvp: components(filter: $hvpComponentsFilter) {\n      partNumber\n      serialNumber\n      signals(names: $hvpSignalNames) {\n        name\n        value\n        textValue\n        boolValue\n        timestamp\n      }\n      activeAlerts {\n        name\n      }\n    }\n    baggr: components(filter: $baggrComponentsFilter) {\n      signals(names: $baggrSignalNames) {\n        name\n        value\n        textValue\n        boolValue\n        timestamp\n      }\n      activeAlerts {\n        name\n      }\n    }\n  }\n}\n"
            pb.message.payload.send.code = b'0\201\210\002B\000\270q\354>\243m\325p\371S\253\231\346~:\032\216~\242\263\207\017L\273O\203u\241\270\333w\233\354\276\246h\262\243\255\261\007\202D\277\353x\023O\022\303\216\264\010-\'i6\360>B\237\236\304\244m\002B\001\023Pk\033)\277\236\342R\264\247g\260u\036\023\3662\354\242\353\035\221\234\027\245\321J\342\345\037q\262O\3446-\353\315m1\237zai0\341\207C4\307\300Z\177@h\335\327\0239\252f\n\206W'
            pb.message.payload.send.b.value = "{\"pwsComponentsFilter\":{\"types\":[\"PW3SAF\"]},\"pwsSignalNames\":[\"PWS_SelfTest\",\"PWS_PeImpTestState\",\"PWS_PvIsoTestState\",\"PWS_RelaySelfTest_State\",\"PWS_MciTestState\",\"PWS_appGitHash\",\"PWS_ProdSwitch_State\"],\"pchComponentsFilter\":{\"types\":[\"PCH\"]},\"pchSignalNames\":[\"PCH_State\",\"PCH_PvState_A\",\"PCH_PvState_B\",\"PCH_PvState_C\",\"PCH_PvState_D\",\"PCH_PvState_E\",\"PCH_PvState_F\",\"PCH_AcFrequency\",\"PCH_AcVoltageAB\",\"PCH_AcVoltageAN\",\"PCH_AcVoltageBN\",\"PCH_packagePartNumber_1_7\",\"PCH_packagePartNumber_8_14\",\"PCH_packagePartNumber_15_20\",\"PCH_packageSerialNumber_1_7\",\"PCH_packageSerialNumber_8_14\",\"PCH_PvVoltageA\",\"PCH_PvVoltageB\",\"PCH_PvVoltageC\",\"PCH_PvVoltageD\",\"PCH_PvVoltageE\",\"PCH_PvVoltageF\",\"PCH_PvCurrentA\",\"PCH_PvCurrentB\",\"PCH_PvCurrentC\",\"PCH_PvCurrentD\",\"PCH_PvCurrentE\",\"PCH_PvCurrentF\",\"PCH_BatteryPower\",\"PCH_AcRealPowerAB\",\"PCH_SlowPvPowerSum\",\"PCH_AcMode\",\"PCH_AcFrequency\",\"PCH_DcdcState_A\",\"PCH_DcdcState_B\",\"PCH_appGitHash\"],\"bmsComponentsFilter\":{\"types\":[\"PW3BMS\"]},\"bmsSignalNames\":[\"BMS_nominalEnergyRemaining\",\"BMS_nominalFullPackEnergy\",\"BMS_appGitHash\"],\"hvpComponentsFilter\":{\"types\":[\"PW3HVP\"]},\"hvpSignalNames\":[\"HVP_State\",\"HVP_appGitHash\"],\"baggrComponentsFilter\":{\"types\":[\"BAGGR\"]},\"baggrSignalNames\":[\"BAGGR_State\",\"BAGGR_OperationRequest\",\"BAGGR_NumBatteriesConnected\",\"BAGGR_NumBatteriesPresent\",\"BAGGR_NumBatteriesExpected\",\"BAGGR_LOG_BattConnectionStatus0\",\"BAGGR_LOG_BattConnectionStatus1\",\"BAGGR_LOG_BattConnectionStatus2\",\"BAGGR_LOG_BattConnectionStatus3\"]}"
            pb.tail.value = 2

            r = self.post(
                "tedapi/device/%s/v1" % din,
                headers={'Content-type': 'application/octet-string'},
                data=pb.SerializeToString())

            # Decode response
            tedapi = tedapi_pb2.Message()
            tedapi.ParseFromString(r.content)
            payload = tedapi.message.payload.recv.text
            data = json.loads(payload)
            logger.debug(f"Battery Block('{din}'): {data}")
            self._cache[key] = data
            return data


    def get_pw3_vitals(self, force=False):
        """
        Get Powerwall 3 Battery Vitals Data

        Returns:
        {
            "PVAC--{part}--{sn}" {
                "PVAC_PvState_A": "PV_Active",
                "PVAC_PVCurrent_A": 0.0,
                ...
                "PVAC_PVMeasuredVoltage_A": 0.0,
                ...
                "PVAC_PVMeasuredPower_A": 0.0,
                ...
                "PVAC_Fout": 60.0,
                "PVAC_Pout": 0.0,
                "PVAC_State": X,
                "PVAC_VL1Ground": lookup(p, ['PVAC_Logging', 'PVAC_VL1Ground']),
                "PVAC_VL2Ground": lookup(p, ['PVAC_Logging', 'PVAC_VL2Ground']),
                "PVAC_Vout": lookup(p, ['PVAC_Status', 'PVAC_Vout']),
                "manufacturer": "TESLA",
                "partNumber": packagePartNumber,
                "serialNumber": packageSerialNumber,
            }.
            "PVS--{part}--{sn}" {
                "PVS_StringA_Connected": true,
                ...
            },
            "TEPOD--{part}--{sn}" {
                "alerts": [],
                "POD_nom_energy_remaining": 0.0,
                "POD_nom_full_pack_energy": 0.0,
                "POD_nom_energy_to_be_charged": 0.0,
            }
        }

        Note: This makes multiple API calls, specifically 2 + the number of
              Powerwalls and Expanders in the system
        """
        with self._vitalsLock:
            if not force:
                try:
                    value = self._cache["vitals"]
                    logger.debug("Using Cached pw3_vitals")
                    return value
                except:
                    pass

            # Check Connection
            self.connect()

            config = self.get_config(force)
            components = self.get_components(force)
            battery_blocks = config['battery_blocks']

            # Loop through all the battery blocks (Powerwalls)
            response = {}
            for battery in battery_blocks:
                pw_din = battery['vin'] # 1707000-11-J--TG12xxxxxx3A8Z
                pw_part, pw_serial = pw_din.split('--')
                battery_type = battery['type']
                if "Powerwall3" not in battery_type:
                    continue

                data = self.get_pw_vitals(pw_din, force)

                # TEDPOD
                alerts = []
                components = data['components']
                for component in components:
                    if components[component]:
                        for alert in components[component][0]['activeAlerts']:
                            if alert['name'] not in alerts:
                                alerts.append(alert['name'])
                bms_component = data['components']['bms'][0] # TODO: Process all BMS components
                signals = bms_component['signals']
                nom_energy_remaining = 0
                nom_full_pack_energy = 0
                for signal in signals:
                    if "BMS_nominalEnergyRemaining" == signal['name']:
                        nom_energy_remaining = int(signal['value'] * 1000) # Convert to Wh
                    elif "BMS_nominalFullPackEnergy" == signal['name']:
                        nom_full_pack_energy = int(signal['value'] * 1000) # Convert to Wh
                response[f"TEPOD--{pw_din}"] = {
                    "alerts": alerts,
                    "POD_nom_energy_remaining": nom_energy_remaining,
                    "POD_nom_energy_to_be_charged": nom_full_pack_energy - nom_energy_remaining,
                    "POD_nom_full_pack_energy": nom_full_pack_energy,
                }
                # PVAC, PVS and TEPINV
                response[f"PVAC--{pw_din}"] = {}
                response[f"PVS--{pw_din}"] = {}
                response[f"TEPINV--{pw_din}"] = {}
                pch_components = data['components']['pch']
                # pch_components contain:
                #   PCH_PvState_A through F - textValue in [Pv_Active, Pv_Active_Parallel, Pv_Standby]
                #   PCH_PvVoltageA through F - value
                #   PCH_PvCurrentA through F - value
                # Loop through and find all the strings - PW3 has 6 strings A-F
                for n in ["A", "B", "C", "D", "E", "F"]:
                    pv_state = "Unknown"
                    pv_voltage = 0
                    pv_current = 0
                    for component in pch_components: # TODO: Probably better way to do this
                        signals = component['signals']
                        for signal in signals:
                            if signal['name'] == f'PCH_PvState_{n}':
                                pv_state = signal['textValue']
                            elif signal['name'] == f'PCH_PvVoltage{n}':
                                pv_voltage = max(signal['value'], 0)
                            elif signal['name'] == f'PCH_PvCurrent{n}':
                                pv_current = max(signal['value'], 0)
                            else:
                                match signal['name']:
                                    case 'PCH_AcFrequency':
                                        response[f"PVAC--{pw_din}"]["PVAC_Fout"] = signal['value']
                                        response[f"TEPINV--{pw_din}"]["PINV_Fout"] = signal['value']
                                    case 'PCH_AcVoltageAN':
                                        response[f"PVAC--{pw_din}"]["PVAC_VL1Ground"] = signal['value']
                                        response[f"TEPINV--{pw_din}"]["PINV_VSplit1"] = signal['value']
                                    case 'PCH_AcVoltageBN':
                                        response[f"PVAC--{pw_din}"]["PVAC_VL2Ground"] = signal['value']
                                        response[f"TEPINV--{pw_din}"]["PINV_VSplit2"] = signal['value']
                                    case 'PCH_AcVoltageAB':
                                        response[f"PVAC--{pw_din}"]["PVAC_Vout"] = signal['value']
                                        response[f"TEPINV--{pw_din}"]["PINV_Vout"] = signal['value']
                                    case 'PCH_AcRealPowerAB':
                                        response[f"PVAC--{pw_din}"]["PVAC_Pout"] = signal['value']
                                        response[f"TEPINV--{pw_din}"]["PINV_Pout"] = (signal['value'] or 0) / 1000
                                    case 'PCH_AcMode':
                                        response[f"PVAC--{pw_din}"]["PVAC_State"] = signal['textValue']
                                        response[f"TEPINV--{pw_din}"]["PINV_State"] = signal['textValue']
                    pv_power = pv_voltage * pv_current # Calculate power
                    response[f"PVAC--{pw_din}"][f"PVAC_PvState_{n}"] = pv_state
                    response[f"PVAC--{pw_din}"][f"PVAC_PVMeasuredVoltage_{n}"] = pv_voltage
                    response[f"PVAC--{pw_din}"][f"PVAC_PVCurrent_{n}"] = pv_current
                    response[f"PVAC--{pw_din}"][f"PVAC_PVMeasuredPower_{n}"] = pv_power
                    response[f"PVAC--{pw_din}"]["manufacturer"] = "TESLA"
                    response[f"PVAC--{pw_din}"]["partNumber"] = pw_part
                    response[f"PVAC--{pw_din}"]["serialNumber"] = pw_serial
                    response[f"PVS--{pw_din}"][f"PVS_String{n}_Connected"] = ("Pv_Active" in pv_state)
            self._cache["vitals"] = response
            return response


    def get_battery_blocks(self, force=False):
        """
        Return Powerwall Battery Blocks
        """
        config = self.get_config(force)
        battery_blocks = config.get('battery_blocks') or []
        return battery_blocks
