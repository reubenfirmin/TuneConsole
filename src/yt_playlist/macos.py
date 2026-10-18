"""Cocoa application lifecycle and Sparkle integration for the frozen macOS app.

Imports stay inside run() so other platforms need neither Cocoa nor PyObjC.
The main thread runs Cocoa; the HTTP server runs in a worker and is drained
before Cocoa allows Sparkle (or Dock → Quit) to terminate the process.
"""
import logging
import os
from pathlib import Path
import queue
import signal
import threading
import time

from yt_playlist.core import logsetup
from yt_playlist.core.macos_update import MacOSUpdater, RESUME_KEY, launch_arguments


def run(argv):
    import objc
    from AppKit import (NSApplication, NSApplicationActivationPolicyRegular,
                        NSMenu, NSMenuItem, NSTerminateLater, NSTerminateNow)
    from Foundation import NSBundle, NSObject, NSTimer, NSUserDefaults

    from yt_playlist.__main__ import main

    logsetup.configure()
    bundle = NSBundle.mainBundle()
    framework = Path(bundle.bundlePath()) / "Contents/Frameworks/Sparkle.framework"
    objc.loadBundle("Sparkle", {}, bundle_path=str(framework))
    controller_class = objc.lookUpClass("SPUStandardUpdaterController")
    defaults = NSUserDefaults.standardUserDefaults()
    resume = defaults.dictionaryForKey_(RESUME_KEY)
    defaults.removeObjectForKey_(RESUME_KEY)  # consume once, even if launch fails
    argv = launch_arguments(argv, resume, no_open=os.environ.get("YT_PLAYLIST_NO_OPEN") == "1")
    updater = MacOSUpdater()
    native = {"controller": None, "quit_pending": False, "finished": False, "relaunch": False}
    quit_requested = threading.Event()

    def serve():
        try:
            main(argv, updater=updater, server_callback=updater.attach)
        except BaseException:
            logging.getLogger(__name__).exception("macOS server stopped unexpectedly")
        finally:
            native["finished"] = True

    server_thread = threading.Thread(target=serve, name="TuneConsole HTTP", daemon=True)

    # Sparkle has no generated PyObjC metadata. Declare its callback ABIs explicitly;
    # the update-check argument is an NSInteger rather than an Objective-C object.
    class TuneConsoleDelegate(NSObject, protocols=[objc.protocolNamed("NSApplicationDelegate"),
                                                 objc.protocolNamed("NSMenuItemValidation")]):
        def applicationDidFinishLaunching_(self, notification):
            native["controller"] = controller_class.alloc().initWithStartingUpdater_updaterDelegate_userDriverDelegate_(
                os.environ.get("TUNECONSOLE_DISABLE_UPDATES") != "1", self, None)
            server_thread.start()

        @objc.typedSelector(b"v@:@")
        def checkForUpdates_(self, sender):
            controller = native["controller"]
            if controller is not None:
                updater.set_state("installing", "Continue in the macOS update window.")
                app.activateIgnoringOtherApps_(True)
                controller.checkForUpdates_(sender)

        def validateMenuItem_(self, item):
            controller = native["controller"]
            return bool(controller and controller.updater().canCheckForUpdates())

        def applicationShouldTerminate_(self, sender):
            if native["finished"]:
                return NSTerminateNow
            native["quit_pending"] = True
            updater.request_quit()
            return NSTerminateLater

        def applicationWillTerminate_(self, notification):
            if native["relaunch"]:
                # Write after active work has drained, so a long sync cannot age out
                # the handoff. Sparkle relaunches through Launch Services without argv.
                defaults.setObject_forKey_({"argv": [arg for arg in argv if arg != "--open"],
                                            "time": time.time()}, RESUME_KEY)
                defaults.synchronize()

        @objc.typedSelector(b"v@:@")
        def tick_(self, timer):
            if quit_requested.is_set():
                quit_requested.clear()
                app.terminate_(None)
            try:
                while True:
                    command = updater.commands.get_nowait()
                    if command == "check":
                        self.checkForUpdates_(None)
            except queue.Empty:
                pass
            if native["quit_pending"]:
                updater.poll_quit()
                if native["finished"]:
                    app.replyToApplicationShouldTerminate_(True)
            elif native["finished"]:
                app.terminate_(None)

        @objc.typedSelector(b"v@:@@")
        def updater_didFindValidUpdate_(self, sender, item):
            updater.set_state("installing", "An update is ready. Continue in the macOS update window.")

        @objc.typedSelector(b"v@:@@@")
        def updater_willDownloadUpdate_withRequest_(self, sender, item, request):
            updater.set_state("installing", "Downloading the update. Progress is in the macOS update window.")

        @objc.typedSelector(b"v@:@@")
        def updater_didExtractUpdate_(self, sender, item):
            updater.set_state("installing", "Choose Install and Relaunch in the macOS update window.")

        @objc.typedSelector(b"v@:@")
        def updaterWillRelaunchApplication_(self, sender):
            native["relaunch"] = True

        @objc.typedSelector(b"v@:@q@")
        def updater_didFinishUpdateCycleForUpdateCheck_error_(self, sender, check, error):
            # Codes are Sparkle's SUNoUpdateError / SUInstallationCanceledError.
            if error is not None and error.code() == 1001:
                updater.set_state("current", "Your update channel has no newer update to install.")
            elif error is not None and error.code() != 4007:
                logging.getLogger(__name__).warning("Sparkle update failed: %s", error)
                updater.set_state("error", "The update couldn't be completed. Please try again.")
            else:
                updater.set_state("available")

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    delegate = TuneConsoleDelegate.alloc().init()
    app.setDelegate_(delegate)
    menu = NSMenu.alloc().init()
    application_item = NSMenuItem.alloc().init()
    menu.addItem_(application_item)
    application_menu = NSMenu.alloc().initWithTitle_("TuneConsole")
    check_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Check for Updates…", "checkForUpdates:", "")
    check_item.setTarget_(delegate)
    application_menu.addItem_(check_item)
    application_menu.addItem_(NSMenuItem.separatorItem())
    application_menu.addItemWithTitle_action_keyEquivalent_("Quit TuneConsole", "terminate:", "q")
    application_item.setSubmenu_(application_menu)
    app.setMainMenu_(menu)
    timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        0.2, delegate, "tick:", None, True)
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: quit_requested.set())
    app.run()
    timer.invalidate()
