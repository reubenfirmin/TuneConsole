"""Update only our own Flatpak through the host's permission-controlled portal.

The monitor belongs to its D-Bus connection. Keep that connection alive across HTTP
requests; disconnecting would cancel an installation. All methods run on the ASGI loop.
"""
import asyncio
import logging
import uuid

PORTAL = "org.freedesktop.portal.Flatpak"
PORTAL_PATH = "/org/freedesktop/portal/Flatpak"
MONITOR = PORTAL + ".UpdateMonitor"
log = logging.getLogger(__name__)


class PortalError(Exception):
    def __init__(self, name, message=""):
        super().__init__(message or name)
        self.name = name


class FlatpakUpdater:
    def __init__(self, *, bus_factory=None):
        self.instance = uuid.uuid4().hex
        self.phase = "available"
        self.message = ""
        self.progress = None
        self._bus_factory = bus_factory
        self._bus = None
        self._owner = None
        self._path = None
        self._early_signals = []
        self._connecting = asyncio.Lock()
        self._installing = False
        self._connected = False
        self._installed_update = False
        self._disconnect_task = None

    @property
    def busy(self):
        return self._installing or self.phase == "restarting"

    def snapshot(self):
        return {"instance": self.instance, "phase": self.phase,
                "message": self.message, "progress": self.progress}

    async def status(self):
        if not self._connected and self.phase != "unavailable":
            await self._connect()
        return self.snapshot()

    async def _call(self, member, *, path=PORTAL_PATH, interface=PORTAL,
                    signature="", body=None):
        from dbus_next import Message, MessageType
        reply = await asyncio.wait_for(self._bus.call(Message(
            destination=PORTAL, path=path, interface=interface,
            member=member, signature=signature, body=body or [])), timeout=15)
        if reply.message_type == MessageType.ERROR:
            raise PortalError(reply.error_name, str(reply.body[0]) if reply.body else "")
        return reply

    async def _connect(self):
        from dbus_next import Variant
        from dbus_next.aio import MessageBus
        async with self._connecting:
            if self._connected:
                return
            try:
                self._owner = None
                bus = (self._bus_factory or MessageBus)()
                self._bus = await asyncio.wait_for(bus.connect(), timeout=10)
                token = uuid.uuid4().hex
                sender = self._bus.unique_name[1:].replace(".", "_")
                self._path = f"{PORTAL_PATH}/update_monitor/{sender}/{token}"
                self._bus.add_message_handler(self._signal)
                reply = await self._call("CreateUpdateMonitor", signature="a{sv}",
                                         body=[{"handle_token": Variant("s", token)}])
                if reply.body != [self._path]:
                    raise PortalError("InvalidMonitor", "Unexpected update monitor path")
                self._owner = reply.sender
                self._connected = True
                # A unicast signal can arrive just before CreateUpdateMonitor's reply.
                early, self._early_signals = self._early_signals, []
                for message in early:
                    self._signal(message)
                self._disconnect_task = asyncio.create_task(self._watch_disconnect(bus))
            except Exception as exc:
                await self.close()
                self._error(exc)

    async def _watch_disconnect(self, bus):
        try:
            await bus.wait_for_disconnect()
        except Exception:
            log.warning("Flatpak update connection lost", exc_info=True)
        if self._bus is bus:
            self._bus = None
            self._connected = False
            self._installing = False
            self.phase = "error"
            self.message = "The update connection was lost. Please try again."

    def _signal(self, message):
        from dbus_next import MessageType
        if (message.message_type != MessageType.SIGNAL or message.path != self._path
                or message.interface != MONITOR or len(message.body) != 1):
            return
        if self._owner is None:
            if len(self._early_signals) < 16:
                self._early_signals.append(message)
            return
        if message.sender != self._owner:
            return
        info = {key: value.value for key, value in message.body[0].items()}
        if message.member == "UpdateAvailable":
            running, local, remote = (info.get(k) for k in
                                      ("running-commit", "local-commit", "remote-commit"))
            self._installed_update = bool(running and local and running != local)
            if self.busy:
                return
            if running and local and running != local:
                self.phase, self.message = "ready", "Update installed. Restart when you're ready."
            elif local and remote and local != remote:
                self.phase, self.message = "available", "A TuneConsole update is available."
        elif message.member == "Progress":
            status = info.get("status", 0)
            if status == 0:
                self.phase = "installing"
                self.message = "Downloading and installing the update…"
                count = max(1, info.get("n_ops", 1))
                self.progress = min(99, max(0, int(
                    (info.get("op", 0) * 100 + info.get("progress", 0)) / count)))
            elif status == 1:
                self._installing = False
                # A software manager may already have installed it while we were running.
                if self._installed_update:
                    self.phase, self.message = "ready", "Update installed. Restart when you're ready."
                else:
                    self.phase = "current"
                    self.message = "Your update channel has no newer update to install."
                self.progress = None
            elif status == 2:
                self._installing = False
                self._installed_update = True
                self.phase, self.progress = "ready", 100
                self.message = "Update installed. Restart when you're ready."
            elif status == 3:
                self._installing = False
                self._error(PortalError(info.get("error", ""), info.get("error_message", "")))

    def _error(self, exc):
        log.warning("Flatpak update failed: %s", exc)
        name = getattr(exc, "name", "")
        self.progress = None
        self.phase = "error"
        if name.endswith("AccessDenied"):
            self.phase = "unavailable"
            self.message = "Updates weren't allowed. You can update TuneConsole in your software manager."
        elif name.endswith(("NotSupported", "ServiceUnknown", "UnknownMethod")):
            self.phase = "unavailable"
            self.message = "This update needs your software manager. Open it to update TuneConsole."
        else:
            self.message = "The update couldn't be completed. Please try again."

    async def install(self):
        await self.status()
        if self.busy or self.phase in ("ready", "unavailable") or self._bus is None:
            return self.snapshot()
        self._installing = True
        self.phase, self.progress = "installing", None
        self.message = "Allow the update in the system dialog if prompted."
        try:
            # Update returns after dispatch; Progress signals report consent and installation.
            await self._call("Update", path=self._path, interface=MONITOR,
                             signature="sa{sv}", body=["", {}])
        except Exception as exc:
            self._installing = False
            self._error(exc)
        return self.snapshot()

    async def restart(self, host, port):
        if self.phase != "ready":
            raise ValueError("Install the update before restarting.")
        self.phase = "restarting"
        try:
            # The new sandbox survives our exit (no WATCH_BUS flag). Its launcher waits
            # for the old listener to disappear before binding, and doesn't open another tab.
            argv = ["/app/bin/yt-playlist", "--exit-on-idle", "--wait-for-port",
                    "--host", host, "--port", str(port)]
            await self._call("Spawn", signature="ayaaya{uh}a{ss}ua{sv}",
                             body=[b"/\0", [arg.encode() + b"\0" for arg in argv],
                                   {}, {}, 2, {}])  # LATEST_VERSION
        except Exception:
            self.phase = "ready"
            raise
        return self.snapshot()

    async def close(self):
        bus, self._bus = self._bus, None
        self._connected = False
        self._owner, self._path = None, None
        self._early_signals = []
        if bus is not None:
            bus.disconnect()
        task, self._disconnect_task = self._disconnect_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
