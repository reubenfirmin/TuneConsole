"""Exercise real D-Bus messages against a fake portal on an isolated session bus.

These tests never connect to the user's portal, update a package, or stop a server.
"""
import asyncio
import shutil
import subprocess

import pytest

dbus_next = pytest.importorskip("dbus_next")
from dbus_next import Message, MessageType, Variant
from dbus_next.aio import MessageBus

from yt_playlist.core.flatpak_update import FlatpakUpdater, MONITOR, PORTAL, PORTAL_PATH


@pytest.fixture
def bus_address():
    if not shutil.which("dbus-daemon"):
        pytest.skip("dbus-daemon is required for the isolated portal test")
    process = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--print-address=1"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        address = process.stdout.readline().strip()
        assert address
        yield address
    finally:
        process.terminate()
        process.wait(timeout=5)


class FakePortal:
    def __init__(self, bus):
        self.bus = bus
        self.path = None
        self.sender = None
        self.installs = 0
        self.spawn = None
        self.spawns = []
        self.probe_exit = 0
        self.signals = []
        bus.add_message_handler(self.handle)

    def handle(self, message):
        if message.message_type != MessageType.METHOD_CALL:
            return
        if message.member == "CreateUpdateMonitor":
            token = message.body[0]["handle_token"].value
            sender = message.sender[1:].replace(".", "_")
            self.path = f"{PORTAL_PATH}/update_monitor/{sender}/{token}"
            self.sender = message.sender
            return Message.new_method_return(message, "o", [self.path])
        if message.member == "Update":
            assert message.path == self.path
            assert message.sender == self.sender  # the original connection must still own the monitor
            self.installs += 1
            return Message.new_method_return(message)
        if message.member == "Spawn":
            self.spawn = message
            self.spawns.append(message)
            pid = 12345 + len(self.spawns)
            if message.body[1][-1] == b"--help\0" and self.probe_exit is not None:
                # Intentionally send before the method reply to cover a fast child exit.
                self.bus.send(Message(destination=message.sender, path=PORTAL_PATH,
                                      interface=PORTAL, member="SpawnExited",
                                      message_type=MessageType.SIGNAL, signature="uu",
                                      body=[pid, self.probe_exit]))
            return Message.new_method_return(message, "u", [pid])
        if message.member == "SpawnSignal":
            self.signals.append(message.body)
            return Message.new_method_return(message)

    async def emit(self, member, info, *, sender_bus=None):
        data = {key: Variant("u" if isinstance(value, int) else "s", value)
                for key, value in info.items()}
        await (sender_bus or self.bus).send(Message(
            destination=self.sender, path=self.path, interface=MONITOR,
            member=member, message_type=MessageType.SIGNAL,
            signature="a{sv}", body=[data]))
        await asyncio.sleep(0.02)


async def portal_pair(address):
    service = await MessageBus(bus_address=address).connect()
    await service.request_name(PORTAL)
    portal = FakePortal(service)
    updater = FlatpakUpdater(bus_factory=lambda: MessageBus(bus_address=address))
    await updater.status()
    return service, portal, updater


def test_install_progress_and_latest_version_restart(bus_address):
    async def run():
        service, portal, updater = await portal_pair(bus_address)
        try:
            owner = updater._bus.unique_name
            assert (await updater.install())["phase"] == "installing"
            await updater.install()                 # a second tab must not start another installation
            assert portal.installs == 1
            assert (await updater.status())["instance"] == updater.instance
            assert updater._bus.unique_name == owner
            await portal.emit("Progress", {"status": 0, "n_ops": 2, "op": 1, "progress": 20})
            assert updater.snapshot()["progress"] == 60
            assert updater.busy
            await portal.emit("Progress", {"status": 2})
            assert updater.phase == "ready"
            assert not updater.busy
            await updater.restart("127.0.0.1", 9123)
            argv = [arg.rstrip(b"\0").decode() for arg in portal.spawn.body[1]]
            assert argv == ["/app/bin/yt-playlist", "--exit-on-idle", "--wait-for-port",
                            "--host", "127.0.0.1", "--port", "9123"]
            assert portal.spawn.body[4] == 2        # latest installed version; survives the old bus
            assert len(portal.spawns) == 2
            assert portal.spawns[0].body[4] == 18   # probe dies with this caller
            assert updater.phase == "restarting"
        finally:
            await updater.close()
            service.disconnect()
    asyncio.run(run())


@pytest.mark.parametrize("exit_status", [256, 9, None])
def test_incompatible_or_stalled_replacement_requires_manual_relaunch(bus_address, monkeypatch, exit_status):
    monkeypatch.setattr("yt_playlist.core.flatpak_update._START_CHECK_TIMEOUT_S", .05)

    async def run():
        service, portal, updater = await portal_pair(bus_address)
        try:
            portal.probe_exit = exit_status
            await portal.emit("Progress", {"status": 2})
            result = await updater.restart("127.0.0.1", 9123)
            assert result["phase"] == "manual_restart"
            assert "applications menu" in result["message"]
            assert not updater.busy
            assert len(portal.spawns) == 1
            assert portal.signals == ([[12346, 15, True]] if exit_status is None else [])
            assert (await updater.install())["phase"] == "manual_restart"
            assert portal.installs == 0
        finally:
            await updater.close()
            service.disconnect()
    asyncio.run(run())


@pytest.mark.parametrize("error, phase", [
    ("org.freedesktop.DBus.Error.AccessDenied", "unavailable"),
    ("org.freedesktop.DBus.Error.NotSupported", "unavailable"),
    ("org.freedesktop.DBus.Error.Failed", "error"),
])
def test_denial_permissions_change_and_download_failure(bus_address, error, phase):
    async def run():
        service, portal, updater = await portal_pair(bus_address)
        try:
            await updater.install()
            await portal.emit("Progress", {"status": 3, "error": error, "error_message": "failure"})
            assert updater.phase == phase
            assert not updater.busy
            assert updater.message
            if phase == "error":
                await updater.install()
                assert portal.installs == 2
        finally:
            await updater.close()
            service.disconnect()
    asyncio.run(run())


def test_software_manager_installed_update_and_no_op_install(bus_address):
    async def run():
        service, portal, updater = await portal_pair(bus_address)
        try:
            await updater.install()
            await portal.emit("UpdateAvailable", {"running-commit": "old", "local-commit": "new",
                                                  "remote-commit": "new"})
            await portal.emit("Progress", {"status": 1})
            assert updater.phase == "ready"         # an empty transaction still needs a restart
        finally:
            await updater.close()
            service.disconnect()
    asyncio.run(run())


def test_unrelated_signal_cannot_offer_a_restart(bus_address):
    async def run():
        service, portal, updater = await portal_pair(bus_address)
        intruder = await MessageBus(bus_address=bus_address).connect()
        try:
            await portal.emit("Progress", {"status": 2}, sender_bus=intruder)
            assert updater.phase == "available"
            with pytest.raises(ValueError):
                await updater.restart("127.0.0.1", 8765)
            assert portal.spawn is None
        finally:
            await updater.close()
            service.disconnect()
            intruder.disconnect()
    asyncio.run(run())
