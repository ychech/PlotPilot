import logging

from interfaces.api.middleware.logging_config import (
    UvicornAccessNoiseFilter,
    configure_uvicorn_access_logging,
)


def _record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_uvicorn_access_noise_filter_suppresses_autopilot_status_polling():
    filt = UvicornAccessNoiseFilter()

    suppressed = [
        '127.0.0.1:57000 - "GET /api/v1/autopilot/novel-1779330438193/status HTTP/1.1" 200 OK',
        '127.0.0.1:63163 - "GET /api/v1/autopilot/novel-1779330438193/circuit-breaker HTTP/1.1" 200 OK',
        '127.0.0.1:63109 - "GET /api/v1/novels/novel-1779330438193/monitor/voice-drift HTTP/1.1" 200 OK',
        '127.0.0.1:63111 - "GET /api/v1/novels/novel-1779330438193/foreshadow-ledger HTTP/1.1" 200 OK',
    ]

    for message in suppressed:
        assert filt.filter(_record(message)) is False


def test_uvicorn_access_noise_filter_keeps_other_access_logs():
    filt = UvicornAccessNoiseFilter()

    assert filt.filter(_record('127.0.0.1:57000 - "GET /api/v1/novels HTTP/1.1" 200 OK')) is True
    assert filt.filter(_record(
        '127.0.0.1:57000 - "GET /api/v1/autopilot/novel-1779330438193/status HTTP/1.1" 500 Internal Server Error'
    )) is True


def test_configure_uvicorn_access_logging_filters_existing_handlers():
    logger = logging.getLogger("uvicorn.access")
    handler = logging.StreamHandler()
    old_handlers = list(logger.handlers)
    old_filters = list(logger.filters)
    try:
        logger.handlers = [handler]
        logger.filters = []

        configure_uvicorn_access_logging()

        assert any(isinstance(f, UvicornAccessNoiseFilter) for f in logger.filters)
        assert any(isinstance(f, UvicornAccessNoiseFilter) for f in handler.filters)
    finally:
        logger.handlers = old_handlers
        logger.filters = old_filters
