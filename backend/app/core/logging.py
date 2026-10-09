import logging
import sys

_LOGGER_NAME = "app"


def configure_logging(level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
        )
        logger.addHandler(handler)
    logger.propagate = False
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(f"{_LOGGER_NAME}.{name}" if name else _LOGGER_NAME)
