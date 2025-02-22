# staticroutes
A simple Home Assistant Add On that adds static routes to HAOS.

## How to use
1) ***DO NOT*** enable "Start on boot" until you have tested the add-on by starting it manually and verifying everything still works.  Failure to heed this warning could result in **BRICKING** Home Assistant until you login on the physical console,  manual remove the add-on, and reboot to restore connectivity.
2) Enable privilege mode.
3) Add a Ping integration for an IP you are adding a route for.  It should report down for the moment.
4) Make sure the IP(s) you want to connect to are available through a static IP on the LOCAL subnet.
5) Add each pair of nexthop and network to the configuration.
6) Manually start the add-on.
7) Verify the following:
	* You can still connect to and manage HA
	* All your other add-ons still work
	* The Ping monitor you added is reporting Connected
8) Enable "Start on boot"

## Making changes
1) ***Disable "Start on boot" FIRST.***
2) Follow the steps in "How to use"

## Notes
* If the nexthop is not on the local subnet you don't need this add-on and instead need to add the route on your router instead.  The add-on will verify the nexthop is on the local subnet before proceeding and will report an error if it is not.
* If the route you are trying to add overlaps with the subnet another add-on is using, you will need to re-ip your network to avoid that conflict.
