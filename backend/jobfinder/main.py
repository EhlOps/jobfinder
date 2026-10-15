"""Shim until #53: the app is built by jobfinder.api.app.create_app (uvicorn ... --factory).

`jobfinder.main:app` still works for an old command line; it loads the settings when imported.
"""
from jobfinder.api.app import create_app

app = create_app()
