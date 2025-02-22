#!/usr/bin/python3

import ipaddress
import json
import logging
import re
from pyroute2 import IPRoute
from socket import AF_INET


def getAttr(list, key):
    for item in list:
        if item[0] == key:
            return item[1]
    return None


def checkExistingRoute(existing, route):
    return route['network'] in existing.keys()


def checkLocalAddress(networks, ip):
    for network in networks:
        if ipaddress.ip_address(ip) in ipaddress.ip_network(network, False):
            return True
    return False


def checkMatchingRoute(existing, route):
    if route['network'] in existing.keys():
        return route['nexthop'] == existing[route['network']]['gateway']


def getNetworks(ipr):
    networks = {}
    for addr in ipr.get_addr(family=AF_INET):
        network = "%s/%d" % (addr.get_attr('IFA_ADDRESS'), addr['prefixlen'])
        link = ipr.get_links(addr['index'])[0]
        iface = getAttr(link['attrs'], 'IFLA_IFNAME')
        usable = iface not in ['lo', 'lo0', 'hassio', 'docker0']
        networks[network] = {'name': iface, 'usable': usable, 'link': link}
    return networks


def getRoutes(ipr, networks):
    existing = {}
    ipr_routes = ipr.get_routes(family=AF_INET)
    for item in ipr_routes:
        # Filter out local networks
        gateway = getAttr(item['attrs'], 'RTA_GATEWAY')
        if gateway != None:
            network = getAttr(item['attrs'], 'RTA_DST')
            if network == None:
                network = "0.0.0.0"
            network = "%s/%d" % (network, item['dst_len'])
            if network not in networks:
                route = {}
                route['gateway'] = gateway
                route['raw'] = item
                existing[network] = route
    return existing


def printRoutes(prefix, networks, routes):
    keys = list(networks.keys()) + list(routes.keys())
    keys.sort(key=sortNetwork)
    logger.info("%sRoutes:" % prefix)
    for network in keys:
        if network in networks.keys():
            iface = networks[network]['name']
            netaddr = ipaddress.ip_network(network, False).network_address
            ipaddr, mask = network.split('/')
            network = "%s/%s" % (netaddr, mask)
            logger.info("  %s via %s dev %s" % (network, ipaddr, iface))
        else:
            gateway = routes[network]['gateway']
            if network == "0.0.0.0/0":
                network = "default"
            logger.info("  %s via %s" % (network, gateway))


def sortNetwork(network):
    return [int(x) for x in re.split(r'\.|/', network)]


logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S")

ipr = IPRoute()

# Get local subnets and interfaces
networks = getNetworks(ipr)

# Get all existing routes
existing = getRoutes(ipr, networks.keys())

# Print routes
printRoutes("Initial ", networks, existing)

# Load static routes from config
config = json.load(open('/data/options.json', 'r'))
routes = config['routes']
for route in routes:
    route['address'] = re.sub('/.*', '', route['network'])
# Remove routes that already exist and match
changes = [ x for x in routes if not checkMatchingRoute(existing, x)]

# Check remaining routes to make sure the gateway is local and usable
usable = [ x for x, n in networks.items() if n['usable']]
errors = [
    "nexthop '%s' is not locally addressable on a usable network" % x['nexthop']
    for x in changes if not checkLocalAddress(usable, x['nexthop'])]

# Check remaining routes to make sure they aren't a subset of an unusable link
notusable = [ x for x, n in networks.items() if not n['usable']]
errors += [
    "network '%s' is a subnet on a reserved network" % x['network']
    for x in changes if checkLocalAddress(notusable, x['address'])]
for error in errors:
    logger.error(error)
if len(errors):
    logger.critical("Exiting due to bad routes.  No changes were made.")
    exit(1)

changes = [ x for x in changes if checkLocalAddress(usable, x['nexthop'])]
changes = [ x for x in changes if not checkLocalAddress(notusable, x['address'])]

# Add or change required routes
for route in changes:
    logger.debug("Adding route '%s' via '%s'" % (route['network'], route['nexthop']))
    ipr.route('replace', dst=route['network'], gateway=route['nexthop'])


# Check all routes on primary interface to see if they are still needed
if config['prune']:
    del existing['0.0.0.0/0']
    for route in routes:
        if route['network'] in existing.keys():
            del existing[route['network']]
    for network, route in existing.items():
        logger.debug("Removing route '%s' via '%s'" % (network, route['gateway']))
        ipr.route('del', dst=network, gateway=route['gateway'])

# Get local subnets and interfaces
networks = getNetworks(ipr)

# Get all existing routes
existing = getRoutes(ipr, networks.keys())

# Print routes
printRoutes("Final ", networks, existing)
