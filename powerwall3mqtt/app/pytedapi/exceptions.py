class TEDAPIException(Exception):
    # "Error fetching %s: %d"
    pass

class TEDAPIRateLimitedException(TEDAPIException):
    # 'Rate limit cooldown period - Pausing API calls'
    pass

class TEDAPIRateLimitingException(TEDAPIException):
    # 'Possible Rate limited by Powerwall at - Activating 5 minute cooldown'
    pass

class TEDAPIAccessDeniedException(TEDAPIException):
    # "Access Denied: Check your Gateway Password"
    pass

class TEDAPINotConnectedException(TEDAPIException):
    # "Not Connected - Unable to get configuration"
    pass



# Old stuff
class PyPowerwallTEDAPINoTeslaAuthFile(Exception):
    pass


class PyPowerwallTEDAPITeslaNotConnected(Exception):
    pass


class PyPowerwallTEDAPINotImplemented(Exception):
    pass


class PyPowerwallTEDAPIInvalidPayload(Exception):
    pass
