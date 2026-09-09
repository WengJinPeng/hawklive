"""A thread-scoped Windows idle-sleep request, never a power-plan change."""
from __future__ import annotations

import ctypes
import logging
import os

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def execution_state_api():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    api = kernel.SetThreadExecutionState
    api.argtypes = [ctypes.c_uint32]
    api.restype = ctypes.c_uint32
    return api


class SystemSleepInhibitor:
    """Enter and exit on the same thread; Windows also clears it on thread exit.

    Keep the system running, but allow display timeout and user-initiated sleep.
    Failure is observable and does not stop collection or change OS settings.
    """

    def __init__(self):
        self.state = "inactive" if os.name == "nt" else "unsupported"
        self._api = None
        self._previous = None

    def __enter__(self):
        if os.name != "nt":
            return self
        try:
            self._api = execution_state_api()
            previous = self._api(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            if not previous:
                raise OSError("Windows rejected the idle-sleep prevention request")
            self._previous = previous
            self.state = "active"
        except (OSError, AttributeError):
            self.state = "failed"
            logging.getLogger(__name__).warning(
                "Automatic sleep prevention unavailable; collection will continue."
            )
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self._previous is not None:
            try:
                # Preserve any execution requirements the caller already held.
                if not self._api(self._previous | ES_CONTINUOUS):
                    raise OSError("Windows rejected execution-state restoration")
                self.state = "inactive"
            except OSError:
                self.state = "failed"
                logging.getLogger(__name__).warning(
                    "Sleep request could not be cleared; Windows clears it on thread exit."
                )
            finally:
                self._previous = None

    def status(self):
        return {"state": self.state, "system_required": self.state == "active"}
