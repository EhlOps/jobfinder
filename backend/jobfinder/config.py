"""Shim until #53: settings live in jobfinder.core.config. New code imports from there."""
from jobfinder.core.config import Settings, SettingsError, get_settings, load_settings, settings

__all__ = ["Settings", "SettingsError", "get_settings", "load_settings", "settings"]
