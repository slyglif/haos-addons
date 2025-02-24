class Entity:
    def __init__(self, id_prefix, name, type, template = None, device_class = None, unit = None, state_class = None, enabled = True):
        self.prefix = id_prefix
        self.name = name
        self.type = type
        self.template = template
        self.device_class = device_class
        self.unit = unit
        self.state_class = state_class
        self.value = None
        self.enabled = enabled

    def getDiscoveryComponent(self):
        unique_id = self.name.lower().replace(' ', '_')
        if self.prefix != None:
            unique_id = self.prefix + '_' + unique_id
        msg = {}
        msg['p'] = self.type
        msg['value_template'] = '{{ %s }}' % self.template
        msg['unique_id'] = unique_id
        msg['name'] = self.name
        if self.device_class != None:
            msg['device_class'] = self.device_class
        if self.unit != None:
            msg['unit_of_measurement'] = self.unit
        if self.state_class != None:
            msg['state_class'] = self.state_class
        if not self.enabled:
            msg['en'] = "false"
        return msg


class ValueEntity(Entity):
    def __init__(self, id_prefix, name, type, template = None, device_class = None, unit = None, state_class = None, enabled = True):
        Entity.__init__(self, id_prefix, name, type, template, device_class, unit, state_class, enabled)
        if template == None:
            self.template = name.lower().replace(' ', '_')

    def getDiscoveryComponent(self):
        msg = Entity.getDiscoveryComponent(self)
        msg['value_template'] = '{{ value_json.%s }}' % self.template
        return msg

    def get(self):
        return self.value

    def set(self, value):
        self.value = value



class Battery(ValueEntity):
    def __init__(self, id_prefix, name, template = None, enabled = True):
        ValueEntity.__init__(self,
            id_prefix=id_prefix,
            name=name,
            type="sensor",
            template=template,
            device_class="battery",
            unit="%",
            enabled=enabled)


class Connectivity(ValueEntity):
    def __init__(self, id_prefix, name, template = None, enabled = True):
        ValueEntity.__init__(self,
            id_prefix=id_prefix,
            name=name,
            type="binary_sensor",
            template=template,
            device_class="connectivity",
            enabled=enabled)


class Current(ValueEntity):
    def __init__(self, id_prefix, name, template = None, enabled = True):
        ValueEntity.__init__(self,
            id_prefix=id_prefix,
            name=name,
            type="sensor",
            template=template,
            device_class="current",
            unit="A",
            enabled=enabled)


class Duration(ValueEntity):
    def __init__(self, id_prefix, name, template = None, enabled = True):
        ValueEntity.__init__(self,
            id_prefix=id_prefix,
            name=name,
            type="sensor",
            template=template,
            device_class="duration",
            unit="s",
            enabled=enabled)


class EnergyStorage(ValueEntity):
    def __init__(self, id_prefix, name, template = None, enabled = True):
        ValueEntity.__init__(self,
            id_prefix=id_prefix,
            name=name,
            type="sensor",
            template=template,
            device_class="energy_storage",
            unit="Wh",
            enabled=enabled)


class PowerTemplate(Entity):
    def __init__(self, id_prefix, name, template = None, enabled = True):
        Entity.__init__(self,
            id_prefix=id_prefix,
            name=name,
            type="sensor",
            template=template,
            device_class="power",
            unit="W",
            state_class='measurement',
            enabled=enabled)


class PowerValue(ValueEntity):
    def __init__(self, id_prefix, name, template = None, enabled = True):
        ValueEntity.__init__(self,
            id_prefix=id_prefix,
            name=name,
            type="sensor",
            template=template,
            device_class="power",
            unit="W",
            state_class='measurement',
            enabled=enabled)


class Running(ValueEntity):
    def __init__(self, id_prefix, name, template = None, enabled = True):
        ValueEntity.__init__(self,
            id_prefix=id_prefix,
            name=name,
            type="binary_sensor",
            template=template,
            device_class="running",
            enabled=enabled)


class Timestamp(ValueEntity):
    def __init__(self, id_prefix, name, template = None, enabled = True):
        ValueEntity.__init__(self,
            id_prefix=id_prefix,
            name=name,
            type="sensor",
            template=template,
            device_class="timestamp",
            enabled=enabled)


class Voltage(ValueEntity):
    def __init__(self, id_prefix, name, template = None, enabled = True):
        ValueEntity.__init__(self,
            id_prefix=id_prefix,
            name=name,
            type="sensor",
            template=template,
            device_class="voltage",
            unit="V",
            enabled=enabled)
