"""services: the application's rules, one module per area, called by routers, task handlers and the CLI.

A service takes the caller's session, raises `core.errors.AppError` subclasses, and never commits;
the boundary does. SQL lives in `jobfinder.repositories`. `ratelimit` is the DB-backed rate limiter.
"""
