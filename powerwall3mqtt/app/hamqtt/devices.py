import logging

from pytedapi import lookup as dict_lookup

from hamqtt.entities import *

logger = logging.getLogger(__name__)
online = b'online'
offline = b'offline'
origin = {}
will_topic = None

def get_item_value(list, match_name, match_value, get_value):
    for p in list:
        if p.get(match_name).lower() == match_value.lower():
            return p.get(get_value)
    return None

def get_power(list, name):
    return get_item_value(list, 'location', name, 'realPowerW')


class Device:
    def __init__(self, mqtt_prefix, id):
        self.id = id
        self.config_topic = '%s/device/%s/config' % (mqtt_prefix, id)
        self.state_topic = "%s/device/%s/state" % (mqtt_prefix, id)


    def getDiscoveryMessage(self):
        msg = {}
        msg['topic'] = self.config_topic
        msg['payload'] = {}
        msg['payload']['dev'] = {}
        msg['payload']['dev']['ids'] = self.id
        msg['payload']['state_topic'] = self.state_topic
        msg['payload']['availability'] = []
        msg['payload']['availability'].append({'topic': self.state_topic})
        msg['payload']['availability'][0]['value_template'] = '{{ value_json.mqtt_availability }}'
        msg['payload']['availability'].append({'topic': will_topic})
        msg['payload']['o'] = origin
        cmps = {}
        for name, value in vars(self).items():
            if issubclass(type(value), Entity):
                cmps[name] = value.getDiscoveryComponent()
        msg['payload']['cmps'] = cmps
        return msg


    def recurse(self, item):
        if issubclass(type(item), Entity):
            return item.get()
        elif issubclass(type(item), dict):
            values = {}
            for i in item.keys():
                value = self.recurse(item[i])
                if value != None:
                    values[i] = value
            if len(values):
                return values
            return None
        return None


    def getStateMessage(self):
        msg = {}
        msg['topic'] = self.state_topic
        msg['payload'] = {}
        msg['payload']['mqtt_availability'] = "online"
        for name, value in vars(self).items():
            if issubclass(type(value), ValueEntity):
                msg['payload'][name] = value.get()
            elif issubclass(type(value), dict):
                value = self.recurse(value)
                if value != None:
                    msg['payload'][name] = value
        return msg


class PowerWall3(Device):
    def __init__(self, mqtt_prefix, parent, vin, config, vitals):
        self.vin = vin
        self.type = get_item_value(config['battery_blocks'], 'vin', vin, 'type')
        id = self.type + '_' + self.vin
        Device.__init__(self, mqtt_prefix, id)

        self.via = parent
        self.name = "%s %s" % (config['site_info']['site_name'], self.vin.split('--')[1])

        # Home Assistant Components
        self.battery_capacity = EnergyStorage(id, "Battery Capacity")
        self.battery_remaining = EnergyStorage(id, "Battery Remaining")

        # Default disable PV String info
        self.strings = {}
        for i in 'ABCDEF':
            key = "strings['%s']" % i
            self.strings[i] = {}
            self.strings[i]['mode'] = ValueEntity(
                id,
                "PV String %s Mode" % i, 'sensor',
                template = '%s.mode' % key,
                enabled = False)
            self.strings[i]['current'] = Current(
                id,
                "PV String %s Current" % i,
                template = '%s.current' % key,
                enabled = False)
            self.strings[i]['voltage'] = Voltage(
                id,
                "PV String %s Voltage" % i,
                template = '%s.voltage' % key,
                enabled = False)
            self.strings[i]['power'] = PowerValue(
                id,
                "PV String %s Power" % i,
                template = '%s.power' % key,
                enabled = False)
        self.update(config, vitals)


    def update(self, config, vitals):
        self.name = "%s %s" % (config['site_info']['site_name'], self.vin.split('--')[1])
        vk = 'TEPOD--' + self.vin
        self.battery_capacity.set(vitals[vk]['POD_nom_full_pack_energy'])
        self.battery_remaining.set(vitals[vk]['POD_nom_energy_remaining'])
        vk = 'PVAC--' + self.vin
        for i in self.strings.keys():
            self.strings[i]['mode'].set(vitals[vk]['PVAC_PvState_' + i])
            self.strings[i]['current'].set(round(vitals[vk]['PVAC_PVCurrent_' + i], 2))
            self.strings[i]['voltage'].set(round(vitals[vk]['PVAC_PVMeasuredVoltage_' + i], 2))
            self.strings[i]['power'].set(round(vitals[vk]['PVAC_PVMeasuredPower_' + i], 2))


    def getDiscoveryMessage(self):
        msg = Device.getDiscoveryMessage(self)
        msg['payload']['dev']['mf'] = 'Tesla'
        msg['payload']['dev']['mdl'] = 'Powerwall3' # FIXME: Pull from tesla info
        msg['payload']['dev']['mdl_id'] = self.vin.split('--')[0] # TODO: Check if available separately
        msg['payload']['dev']['sn'] = self.vin.split('--')[1] # TODO: CHeck if available separately
        msg['payload']['dev']['name'] = self.name
        msg['payload']['dev']['via_device'] = self.via
        for i, s in self.strings.items():
            for item, value in s.items():
                msg['payload']['cmps']['string_%s_%s' % (i, item)] = value.getDiscoveryComponent()
        return msg



