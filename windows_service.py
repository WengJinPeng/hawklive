from __future__ import annotations

import os
from pathlib import Path

if os.name != "nt":
    raise SystemExit("windows_service.py can only run on Windows")

import servicemanager
import win32event
import win32service
import win32serviceutil

import dashboard_server


class Dcp8001CollectorService(win32serviceutil.ServiceFramework):
    _svc_name_ = "HawkHiveDCP8001Collector"
    _svc_display_name_ = "HawkHive DCP8001 Collector"
    _svc_description_ = "Collects DCP8001 workshop data and uploads it to the configured cloud service."
    _svc_deps_ = ("Tcpip",)

    def __init__(self, args: list[str]) -> None:
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)

    def SvcStop(self) -> None:
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.stop_event)
        dashboard_server.request_shutdown()

    def SvcShutdown(self) -> None:
        self.SvcStop()

    def SvcDoRun(self) -> None:
        os.chdir(Path(__file__).resolve().parent)
        servicemanager.LogInfoMsg(f"{self._svc_name_} is starting")
        try:
            dashboard_server.run_collector("127.0.0.1", 8787)
        except Exception as exc:
            servicemanager.LogErrorMsg(f"{self._svc_name_} failed: {exc}")
            raise
        finally:
            servicemanager.LogInfoMsg(f"{self._svc_name_} stopped")


if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(Dcp8001CollectorService)
