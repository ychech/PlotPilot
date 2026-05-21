"""Unified logging configuration module."""

import logging
import os
import re
from typing import Optional

# Valid logging levels for validation
VALID_LOGGING_LEVELS = [
    logging.DEBUG,
    logging.INFO,
    logging.WARNING,
    logging.ERROR,
    logging.CRITICAL
]


class PromptLogFilter(logging.Filter):
    """只放行 infrastructure.ai 命名空间下的日志记录，用于提示词专用日志文件。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith("infrastructure.ai")


# 终端静默的 logger 前缀（守护进程轮询等高频日志，仍写入文件）
_CONSOLE_SUPPRESS_PREFIXES = (
    "application.engine.services.autopilot_daemon",
    "infrastructure.persistence.database.sqlite_novel_repository",
    "infrastructure.persistence.database.connection_pool",
)


class ConsoleQuietFilter(logging.Filter):
    """终端不刷守护进程/数据库轮询日志，这些日志仍写入文件。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(_CONSOLE_SUPPRESS_PREFIXES)


class UvicornAccessNoiseFilter(logging.Filter):
    """Filter high-frequency successful polling requests from uvicorn access logs."""

    _POLLING_200_RE = re.compile(
        r'"GET (?P<path>/api/v1/(?:'
        r'autopilot/[^/\s]+/(?:status|circuit-breaker)|'
        r'novels/[^/\s]+/(?:monitor/voice-drift|foreshadow-ledger)'
        r')(?:\?[^"\s]*)?) HTTP/[^"]+" 200'
    )

    def filter(self, record: logging.LogRecord) -> bool:
        return not self._POLLING_200_RE.search(record.getMessage())


class SafeConsoleHandler(logging.StreamHandler):
    """Console handler that degrades gracefully on encoding errors.

    Windows terminals commonly default to legacy encodings such as GBK.
    When log messages contain emoji or other non-representable characters,
    the standard StreamHandler can trigger UnicodeEncodeError while writing
    to stderr/stdout. This handler falls back to an ASCII-safe escaped
    representation instead of crashing the log emission path.
    """

    def emit(self, record: logging.LogRecord) -> None:
        """Emit a record, escaping unsupported characters when needed."""
        try:
            msg = self.format(record)
            stream = self.stream
            try:
                stream.write(msg + self.terminator)
            except UnicodeEncodeError:
                safe_msg = self._escape_for_stream(msg + self.terminator, stream)
                stream.write(safe_msg)
            self.flush()
        except RecursionError:
            raise
        except Exception:
            self.handleError(record)

    @staticmethod
    def _escape_for_stream(text: str, stream) -> str:
        """Convert text to a stream-safe escaped representation."""
        encoding = getattr(stream, "encoding", None) or "utf-8"
        return text.encode(encoding, errors="backslashreplace").decode(encoding)


def _validate_logging_level(level: int) -> None:
    """Validate that the logging level is a valid Python logging level.

    Args:
        level: The logging level to validate

    Raises:
        ValueError: If the logging level is not valid
    """
    if level not in VALID_LOGGING_LEVELS:
        raise ValueError(
            f"Invalid logging level: {level}. "
            f"Valid levels are: {VALID_LOGGING_LEVELS}"
        )


def _validate_log_file(log_file: str) -> None:
    """Validate that the log file path is writable.

    Args:
        log_file: Path to the log file to validate

    Raises:
        ValueError: If the log file path is invalid or not writable
        TypeError: If the log_file parameter is not a string
    """
    if not isinstance(log_file, str):
        raise TypeError(f"log_file must be a string, got {type(log_file).__name__}")

    if not log_file or not log_file.strip():
        raise ValueError("log_file cannot be empty or whitespace")

    try:
        log_dir = os.path.dirname(log_file)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)
    except (OSError, IOError) as e:
        raise ValueError(f"Cannot create log directory: {e}")

    try:
        test_handle = open(log_file, 'a')
        test_handle.close()
    except (OSError, IOError, PermissionError) as e:
        raise ValueError(f"Cannot write to log file '{log_file}': {e}")