class TeslaSystem(Device):
    def __init__(self, mqtt_prefix, tedapi, report_vitals=True):
        firmware = tedapi.get_firmware_version(details=True)
        logger.debug("firmware = %r", firmware)
        if firmware == None:
            raise Exception("Unable to fetch firmware information")

        config = tedapi.get_config()
        logger.debug("config = %r", config)
        if config == None:
            raise Exception("Unable to fetch config information")

        status = tedapi.get_status()
        logger.debug("status = %r", status)
        if status == None:
            raise Exception("Unable to fetch status information")

        vitals = tedapi.get_pw3_vitals()
        logger.debug("vitals = %r", vitals)
        if vitals == None:
            logger.warning("Unable to fetch vitals information")

        id = "TeslaEnergySystem_" + config['vin']
        Device.__init__(self, mqtt_prefix, id)

        self.tedapi = tedapi
        self.report_vitals = report_vitals
        self.serial = firmware['gateway']['serialNumber']
        self.part_number = firmware['gateway']['partNumber']
        self.firmware_version = firmware['version']['text']
        self.vin = config['vin']
        self.site_name = config['site_info']['site_name']

        # Home Assistant sensors
        self.battery = Battery(id, "Battery")
        self.battery_capacity = EnergyStorage(id, "Battery Capacity")
        self.battery_power = PowerValue(id, "Battery Power")
        self.battery_remaining = EnergyStorage(id, "Battery Remaining")
        self.battery_reserve_hidden = EnergyStorage(id, "Battery Hidden Reserve", 'battery_reserve_hidden')
        self.battery_reserve_user = Battery(id, "Battery Reserve", 'battery_reserve_user')
        self.battery_time_remaining = Duration(id, "Battery Time Remaining")
        self.calibration = Running(id, "Calibration")
        self.commission_date = Timestamp(id, "Commission Date")
        self.grid_power = PowerValue(id, "Grid Power")
        self.grid_status = Connectivity(id, "Grid Status")
        self.inverter_capacity = PowerValue(id, "Inverter Capacity")
        self.load_power = PowerValue(id, "Load Power")
        self.solar_power = PowerValue(id, "Solar Power")

        # Home Assistant template sensors
        self.battery_power_in = PowerTemplate(
            id,
            "Battery Power Charge",
            template = "[ value_json.battery_power | int, 0 ] | max",
            enabled = False
            )
        self.battery_power_out = PowerTemplate(
            id,
            "Battery Power Discharge",
            template = "[ value_json.battery_power | int, 0 ] | min | abs",
            enabled = False)
        self.grid_power_in = PowerTemplate(
            id,
            "Grid Power Import",
            template = "[ value_json.grid_power | int, 0 ] | max",
            enabled = False)
        self.grid_power_out = PowerTemplate(
            id,
            "Grid Power Export",
            template = "[ value_json.grid_power | int, 0 ] | min | abs",
            enabled = False)

        self.powerwalls = {}
        for b in config['battery_blocks']:
            self.powerwalls[b['vin']] = PowerWall3(mqtt_prefix, id, b['vin'], config, vitals)

        self.update(firmware, config, status, vitals)


    def update(self, firmware = None, config = None, status = None, vitals = None):
        if firmware == None:
            firmware = self.tedapi.get_firmware_version(details=True)
            if firmware == None:
                raise Exception("Unable to fetch firmware information")
        if config == None:
            config = self.tedapi.get_config()
            if config == None:
                raise Exception("Unable to fetch config information")
        if status == None:
            status = self.tedapi.get_status()
            if status == None:
                raise Exception("Unable to fetch status information")
        if vitals == None and self.report_vitals:
            vitals = self.tedapi.get_pw3_vitals()
            if vitals == None:
                logger.warning("Unable to fetch vitals information")

        self.serial = firmware['gateway']['serialNumber']
        self.part_number = firmware['gateway']['partNumber']
        self.firmware_version = firmware['version']['text']
        self.site_name = config['site_info']['site_name']

        # Map config
        self.commission_date.set(config['site_info']['battery_commission_date'])
        self.inverter_capacity.set(config['site_info']['nominal_system_power_ac'] * 1000)
        self.battery_reserve_user.set(int(config['site_info']['backup_reserve_percent'] * 100 / 105))

        # Map status
        self.grid_status.set("OFF")
        if status['esCan']['bus']['ISLANDER']['ISLAND_GridConnection']['ISLAND_GridConnected'] == "ISLAND_GridConnected_Connected":
            self.grid_status.set("ON")
        self.calibration.set('OFF')
        if "BatteryCalibration" in status['control']['alerts']['active']:
            self.calibration.set('ON')
        self.battery_reserve_hidden.set(int(status['control']['systemStatus']['nominalFullPackEnergyWh'] / 20))
        self.battery_capacity.set(status['control']['systemStatus']['nominalFullPackEnergyWh'] - self.battery_reserve_hidden.get())
        self.battery_remaining.set(status['control']['systemStatus']['nominalEnergyRemainingWh'] - self.battery_reserve_hidden.get())
        self.grid_power.set(round(get_power(status['control']['meterAggregates'], 'SITE'), 2))
        self.solar_power.set(round(get_power(status['control']['meterAggregates'], 'SOLAR'), 2))
        self.battery_power.set(round(get_power(status['control']['meterAggregates'], 'BATTERY'), 2))
        self.load_power.set(round(get_power(status['control']['meterAggregates'], 'LOAD'), 2))
        
        # Adjust battery capacity and remaining to account for "hidden" 5% reserve
        self.battery.set(int(self.battery_remaining.get() * 100 / self.battery_capacity.get()))
        self.battery_time_remaining.set(int(round(self.battery_remaining.get() * 3600 / self.load_power.get(), 0)))

        if vitals != None:
            for p in self.powerwalls.keys():
                self.powerwalls[p].update(config, vitals)


    def getDiscoveryMessage(self):
        msg = Device.getDiscoveryMessage(self)
        msg['payload']['dev']['name'] = self.site_name
        msg['payload']['dev']['mf'] = 'Tesla'
        msg['payload']['dev']['mdl'] = 'Powerwall3' # FIXME: Pull from tesla info
        msg['payload']['dev']['mdl_id'] = self.part_number
        msg['payload']['dev']['sw'] = self.firmware_version
        msg['payload']['dev']['sn'] = self.serial
        return msg


    def getDiscoveryMessages(self):
        msgs = []
        msgs.append(self.getDiscoveryMessage())
        for i in self.powerwalls.keys():
            msgs.append(self.powerwalls[i].getDiscoveryMessage())
        return msgs


    def getStateMessages(self):
        msgs = []
        msgs.append(self.getStateMessage())
        if self.report_vitals:
            for i in self.powerwalls.keys():
                msgs.append(self.powerwalls[i].getStateMessage())
        return msgs
