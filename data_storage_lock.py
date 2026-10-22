"""Serialize related local/Git data writes inside one running app process."""
from functools import wraps
import threading

DATA_WRITE_LOCK = threading.RLock()


def with_data_write_lock(function):
    """Share one reentrant lock across adoption, refresh, and legacy writers."""
    @wraps(function)
    def locked(*args, **kwargs):
        with DATA_WRITE_LOCK:
            return function(*args, **kwargs)
    return locked
