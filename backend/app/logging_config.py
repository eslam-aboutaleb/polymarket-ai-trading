"""Application logging configuration."""

import logging.config
import os


def configure_logging() -> None:
    """Configure root logging from the operator-provided log level."""
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {
                    "format": "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                },
            },
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "formatter": "standard",
                },
            },
            "root": {
                "handlers": ["default"],
                "level": os.environ.get("LOG_LEVEL", "INFO").upper(),
            },
        }
    )
