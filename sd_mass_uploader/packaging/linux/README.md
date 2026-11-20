# Linux packaging files

Installed by the `.deb` package, which `scripts/build_deb.py` builds (see the main README).
The package also adds `/usr/bin/sdu`, `/etc/sd-mass-uploader/config.yaml` and the folders under
`/var/lib/sd-mass-uploader` (`database` and `secrets` readable by the station user only).

| File | Goes to | What it does |
| --- | --- | --- |
| `sdu-daemon.service` | `/etc/systemd/system/` | The station service: starts at boot, restarts on failure |
| `50-sd-mass-uploader.rules` | `/etc/polkit-1/rules.d/` | Lets the `sdu` user mount cards (read-only is enforced in code) |
| `sd-uploader-console.desktop` | `/usr/share/applications/` | "SD Uploader" in the app menu: opens the console window |
| `sd-uploader-indicator.desktop` | `/etc/xdg/autostart/` | Panel icon and notifications at every login |
| `icons/sd-uploader.svg` | `/usr/share/icons/hicolor/scalable/apps/` | App icon |

The desktop parts need these system packages (the venv must be created with
`--system-site-packages` so it can use PyGObject):

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1 \
  gir1.2-ayatanaappindicator3-0.1 gir1.2-notify-0.7
```

Operators who use the console must be able to read the API token (`api.token_file`): add them
to the station's group, which owns the file with mode 0640.

On stock Ubuntu GNOME the panel icon needs the AppIndicator extension, which Ubuntu enables by
default. Without it, notifications still work. Check a machine with
`python main.py desktop --check`.
