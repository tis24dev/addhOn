"""Native hOn client of addhOn.

The whole client (auth/transport, command/parameter/rules engine, appliance) is OUR
code.

Boundary rule: the body of the integration does not depend on the concrete client
objects but on the Protocols in `interfaces.py`. The factory in `factory.py`
builds the native session and appliance.
"""
