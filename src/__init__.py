# Python/Flask port of amanda (originally (c) Matt Martz, GPL-3.0+)
"""scog: an Ansible Galaxy v3 API mirror and pull-through cache."""

from .app import create_app
from .config import Config

__all__ = ["create_app", "Config"]
