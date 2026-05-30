"""Shared ``application.bot_data`` keys.

Hoisted out of ``handlers.py`` so ``main.py`` can use the same constants
when wiring resources and avoid string-literal drift between the two
modules. The leading underscore is a soft "internal" signal — nothing
outside the vhsbot package should reach into ``bot_data`` directly.
"""

from __future__ import annotations

BD_SETTINGS = "settings"
BD_DB = "db"
BD_DB_LOCK = "db_lock"
BD_CLIENT = "http_client"
