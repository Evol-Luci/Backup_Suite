# Solace Backup: Background Mode & Waybar Integration

This document outlines the technical implementation of Solace Backup's background operation, its integration with system bars like Waybar, and how it handles user interactions via the system tray.

## 1. Background Mode Implementation

Solace Backup achieves background persistence by decoupling the application process's lifecycle from the main window's visibility.

### Mechanism
The application is built on `tkinter`, which runs a main event loop (`root.mainloop()`). Normally, closing the main window destroys this loop and terminates the process. Solace modifies this behavior:

*   **Hiding vs. Closing:** When the "background mode" triggers, the application does **not** close. Instead, it calls the tkinter `withdraw()` method on the main root window.
    ```python
    def hide_window(self):
        self.root.withdraw()
    ```
    `withdraw()` unmaps the window from the screen and the window manager's list, effectively making it invisible, but the `tkinter` mainloop continues to run. This allows scheduled jobs (handled by `APScheduler`) and the system tray icon listener to continue operating in the background.

### Restoring
To bring the application back, the `deiconify()` method is called, which remaps the window to the screen.
```python
def show_window(self):
    self.root.after(0, self.root.deiconify)
```

## 2. Circumventing User Closure

To prevent the user from accidentally killing the background process by clicking the "X" (close) button on the window title bar, Solace intercepts the window manager's close signal.

### Protocol Interception
In the `BackupApp.__init__` method, the application registers a handler for the `WM_DELETE_WINDOW` protocol:

```python
if TRAY_AVAILABLE:
    self.setup_tray_icon()
    self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
```

*   **Standard Behavior:** Normally, `WM_DELETE_WINDOW` triggers the destruction of the widget.
*   **Solace Behavior:** By binding this protocol to `self.hide_window`, Solace overrides the default destruction. Clicking "X" executes `hide_window()` instead, sending the app to the background.
*   **Exit Route:** The only way to fully terminate the application is via the "Exit" option in the system tray context menu, which calls `self.quit_application()`, performing a proper shutdown of the scheduler and tray icon before destroying the root window.

## 3. Waybar & System Tray Integration

Solace Backup does not communicate with Waybar directly via a custom API. Instead, it relies on the standard **StatusNotifierItem (SNI)** or **AppIndicator** protocols, which Waybar (and other bars like Polybar, Tint2, Plasma) supports.

### Library: `pystray`
The integration is handled by the `pystray` library. 
*   **Initialization:** When `self.setup_tray_icon()` is called, `pystray` creates a system tray icon.
*   **Icon Selection:** It attempts to load `Solace_Backup_Tray.png`. Using a PNG is preferred for Linux/Wayland compatibility over Windows `.ico` files.
*   **DBus Registration:** On Linux/Wayland, `pystray` registers the icon on the DBus. Waybar's `tray` module listens for these registrations and renders the icon in the bar.

### Waybar Configuration
For the icon to appear in Waybar, the `tray` module must be present in the Waybar config:
```json
"modules-right": [..., "tray", ...]
```

## 4. Right-Click Context Menu

The context menu that appears when right-clicking the tray icon is defined programmatically but rendered by the desktop environment (in this case, the specific implementation used by Waybar/Hyprland), giving it a native look and feel.

### Implementation
The menu is defined in `setup_tray_icon` as a tuple of `pystray.MenuItem` objects:

```python
menu = (
    pystray.MenuItem('Show', self.show_window, default=True),
    pystray.MenuItem('Run All Now', self.run_all_backups),
    pystray.Menu.SEPARATOR,
    pystray.MenuItem('Exit', self.quit_application)
)
self.tray_icon = pystray.Icon("BackupSuite", image, "Solace Backup Suite", menu)
```

1.  **Show:** Calls `show_window` to runs `deiconify()`, bringing the UI back. Marked as `default=True`, meaning it also triggers on a left-click (depending on the OS/Bar behavior).
2.  **Run All Now:** Triggers the `run_all_backups` method directly from the background, without needing the UI.
3.  **Exit:** Calls `quit_application` to terminate the program.

### Native Rendering
Because `pystray` acts as a bridge to the system's tray implementation:
*   Solace does **not** draw the menu using `tkinter` widgets.
*   It passes the menu structure to the system tray host (Waybar).
*   Waybar/Sway/Hyprland renders the menu using its own toolkit (e.g., GTK, Qt), ensuring the menu respects the user's system theme, rounded corners, and transparency settings automatically.
