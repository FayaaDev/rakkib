"""Rakkib — agent-driven personal server kit."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("rakkib")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
