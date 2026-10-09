#!/usr/bin/env python3
"""GTK4 entry point for Solace Backup Suite.

Usage:
    python3 backup_suite_gtk.py          # Launch main window
    python3 backup_suite_gtk.py --tray   # Start in system tray
    python3 backup_suite_gtk.py --minimized  # Start minimized
"""
import sys
import os
from pathlib import Path

# Ensure backup-suite directory is on path
sys.path.insert(0, str(Path(__file__).parent))


def main():
    tray_mode = "--tray" in sys.argv
    minimized = "--minimized" in sys.argv

    from omarchy_backup_suite.main import main as run_app
    run_app(tray_mode=tray_mode, minimized=minimized)


if __name__ == "__main__":
    main()
