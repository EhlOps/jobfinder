"""schemas: the request and response bodies of the HTTP API, one module per router in jobfinder.api.

These classes are the published contract (tests/snapshots/openapi.json). No module here imports a router or
the Claude task schemas (integrations/claude/schemas.py), so a prompt change cannot silently change the API.
"""
