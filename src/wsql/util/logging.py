"""Logging configuration for WSQL.

Provides a standardized logger factory that inherits wxpath's
logging configuration when available, or falls back to Python's
standard logging.
"""

from __future__ import annotations

import logging


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance for the specified module.

    Args:
        name: Module name (typically ``__name__``).

    Returns:
        A configured logger instance.

    Example::

        >>> from wsql.util.logging import get_logger
        >>> log = get_logger(__name__)
        >>> log.info('WSQL query started')
    """
    return logging.getLogger(name)