def setup_logging(
    level: int = logging.INFO,
    log_file: Optional[str] = None,
    format_string: str = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
) -> None:
    """Configure logging with console and optional file output.

    Args:
        level: Logging level (default: INFO)
        log_file: Optional file path for file logging output
        format_string: Log message format string

    Raises:
        ValueError: If logging level or log file path is invalid
        TypeError: If log_file parameter is not a string when provided
    """
    _validate_logging_level(level)

    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    root_logger.setLevel(level)

    console_formatter = logging.Formatter(
        format_string,
        datefmt="%H:%M:%S"
    )

    console_handler = SafeConsoleHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(console_formatter)
    console_handler.addFilter(ConsoleQuietFilter())
    root_logger.addHandler(console_handler)

    if log_file is not None:
        _validate_log_file(log_file)

        try:
            file_formatter = logging.Formatter(
                format_string,
                datefmt="%Y-%m-%d %H:%M:%S"
            )
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setLevel(level)
            file_handler.setFormatter(file_formatter)
            root_logger.addHandler(file_handler)

        except (OSError, IOError, PermissionError) as e:
            print(f"WARNING: Failed to setup file logging: {e}")
            print("Logging will continue with console output only.")

    # 提示词专用日志文件（与守护进程日志分离，仅 infrastructure.ai.* 日志写入）
    prompts_log_file = os.getenv("PROMPTS_LOG_FILE", "logs/prompts.log")
    if prompts_log_file:
        try:
            prompts_dir = os.path.dirname(prompts_log_file)
            if prompts_dir and not os.path.exists(prompts_dir):
                os.makedirs(prompts_dir, exist_ok=True)
            prompts_handler = logging.FileHandler(prompts_log_file, encoding="utf-8")
            prompts_handler.setLevel(logging.DEBUG)
            prompts_handler.setFormatter(logging.Formatter(
                format_string,
                datefmt="%Y-%m-%d %H:%M:%S"
            ))
            prompts_handler.addFilter(PromptLogFilter())
            root_logger.addHandler(prompts_handler)
        except (OSError, IOError, PermissionError) as e:
            print(f"WARNING: Failed to setup prompts log file: {e}")

    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    uvicorn_access_logger = logging.getLogger("uvicorn.access")
    uvicorn_access_logger.setLevel(logging.INFO)
    if not any(isinstance(f, UvicornAccessNoiseFilter) for f in uvicorn_access_logger.filters):
        uvicorn_access_logger.addFilter(UvicornAccessNoiseFilter())
    for handler in uvicorn_access_logger.handlers:
        if not any(isinstance(f, UvicornAccessNoiseFilter) for f in handler.filters):
            handler.addFilter(UvicornAccessNoiseFilter())
    logging.getLogger("fastapi").setLevel(logging.WARNING)


def configure_uvicorn_access_logging() -> None:
    """Re-apply access-log filters after uvicorn installs its own handlers."""
    filt = UvicornAccessNoiseFilter()
    for name in ("uvicorn.access",):
        logger_obj = logging.getLogger(name)
        if not any(isinstance(f, UvicornAccessNoiseFilter) for f in logger_obj.filters):
            logger_obj.addFilter(filt)
        for handler in logger_obj.handlers:
            if not any(isinstance(f, UvicornAccessNoiseFilter) for f in handler.filters):
                handler.addFilter(filt)

    # 仅在需要抓包时用 DEBUG_HTTP=1；否则控制台会被 httpx/httpcore 逐帧日志淹没
    if os.environ.get("DEBUG_HTTP", "").strip().lower() not in {"1", "true", "yes"}:
        for name in (
            "httpcore",
            "httpcore.http11",
            "httpcore.connection",
            "hpack",
            "hpack.hpack",
            "httpx",
            "anthropic",
            "anthropic._base_client",
        ):
            logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance with the specified name.

    Args:
        name: Logger name, typically __name__ of the calling module

    Returns:
        Logger instance configured with the global settings
    """
    return logging.getLogger(name)
