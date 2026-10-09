"""Entry point for the GTK4 Backup Suite."""
import sys
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, GLib
from omarchy_backup_suite.ui.main_window import BackupMainWindow


class BackupApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.omarchy.solace.backup")
        self.connect("activate", self.on_activate)

    def on_activate(self, app):
        win = BackupMainWindow(app)
        win.present()


def main(tray_mode=False, minimized=False):
    app = BackupApp()
    app.run(sys.argv)
