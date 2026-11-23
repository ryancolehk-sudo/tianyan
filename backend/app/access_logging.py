"""Keep Uvicorn request diagnostics without retaining query parameters."""

import logging


class PrivateAccessLog(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn supplies (client, method, path_with_query, HTTP version, status).
        # Fail closed if that format changes instead of logging unchecked input.
        if not isinstance(record.args, tuple) or len(record.args) != 5:
            return False
        client, method, target, version, status = record.args
        if not isinstance(target, str):
            return False
        record.args = (client, method, target.split("?", 1)[0], version, status)
        return True
