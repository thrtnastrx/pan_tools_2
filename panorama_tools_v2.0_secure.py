import warnings
import sys
import os
import re
import fcntl

# Suppress early urllib3/ssl warnings on import
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL", category=Warning, module="urllib3.*")

import rumps
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import xml.etree.ElementTree as ET
import json
import webbrowser
import logging
import xml.dom.minidom
from logging.handlers import RotatingFileHandler
from AppKit import NSAlert, NSImage, NSApplicationActivateIgnoringOtherApps
from Foundation import NSURL
import objc
from urllib3.exceptions import InsecureRequestWarning, NotOpenSSLWarning
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from typing import Optional

# Secure credential storage
try:
    import keyring
    KEYRING_AVAILABLE = True
except ImportError:
    KEYRING_AVAILABLE = False
    print("WARNING: keyring not available. Credentials will be stored less securely.")

ICON_FILENAME = "pan-logo-1.png"
VERSION = "v2.0 Secure"
BUNDLE_ID = "com.yourcompany.panoramatools"
KEYRING_SERVICE = "PanoramaTools"

# Determine if running as bundled app or script
if getattr(sys, 'frozen', False):
    # Running as bundled app
    BUNDLE_DIR = os.path.dirname(sys.executable)
    ICON_PATH = os.path.join(BUNDLE_DIR, '..', 'Resources', ICON_FILENAME)
    APP_SUPPORT = os.path.expanduser('~/Library/Application Support/PanoramaTools')
    os.makedirs(APP_SUPPORT, exist_ok=True)
    LOGIN_FILE = os.path.join(APP_SUPPORT, "panorama_sync_login.json")
    LOG_FILE = os.path.join(APP_SUPPORT, "panorama_sync_log.txt")
    CUSTOM_CMDS_FILE = os.path.join(APP_SUPPORT, "custom_cli_commands.json")
    CLI_LOG_FILE = os.path.join(APP_SUPPORT, "cli_debug.log")
    WORK_DIR = APP_SUPPORT
    LOCK_FILE = os.path.join(APP_SUPPORT, "panorama_tools.lock")
else:
    # Running as script
    ICON_PATH = os.path.join(os.path.dirname(__file__), ICON_FILENAME)
    LOGIN_FILE = os.path.expanduser("~/.panorama_sync_login")
    LOG_FILE = os.path.join(os.getcwd(), "panorama_sync_log.txt")
    CUSTOM_CMDS_FILE = os.path.join(os.getcwd(), "custom_cli_commands.json")
    CLI_LOG_FILE = os.path.join(os.getcwd(), "cli_debug.log")
    WORK_DIR = os.getcwd()
    LOCK_FILE = os.path.join(os.getcwd(), "panorama_tools.lock")

# Setup rotating logging for CLI debug
log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
cli_handler = RotatingFileHandler(CLI_LOG_FILE, maxBytes=10*1024*1024, backupCount=5)
cli_handler.setFormatter(log_formatter)

root_logger = logging.getLogger()
root_logger.setLevel(logging.DEBUG)
root_logger.addHandler(cli_handler)

logging.captureWarnings(True)


class PanoramaSyncMonitor(rumps.App):
    def __init__(self, name="Panorama Tools"):
        app_icon = ICON_PATH if os.path.exists(ICON_PATH) else None
        super().__init__(name="Panorama Tools", icon=app_icon, quit_button=None)
        
        # Ensure the menu bar item appears even if the icon file is missing
        self.title = "Panorama Tools"
        if not app_icon:
            logging.warning("Menu bar icon not found at %s — showing title only.", ICON_PATH)
        
        # Acquire lock to prevent multiple instances
        if not self._acquire_lock():
            rumps.quit_application()
            return
        
        # Ensure working directory is accessible
        if not self._ensure_work_dir():
            rumps.alert("Cannot access Application Support folder. App may not work correctly.")
        
        self.panorama_url = None
        self.username = None
        self.password = None
        self.api_key = None
        self.verify_ssl = True  # Default to secure
        self.custom_ca_path = None
        self._override_cache = {}
        self._fw_items = {}
        self._fw_base = {}
        self._executor = ThreadPoolExecutor(max_workers=6)
        self._ui_update_lock = threading.Lock()
        self._pending_timers = set()

        # Reuse HTTP connections for speed (keep-alive + connection pool)
        self._http = requests.Session()
        try:
            retry = Retry(total=3, backoff_factor=0.3, status_forcelist=(429, 500, 502, 503, 504))
            adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=retry)
            self._http.mount('http://', adapter)
            self._http.mount('https://', adapter)
        except Exception:
            pass
        self._http.headers.update({'Accept-Encoding': 'gzip, deflate'})

        self.load_stored_login()

        self.firewalls_menu = rumps.MenuItem("Firewalls")
        self.device_groups_menu = rumps.MenuItem("Device Groups")
        self.templates_menu = rumps.MenuItem("Templates")
        self.firewalls_menu.add(rumps.MenuItem("No firewalls loaded"))
        self.device_groups_menu.add(rumps.MenuItem("No device groups loaded"))
        self.templates_menu.add(rumps.MenuItem("No templates loaded"))

        # Submenu listing devices with detected local overrides
        self.overrides_menu = rumps.MenuItem("Local Overrides")
        self.overrides_menu.add(rumps.MenuItem("None detected"))

        # Group commands into submenus for organization
        options_menu = rumps.MenuItem("Options")
        options_menu.add(rumps.MenuItem("Login to Panorama", callback=self.login_to_panorama))
        options_menu.add(rumps.MenuItem("Set Display Name", callback=self.set_display_name))
        options_menu.add(rumps.MenuItem("SSL Settings", callback=self.configure_ssl))
        options_menu.add(rumps.MenuItem("About", callback=self.show_about))

        fetch_menu = rumps.MenuItem("Fetch")
        fetch_menu.add(rumps.MenuItem("Fetch All Firewalls", callback=self.fetch_firewalls))
        fetch_menu.add(rumps.MenuItem("Fetch Connected Firewalls", callback=self.fetch_connected_firewalls))
        fetch_menu.add(rumps.MenuItem("Fetch Device Groups", callback=self.fetch_device_groups))
        fetch_menu.add(rumps.MenuItem("Fetch Templates", callback=self.fetch_templates))
        fetch_menu.add(rumps.MenuItem("Fetch All", callback=self.fetch_all))

        # Build the menu and optionally kick off a deferred fetch
        self.menu = [
            self.firewalls_menu,
            self.overrides_menu,
            self.device_groups_menu,
            self.templates_menu,
            None,
            fetch_menu,
            rumps.MenuItem("Check All for Local Overrides", callback=self.check_all_overrides),
            options_menu,
            rumps.MenuItem("Logout", callback=self.logout),
            rumps.MenuItem("Quit", callback=self.cleanup),
        ]

        # Defer auto-fetch so the menu bar item appears immediately (avoid blocking UI)
        if self.panorama_url and self.username and self.api_key:
            try:
                # Auto-fetch connected FWs, templates, and device groups shortly after startup
                self._initial_fetch_timer = rumps.Timer(self._deferred_fetch_on_launch, 0.2)
                self._initial_fetch_timer.start()
            except Exception as e:
                logging.warning("Failed to start deferred fetch timer: %s", e)

    def _acquire_lock(self):
        """Ensure only one instance runs at a time"""
        try:
            self._lock_fd = open(LOCK_FILE, 'w')
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_fd.write(str(os.getpid()))
            self._lock_fd.flush()
            return True
        except IOError:
            rumps.alert("Another instance of Panorama Tools is already running.")
            return False
        except Exception as e:
            self.log(f"Lock acquisition error: {e}")
            return True  # Continue anyway

    def _ensure_work_dir(self):
        """Ensure working directory exists and is writable"""
        try:
            os.makedirs(WORK_DIR, exist_ok=True)
            # Test write permission
            test_file = os.path.join(WORK_DIR, ".write_test")
            with open(test_file, 'w') as f:
                f.write("test")
            os.remove(test_file)
            return True
        except Exception as e:
            self.log(f"Work directory not writable: {e}")
            return False

    def _sanitize_log(self, message):
        """Remove sensitive data from log messages"""
        if self.api_key:
            message = message.replace(self.api_key, "***REDACTED***")
        if self.password:
            message = message.replace(self.password, "***REDACTED***")
        # Redact URL parameters that might contain keys
        message = re.sub(r'key=[^&\s]+', 'key=***REDACTED***', message)
        message = re.sub(r'password=[^&\s]+', 'password=***REDACTED***', message)
        return message

    def _secure_file_permissions(self, filepath):
        """Set restrictive permissions on sensitive files"""
        try:
            # Owner read/write only (600)
            os.chmod(filepath, 0o600)
        except Exception as e:
            self.log(f"Cannot set permissions on {filepath}: {e}")

    def _make_request(self, url, timeout=10):
        """Centralized request method with proper SSL handling"""
        verify = self.verify_ssl
        if self.custom_ca_path and os.path.exists(self.custom_ca_path):
            verify = self.custom_ca_path
        
        try:
            return self._http.get(url, verify=verify, timeout=timeout)
        except requests.exceptions.SSLError as e:
            self.log(f"SSL Error: {e}")
            # Prompt user about SSL issues
            response = rumps.alert(
                title="SSL Certificate Error",
                message="Cannot verify SSL certificate. Allow insecure connection this time?",
                ok="Allow Once",
                cancel="Cancel"
            )
            if response == 1:
                return self._http.get(url, verify=False, timeout=timeout)
            raise

    def configure_ssl(self, _):
        """Configure SSL verification settings"""
        current = "Enabled" if self.verify_ssl else "Disabled"
        ca = f"\nCA Path: {self.custom_ca_path}" if self.custom_ca_path else ""
        
        message = f"Current SSL Verification: {current}{ca}\n\nChoose an option:"
        
        alert = NSAlert.alloc().init()
        alert.setMessageText_("SSL Settings")
        alert.setInformativeText_(message)
        alert.addButtonWithTitle_("Enable SSL Verification")
        alert.addButtonWithTitle_("Disable SSL Verification (Insecure)")
        alert.addButtonWithTitle_("Set Custom CA Path")
        alert.addButtonWithTitle_("Cancel")
        
        if os.path.exists(ICON_PATH):
            icon_image = NSImage.alloc().initWithContentsOfFile_(ICON_PATH)
            if icon_image:
                alert.setIcon_(icon_image)
        
        from AppKit import NSApp
        NSApp.activateIgnoringOtherApps_(True)
        resp = alert.runModal()
        
        if resp == 1000:  # Enable
            self.verify_ssl = True
            self.custom_ca_path = None
            rumps.alert("SSL verification enabled.")
        elif resp == 1001:  # Disable
            confirm = rumps.alert(
                title="Warning",
                message="Disabling SSL verification is insecure and not recommended. Continue?",
                ok="Yes, Disable",
                cancel="Cancel"
            )
            if confirm == 1:
                self.verify_ssl = False
                rumps.alert("SSL verification disabled. This is insecure!")
        elif resp == 1002:  # Custom CA
            win = rumps.Window("Enter path to custom CA certificate:", "Custom CA Path")
            result = win.run()
            if result.clicked and result.text.strip():
                path = result.text.strip()
                if os.path.exists(path):
                    self.custom_ca_path = path
                    self.verify_ssl = True
                    rumps.alert(f"Custom CA set: {path}")
                else:
                    rumps.alert("File not found. SSL settings unchanged.")

    def cleanup(self, _):
        """Clean up resources before quitting"""
        try:
            self.log("Application shutting down...")
            
            # Stop all timers
            for timer in list(self._pending_timers):
                try:
                    timer.stop()
                except:
                    pass
            self._pending_timers.clear()
            
            # Shutdown executor
            self._executor.shutdown(wait=False)
            
            # Close HTTP session
            self._http.close()
            
            # Release lock
            if hasattr(self, '_lock_fd'):
                try:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                    self._lock_fd.close()
                except:
                    pass
                
            # Remove lock file
            try:
                if os.path.exists(LOCK_FILE):
                    os.remove(LOCK_FILE)
            except:
                pass
                
        except Exception as e:
            self.log(f"Cleanup error: {e}")
        finally:
            rumps.quit_application()

    def _rebuild_firewalls_menu_icons(self):
        """Rebuild Firewalls submenu titles so the ⚠️ icon displays reliably."""
        try:
            with self._ui_update_lock:
                # Preserve items but reset titles from base + cache state
                items = []
                for serial, item in self._fw_items.items():
                    base = self._fw_base.get(serial)
                    if not base:
                        continue
                    icon, hostname = base
                    cached = self._override_cache.get(serial)
                    has_override = cached[0] if isinstance(cached, tuple) else bool(cached)
                    title = f"{icon}{' ⚠️' if has_override else ''} {hostname}"
                    item.title = title
                    items.append((hostname.lower(), item))

                # Clear and re-add to force UI refresh
                self.firewalls_menu.clear()
                self.firewalls_menu.title = "Firewalls"
                for _, it in sorted(items, key=lambda x: x[0]):
                    self.firewalls_menu.add(it)
        except Exception as e:
            self.log(f"[override] rebuild firewalls menu failed: {e}")

    def _rebuild_overrides_menu(self):
        """Rebuild the Local Overrides submenu from the override cache."""
        try:
            with self._ui_update_lock:
                self.overrides_menu.clear()
                added = 0
                for serial, res in self._override_cache.items():
                    has_override = res[0] if isinstance(res, tuple) else bool(res)
                    if not has_override:
                        continue
                    host = self._fw_base.get(serial, ("", serial))[1]
                    mi = rumps.MenuItem(f"⚠️ {host} ({serial})")
                    # allow quick drill-down
                    mi.add(rumps.MenuItem("Check Local Overrides", callback=lambda _, s=serial: self._show_override_details(s)))
                    self.overrides_menu.add(mi)
                    added += 1
                if added == 0:
                    self.overrides_menu.add(rumps.MenuItem("None detected"))
        except Exception as e:
            self.log(f"[override] rebuild overrides menu failed: {e}")

    def _extract_network_subtree(self, xml_text):
        """Return serialized XML of the <network> subtree if present, else empty string."""
        try:
            root = ET.fromstring(xml_text if xml_text else "")
        except Exception:
            return ""
        # Try common locations
        net_elem = root.find(".//config/devices/entry[@name='localhost.localdomain']/network")
        if net_elem is None:
            net_elem = root.find(".//devices/entry[@name='localhost.localdomain']/network")
        if net_elem is None:
            net_elem = root.find(".//network")
        if net_elem is None:
            return ""
        try:
            return ET.tostring(net_elem, encoding="unicode")
        except Exception:
            return ""

    def _xml_path(self, elem):
        try:
            parts = []
            cur = elem
            while cur is not None and hasattr(cur, 'tag'):
                tag = cur.tag
                name = cur.attrib.get('name')
                parts.append(f"{tag}[@name='{name}']" if name else tag)
                cur = cur.getparent() if hasattr(cur, 'getparent') else None
            parts = list(reversed(parts))
            return '/' + '/'.join(parts) if parts else '(no-path)'
        except Exception:
            return '(path-unavailable)'

    def _find_meaningful_nodes(self, net_elem, limit=10):
        """Return a list of up to `limit` nodes under <network> considered 'meaningful' (attribs or text)."""
        hits = []
        if net_elem is None:
            return hits
        count = 0
        for e in net_elem.iter():
            if e is net_elem:
                continue
            if e.attrib or (e.text and e.text.strip()):
                name = e.attrib.get('name')
                txt = (e.text or '').strip()
                attr = f" name='{name}'" if name else ""
                brief = f"<{e.tag}{attr}>"
                if txt:
                    brief += f" text='{txt[:60]}{'...' if len(txt)>60 else ''}'"
                hits.append(brief)
                count += 1
                if count >= limit:
                    break
        return hits

    def _has_meaningful_network_config(self, net_elem):
        """Return True if there is real configuration under <network>."""
        if net_elem is None:
            return False
        for e in net_elem.iter():
            if e is net_elem:
                continue
            if e.attrib:
                return True
            if e.text and e.text.strip():
                return True
        return False

    def _fetch_config_network(self, serial, which):
        """Return XML text of the Network subtree for the device."""
        try:
            network_xp = "/config/devices/entry[@name='localhost.localdomain']/network"
            if which == "running":
                url = (
                    f"https://{self.panorama_url}/api/?key={self.api_key}"
                    f"&type=config&action=get&xpath={self._url_x(network_xp)}&target={serial}"
                )
                r = self._make_request(url, timeout=12)
                return r.text
            elif which == "pushed-template":
                cmd = "&cmd=<show><config><pushed-template></pushed-template></config></show>"
                url = f"https://{self.panorama_url}/api/?key={self.api_key}&type=op{cmd}&target={serial}"
                r = self._make_request(url, timeout=12)
                try:
                    root = ET.fromstring(r.text)
                    net_elem = root.find(
                        ".//config/devices/entry[@name='localhost.localdomain']/network"
                    )
                    if net_elem is not None:
                        return ET.tostring(net_elem, encoding="unicode")
                    return r.text
                except Exception:
                    return r.text
            else:
                return None
        except Exception as e:
            self.log(f"[override] fetch network {which} failed: {e}")
            return None

    def _deferred_fetch_on_launch(self, _):
        """On launch (or post-login), fetch all firewalls, templates, and device groups."""
        try:
            if self.panorama_url and self.api_key:
                self.fetch_firewalls(None)
                self.fetch_templates(None)
                self.fetch_device_groups(None)
        finally:
            try:
                if hasattr(self, "_initial_fetch_timer") and self._initial_fetch_timer:
                    self._initial_fetch_timer.stop()
            except Exception:
                pass

    def _on_main(self, fn, *args, **kwargs):
        """Execute function on main thread."""
        try:
            if threading.current_thread().name == 'MainThread':
                return fn(*args, **kwargs)
        except Exception:
            pass

        def cb(_):
            try:
                fn(*args, **kwargs)
            finally:
                try:
                    t = getattr(cb, '_timer_ref', None)
                    if t:
                        try:
                            t.stop()
                        except Exception:
                            pass
                        try:
                            self._pending_timers.discard(t)
                        except Exception:
                            pass
                except Exception:
                    pass

        timer = rumps.Timer(cb, 0.01)
        cb._timer_ref = timer
        try:
            self._pending_timers.add(timer)
        except Exception:
            pass
        timer.start()

    def _fetch_config_xml(self, serial, which):
        """Return XML text for firewall config via op cmd."""
        try:
            if which not in {"running", "pushed-template"}:
                return None
            cmd = f"&cmd=<show><config><{which}></{which}></config></show>"
            url = f"https://{self.panorama_url}/api/?key={self.api_key}&type=op{cmd}&target={serial}"
            r = self._make_request(url, timeout=8)
            return r.text
        except Exception as e:
            self.log(f"[override] fetch {which} failed: {e}")
            return None

    def _norm_serial(self, s):
        try:
            return str(s).strip().upper()
        except Exception:
            return str(s)

    def _detect_local_override(self, serial):
        """Detect if a firewall has local overrides by checking network configuration."""
        serial = self._norm_serial(serial)
        if serial in self._override_cache:
            return self._override_cache[serial]

        try:
            self.log(f"[override] Starting detection for {serial}")

            running_full = self._fetch_config_xml(serial, "running")
            if not running_full:
                self.log(f"[override] No running config retrieved for {serial}")
                res = (False, "No running config retrieved")
                self._override_cache[serial] = res
                return res

            running_net = self._extract_network_subtree(running_full)

            # Save for troubleshooting
            try:
                save_path = os.path.join(WORK_DIR, f"override_running_{serial}.xml")
                with open(save_path, "w") as f:
                    f.write(running_net or "")
                self._secure_file_permissions(save_path)
            except Exception as e2:
                self.log(f"[override] write running network xml failed: {e2}")

            def to_elem(xml_text):
                try:
                    return ET.fromstring(xml_text) if xml_text else None
                except Exception:
                    return None

            run_elem = to_elem(running_net)
            has_meaning = self._has_meaningful_network_config(run_elem)

            if has_meaning:
                hits = self._find_meaningful_nodes(run_elem, limit=5)
                summary = "Running /network contains local config:\n- " + "\n- ".join(hits) if hits else "Running /network contains local config."
                res = (True, summary)
                self.log(f"[override] {serial}: LOCAL OVERRIDE DETECTED - {len(hits)} meaningful elements")
            else:
                res = (False, "Running /network is empty (no local override).")
                self.log(f"[override] {serial}: No override - empty network config")

            self._override_cache[serial] = res
            return res

        except Exception as e:
            self.log(f"[override] detection failed for {serial}: {e}")
            res = (False, f"Detection error: {e}")
            self._override_cache[serial] = res
            return res

    def _ensure_override_listed(self, serial, has_override):
        """Ensure Local Overrides submenu reflects the given serial's state immediately."""
        try:
            norm = self._norm_serial(serial)
            with self._ui_update_lock:
                placeholders = [mi for mi in self.overrides_menu if getattr(mi, 'title', '') == 'None detected']
                for mi in placeholders:
                    self.overrides_menu.remove(mi)
                to_remove = []
                for mi in self.overrides_menu:
                    if f"({norm})" in getattr(mi, 'title', ''):
                        to_remove.append(mi)
                for mi in to_remove:
                    self.overrides_menu.remove(mi)
                if has_override:
                    host = self._fw_base.get(norm, ("", norm))[1]
                    mi = rumps.MenuItem(f"⚠️ {host} ({norm})")
                    mi.add(rumps.MenuItem("Check Local Overrides", callback=lambda _, s=norm: self._show_override_details(s)))
                    self.overrides_menu.add(mi)
                if not list(self.overrides_menu):
                    self.overrides_menu.add(rumps.MenuItem("None detected"))
                if hasattr(self.overrides_menu, 'update'):
                    self.overrides_menu.update()
            try:
                self._rebuild_overrides_menu()
            except Exception:
                pass
        except Exception as e:
            self.log(f"[override] ensure list failed for {serial}: {e}")

    def _update_override_icon(self, serial, has_override):
        """Update the override icon for a firewall in the UI."""
        try:
            norm_serial = self._norm_serial(serial)
            self.log(f"[override] Updating icon for {norm_serial}: has_override={has_override}")

            item = self._fw_items.get(norm_serial)
            base = self._fw_base.get(norm_serial)

            if not item:
                self.log(f"[override] No menu item found for serial '{norm_serial}'")
                return

            if base:
                icon, hostname = base
                override_icon = " ⚠️" if has_override else ""
                new_title = f"{icon}{override_icon} {hostname}"
                self.log(f"[override] Setting title for {norm_serial}: '{new_title}'")
                item.title = new_title
            else:
                clean_title = item.title.replace(" ⚠️", "").strip()
                new_title = f"{clean_title} ⚠️" if has_override else clean_title
                item.title = new_title
                self.log(f"[override] Fallback title for {norm_serial}: '{new_title}'")

            current_cache = self._override_cache.get(norm_serial)
            if isinstance(current_cache, tuple):
                self._override_cache[norm_serial] = (has_override, current_cache[1])
            else:
                self._override_cache[norm_serial] = (has_override, "Icon updated")

            self._ensure_override_listed(norm_serial, has_override)
            self._rebuild_firewalls_menu_icons()
            self._rebuild_overrides_menu()

        except Exception as e:
            self.log(f"[override] Error updating icon for {serial}: {e}")

    def _notify(self, title, subtitle, message):
        """Post a macOS notification."""
        try:
            rumps.notification(title, subtitle, message)
        except Exception as e:
            try:
                self.log(f"[notify] Notification failed: {e}")
            except Exception:
                pass

    def check_all_overrides(self, _):
        """Fast, multi-threaded local override check with batched UI updates."""
        try:
            self.log("[override] Starting bulk override check (fast, multi-threaded)")
            self._override_cache.clear()

            serials = list(self._fw_items.keys())
            if not serials:
                self.log("[override] No firewalls to check.")
                self._notify("Panorama Sync", "Override Check", "No firewalls loaded to check")
                return

            self._notify("Panorama Sync", "Override Check", f"Checking {len(serials)} firewalls…")

            results = []

            def worker(s):
                try:
                    has_override, summary = self._detect_local_override(s)
                    return (s, has_override)
                except Exception as e:
                    self.log(f"[override] worker error for {s}: {e}")
                    return (s, False)

            max_workers = 12
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(worker, s): s for s in serials}
                batch = []
                for fut in futures:
                    try:
                        s, has_override = fut.result(timeout=40)
                        results.append((s, has_override))
                        batch.append((s, has_override))
                        if len(batch) >= 10:
                            upd = list(batch)
                            batch.clear()
                            def do_updates():
                                for ss, hov in upd:
                                    self._update_override_icon(ss, hov)
                                self._rebuild_overrides_menu()
                                self._rebuild_firewalls_menu_icons()
                            self._on_main(do_updates)
                    except Exception as e:
                        s = futures[fut]
                        self.log(f"[override] future error for {s}: {e}")

                if batch:
                    upd = list(batch)
                    def do_updates_last():
                        for ss, hov in upd:
                            self._update_override_icon(ss, hov)
                        self._rebuild_overrides_menu()
                        self._rebuild_firewalls_menu_icons()
                    self._on_main(do_updates_last)

            override_count = sum(1 for _, hov in results if hov)
            self.log(f"[override] Bulk check complete. Found {override_count} overrides")

            if override_count > 0:
                self._notify("Panorama Sync", "Override Check Complete", f"Found {override_count} firewalls with local overrides")
            else:
                self._notify("Panorama Sync", "Override Check Complete", "No local overrides detected")

        except Exception as e:
            self.log(f"[override] bulk check error: {e}")
            rumps.alert(f"Override check failed: {e}")

    def _show_override_details(self, serial):
        has_override, summary = self._detect_local_override(serial)
        self._update_override_icon(serial, has_override)
        saved_run = os.path.join(WORK_DIR, f"override_running_{serial}.xml")
        detail_text = None
        if has_override:
            try:
                with open(saved_run, "r") as f:
                    run_xml = f.read()
                try:
                    import xml.dom.minidom as minidom
                    dom = minidom.parseString(run_xml)
                    pretty = dom.toprettyxml(indent="  ")
                    lines = [line for line in pretty.splitlines() if line.strip()]
                    detail_text = "\n".join(lines)
                except Exception:
                    detail_text = run_xml
            except Exception as e:
                detail_text = f"(Failed to load running config: {e})\n\n" + (summary or "")
            detail_text += f"\n\n— Showing running /network —\nSaved file:\n- running: {saved_run}"
        else:
            detail_text = (summary or "") + f"\n\nSaved /network XML file:\n- running: {saved_run}"
        
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Local Override Check")

        heading = ("⚠️ Local overrides detected — showing running /network") if has_override else ("No local overrides detected.")
        alert.setInformativeText_(heading)

        try:
            from AppKit import NSScrollView, NSTextView, NSFont, NSMakeSize
            scroll_frame = ((0, 0), (600, 320))
            scroll = NSScrollView.alloc().initWithFrame_(scroll_frame)
            scroll.setHasVerticalScroller_(True)
            scroll.setHasHorizontalScroller_(True)
            scroll.setAutohidesScrollers_(False)
            text_view = NSTextView.alloc().initWithFrame_(scroll_frame)
            text_view.setEditable_(False)
            text_view.setSelectable_(True)
            try:
                mono = NSFont.monospacedSystemFontOfSize_weight_(11.0, 0.0)
                text_view.setFont_(mono)
            except Exception:
                pass
            text_view.setString_(detail_text or "")
            try:
                text_view.setRichText_(False)
                text_view.setHorizontallyResizable_(True)
                text_view.setVerticallyResizable_(True)
                tc = text_view.textContainer()
                if tc:
                    tc.setContainerSize_(NSMakeSize(10_000_000.0, 10_000_000.0))
                    tc.setWidthTracksTextView_(False)
            except Exception:
                pass
            try:
                sample = (detail_text or "").lstrip()[:5]
                if sample.startswith("<"):
                    self._apply_xml_highlighting(text_view)
            except Exception:
                pass
            scroll.setDocumentView_(text_view)
            alert.setAccessoryView_(scroll)
        except Exception as e:
            trimmed = (detail_text[:1800] + "\n... (truncated)") if len(detail_text) > 1800 else (detail_text or "")
            alert.setInformativeText_(f"{heading}\n\n{trimmed}")

        if os.path.exists(ICON_PATH):
            icon_image = NSImage.alloc().initWithContentsOfFile_(ICON_PATH)
            if icon_image:
                alert.setIcon_(icon_image)
        
        from AppKit import NSApp
        alert.addButtonWithTitle_("OK")
        alert.addButtonWithTitle_("Copy Visible")

        NSApp.activateIgnoringOtherApps_(True)
        resp = alert.runModal()
        if resp == 1001:
            try:
                from AppKit import NSPasteboard, NSPasteboardTypeString
                pb = NSPasteboard.generalPasteboard()
                pb.clearContents()
                pb.setString_forType_(detail_text or "", NSPasteboardTypeString)
            except Exception as e:
                print(f"Copy Visible failed: {e}")

    def set_display_name(self, _):
        win = rumps.Window("Enter new display name for the menu:", "Set Display Name")
        result = win.run()
        if result.clicked and result.text.strip():
            self.title = result.text.strip()

    def _apply_xml_highlighting(self, text_view):
        """Apply simple XML syntax coloring to the NSTextView."""
        try:
            import re
            from AppKit import NSColor, NSFont, NSForegroundColorAttributeName
            from Foundation import NSMutableAttributedString

            storage = text_view.textStorage()
            if storage is None:
                return
            full_text = str(storage.string()) if hasattr(storage, 'string') else text_view.string()
            if not full_text:
                return

            attr = {
                NSForegroundColorAttributeName: NSColor.whiteColor(),
            }
            attributed = NSMutableAttributedString.alloc().initWithString_attributes_(full_text, attr)

            tag_pattern = re.compile(r"</?([A-Za-z_][\w:.-]*)([^>]*)>")
            attr_pattern = re.compile(r"([A-Za-z_][\w:.-]*)(\s*=\s*)\"([^\"]*)\"")
            comment_pattern = re.compile(r"<!--.*?-->", re.DOTALL)
            error_word_pattern = re.compile(r"\berror\b", re.IGNORECASE)

            def color_range(start, length, color):
                try:
                    attributed.addAttribute_value_range_(NSForegroundColorAttributeName, color, (start, length))
                except Exception:
                    pass

            text_str = full_text

            for m in comment_pattern.finditer(text_str):
                color_range(m.start(), m.end() - m.start(), NSColor.grayColor())

            for m in tag_pattern.finditer(text_str):
                start, end = m.start(), m.end()
                color_range(start, end - start, NSColor.systemBlueColor() if hasattr(NSColor, 'systemBlueColor') else NSColor.blueColor())
                inner = m.group(2) or ""
                if inner:
                    inner_start = start + text_str[start:end].find(inner)
                    for am in attr_pattern.finditer(inner):
                        val_group = am.group(3)
                        rel = am.start(3)
                        abs_start = inner_start + rel
                        abs_end = abs_start + len(val_group)
                        color_range(abs_start, abs_end - abs_start, NSColor.controlHighlightColor())

            for m in error_word_pattern.finditer(text_str):
                color_range(m.start(), m.end() - m.start(), NSColor.systemRedColor() if hasattr(NSColor, 'systemRedColor') else NSColor.redColor())

            storage.setAttributedString_(attributed)
        except Exception as e:
            try:
                print(f"XML highlighting skipped: {e}")
            except Exception:
                pass

    def _apply_human_highlighting(self, text_view):
        """Apply simple 'Human Readable' coloring."""
        try:
            from AppKit import NSColor
            from Foundation import NSMutableAttributedString, NSMakeRange
            from AppKit import NSForegroundColorAttributeName

            storage = text_view.textStorage()
            full_text = str(storage.string()) if hasattr(storage, 'string') else text_view.string()
            if not full_text:
                return

            attributed = NSMutableAttributedString.alloc().initWithString_attributes_(
                full_text,
                {NSForegroundColorAttributeName: NSColor.whiteColor()}
            )

            offset = 0
            for line in full_text.splitlines(keepends=True):
                colon_idx = -1
                for i, ch in enumerate(line):
                    if ch == ':':
                        colon_idx = i
                        break
                if colon_idx > 0:
                    rng = (offset, colon_idx + 1)
                    try:
                        attributed.addAttribute_value_range_(
                            NSForegroundColorAttributeName,
                            NSColor.systemBlueColor() if hasattr(NSColor, 'systemBlueColor') else NSColor.blueColor(),
                            rng
                        )
                    except Exception:
                        pass
                offset += len(line)

            storage.setAttributedString_(attributed)
        except Exception as e:
            try:
                print(f"Human highlighting skipped: {e}")
            except Exception:
                pass

    def _show_tabbed_alert(self, heading: str, tabs: list, width: int = 720, height: int = 400):
        """Show an NSAlert with a tabbed accessory view."""
        try:
            from AppKit import (
                NSAlert, NSTabView, NSTabViewItem, NSScrollView, NSTextView,
                NSFont, NSImage, NSApp, NSMakeRect
            )

            alert = NSAlert.alloc().init()
            alert.setMessageText_(heading)

            tab_view = NSTabView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))

            for (tab_title, content_text) in tabs:
                scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
                scroll.setHasVerticalScroller_(True)
                scroll.setHasHorizontalScroller_(True)

                text_view = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
                text_view.setEditable_(False)
                text_view.setSelectable_(True)
                try:
                    mono = NSFont.monospacedSystemFontOfSize_weight_(11.0, 0.0)
                    text_view.setFont_(mono)
                except Exception:
                    pass
                text_view.setString_(content_text or "")
                try:
                    from AppKit import NSMakeSize
                    text_view.setRichText_(False)
                    text_view.setHorizontallyResizable_(True)
                    text_view.setVerticallyResizable_(True)
                    tc = text_view.textContainer()
                    if tc:
                        tc.setContainerSize_(NSMakeSize(10_000_000.0, 10_000_000.0))
                        tc.setWidthTracksTextView_(False)
                    scroll.setHasHorizontalScroller_(True)
                    scroll.setAutohidesScrollers_(False)
                except Exception:
                    pass
                
                tab_lower = (tab_title or "").strip().lower()
                if tab_lower == "raw":
                    self._apply_xml_highlighting(text_view)
                elif tab_lower == "human readable":
                    try:
                        from AppKit import NSMakeSize
                        text_view.setRichText_(False)
                        text_view.setHorizontallyResizable_(True)
                        tc = text_view.textContainer()
                        if tc:
                            tc.setContainerSize_(NSMakeSize(10_000_000.0, 10_000_000.0))
                            tc.setWidthTracksTextView_(False)
                        scroll.setHasHorizontalScroller_(True)
                        scroll.setAutohidesScrollers_(False)
                    except Exception:
                        pass
                    self._apply_human_highlighting(text_view)

                scroll.setDocumentView_(text_view)

                tab_item = NSTabViewItem.alloc().initWithIdentifier_(tab_title)
                tab_item.setLabel_(tab_title)
                tab_item.setView_(scroll)
                tab_view.addTabViewItem_(tab_item)

            alert.setAccessoryView_(tab_view)

            if os.path.exists(ICON_PATH):
                icon_image = NSImage.alloc().initWithContentsOfFile_(ICON_PATH)
                if icon_image:
                    alert.setIcon_(icon_image)

            alert.addButtonWithTitle_("OK")
            alert.addButtonWithTitle_("Copy Visible")

            NSApp.activateIgnoringOtherApps_(True)
            resp = alert.runModal()
            if resp == 1001:
                try:
                    current_item = tab_view.selectedTabViewItem()
                    scroll = current_item.view()
                    tv = scroll.documentView()
                    visible_text = tv.string()
                    from AppKit import NSPasteboard, NSPasteboardTypeString
                    pb = NSPasteboard.generalPasteboard()
                    pb.clearContents()
                    pb.setString_forType_(visible_text or "", NSPasteboardTypeString)
                except Exception as e:
                    print(f"Copy Visible failed: {e}")
        except Exception as e:
            joined = []
            for (t, txt) in tabs:
                joined.append(f"===== {t} =====\n{txt}")
            self._show_monospaced_alert(heading, "\n\n".join(joined), width=width, height=height)

    def _show_monospaced_alert(self, heading: str, detail_text: str, width: int = 640, height: int = 360):
        """Show a nicely formatted alert with monospaced text."""
        try:
            from AppKit import NSAlert, NSScrollView, NSView, NSMakeRect, NSSize, NSTextView, NSFont, NSImage, NSApp

            alert = NSAlert.alloc().init()
            alert.setMessageText_(heading)

            scroll_frame = NSMakeRect(0, 0, width, height)
            scroll = NSScrollView.alloc().initWithFrame_(scroll_frame)
            scroll.setHasVerticalScroller_(True)
            scroll.setHasHorizontalScroller_(True)
            scroll.setBorderType_(0)

            content_size = scroll.contentSize()
            text_view = NSTextView.alloc().initWithFrame_(((0, 0), (content_size.width, content_size.height)))
            text_view.setEditable_(False)
            text_view.setSelectable_(True)
            try:
                mono = NSFont.monospacedSystemFontOfSize_weight_(11.0, 0.0)
                text_view.setFont_(mono)
            except Exception:
                pass
            text_view.setString_(detail_text)

            scroll.setDocumentView_(text_view)
            alert.setAccessoryView_(scroll)

            if os.path.exists(ICON_PATH):
                icon_image = NSImage.alloc().initWithContentsOfFile_(ICON_PATH)
                if icon_image:
                    alert.setIcon_(icon_image)

            NSApp.activateIgnoringOtherApps_(True)
            alert.runModal()
        except Exception as e:
            try:
                from AppKit import NSAlert, NSImage, NSApp
                alert = NSAlert.alloc().init()
                alert.setMessageText_(heading)
                trimmed = (detail_text[:1800] + "\n... (truncated)") if len(detail_text) > 1800 else detail_text
                alert.setInformativeText_(trimmed)

                if os.path.exists(ICON_PATH):
                    icon_image = NSImage.alloc().initWithContentsOfFile_(ICON_PATH)
                    if icon_image:
                        alert.setIcon_(icon_image)

                NSApp.activateIgnoringOtherApps_(True)
                alert.runModal()
            except Exception:
                print(detail_text)

    def log(self, message):
        try:
            sanitized = self._sanitize_log(message)
            handler = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=5)
            handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            logger = logging.getLogger(f"PanoramaSyncLogger_{id(self)}")
            if not logger.handlers:
                logger.addHandler(handler)
                logger.setLevel(logging.INFO)
            logger.info(sanitized)
            print(f"[{time.strftime('%H:%M:%S')}] {sanitized}")
        except Exception as e:
            print(f"Logging error: {e}")

    # ==== Custom CLI Commands (persisted) ====
    def _load_custom_commands(self) -> list:
        try:
            if os.path.exists(CUSTOM_CMDS_FILE):
                with open(CUSTOM_CMDS_FILE, 'r') as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return data
        except Exception as e:
            self.log(f"[CustomCmds] Load error: {e}")
        return []

    def _save_custom_commands(self, cmds: list):
        try:
            with open(CUSTOM_CMDS_FILE, 'w') as f:
                json.dump(cmds, f, indent=2)
            self._secure_file_permissions(CUSTOM_CMDS_FILE)
        except Exception as e:
            self.log(f"[CustomCmds] Save error: {e}")

    def _add_custom_command_ui(self, _=None):
        from AppKit import NSApp, NSAlert, NSTextField, NSView, NSMakeRect
        NSApp.activateIgnoringOtherApps_(True)
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Add Custom Command")
        alert.setInformativeText_("Enter a label and the CLI command (e.g., show interface all)")
        label_field = NSTextField.alloc().initWithFrame_(((0,0),(320,24)))
        cmd_field = NSTextField.alloc().initWithFrame_(((0,30),(320,24)))
        label_field.setPlaceholderString_("Label (e.g., Show Interfaces)")
        cmd_field.setPlaceholderString_("CLI command (e.g., show interface all)")
        view = NSView.alloc().initWithFrame_(NSMakeRect(0,0,320,60))
        view.addSubview_(label_field)
        view.addSubview_(cmd_field)
        alert.setAccessoryView_(view)
        alert.addButtonWithTitle_("Save")
        alert.addButtonWithTitle_("Cancel")
        resp = alert.runModal()
        if resp == 1000:
            label = label_field.stringValue().strip()
            cmd = cmd_field.stringValue().strip()
            if label and cmd:
                cmds = self._load_custom_commands()
                cmds = [c for c in cmds if c.get('label') != label]
                cmds.append({'label': label, 'cmd': cmd})
                self._save_custom_commands(cmds)
                try:
                    self.fetch_firewalls(None)
                except Exception:
                    pass

    def _delete_custom_command_ui(self, _=None):
        cmds = self._load_custom_commands()
        if not cmds:
            rumps.alert("No custom commands saved yet.")
            return
        listing = "\n".join([f"{i+1}. {c['label']} — {c['cmd']}" for i,c in enumerate(cmds)])
        win = rumps.Window(f"Select number to delete:\n\n{listing}", title="Delete Custom Command", default_text="", dimensions=(520, 200))
        res = win.run()
        if not res.clicked:
            return
        txt = (res.text or "").strip()
        if not txt.isdigit():
            return
        idx = int(txt) - 1
        if 0 <= idx < len(cmds):
            del cmds[idx]
            self._save_custom_commands(cmds)
            try:
                self.fetch_firewalls(None)
            except Exception:
                pass

    def _run_custom_command(self, serial: str, label: str, cmd: str):
        try:
            self._execute_cli_command(serial, cmd, popup_title=f"Custom: {label}")
        except Exception as e:
            self._show_monospaced_alert("Custom Command Error", f"{label}: {e}")

    def load_stored_login(self):
        """Load credentials securely from Keychain if available, otherwise from JSON"""
        if KEYRING_AVAILABLE and os.path.exists(LOGIN_FILE):
            try:
                with open(LOGIN_FILE, "r") as f:
                    data = json.load(f)
                    self.panorama_url = data.get("panorama_url")
                    self.username = data.get("username")
                    
                if self.panorama_url and self.username:
                    try:
                        self.api_key = keyring.get_password(KEYRING_SERVICE, f"{self.panorama_url}_{self.username}")
                        if self.api_key:
                            self._notify("Panorama Sync", "Auto-login", f"Logged in as {self.username}")
                    except Exception as e:
                        self.log(f"Keyring retrieval failed: {e}")
            except Exception as e:
                self.log(f"Error loading stored login: {e}")
        elif os.path.exists(LOGIN_FILE):
            # Fallback to JSON storage (less secure)
            try:
                with open(LOGIN_FILE, "r") as f:
                    data = json.load(f)
                    self.panorama_url = data.get("panorama_url")
                    self.username = data.get("username")
                    self.api_key = data.get("api_key")
                if self.panorama_url and self.username and self.api_key:
                    self._notify("Panorama Sync", "Auto-login", f"Logged in as {self.username}")
            except Exception as e:
                self.log(f"Error loading stored login: {e}")

    def show_about(self, _):
        try:
            info = f"{VERSION}\n\nSecure credential storage: {'Enabled (Keychain)' if KEYRING_AVAILABLE else 'Disabled'}\nSSL Verification: {'Enabled' if self.verify_ssl else 'Disabled'}"
            rumps.alert(info)
        except Exception:
            print(VERSION)

    def store_login(self):
        """Store credentials securely in Keychain if available, otherwise in JSON"""
        if KEYRING_AVAILABLE:
            try:
                keyring.set_password(KEYRING_SERVICE, f"{self.panorama_url}_{self.username}", self.api_key)
                # Store only non-sensitive metadata in JSON
                data = {
                    "panorama_url": self.panorama_url,
                    "username": self.username,
                    "last_login": time.time()
                }
                with open(LOGIN_FILE, "w") as f:
                    json.dump(data, f)
                self._secure_file_permissions(LOGIN_FILE)
                self.log("Credentials stored securely in Keychain")
            except Exception as e:
                self.log(f"Keyring storage failed, falling back to JSON: {e}")
                self._store_login_json()
        else:
            self._store_login_json()

    def _store_login_json(self):
        """Fallback: store credentials in JSON (less secure)"""
        data = {
            "panorama_url": self.panorama_url,
            "username": self.username,
            "api_key": self.api_key
        }
        try:
            with open(LOGIN_FILE, "w") as f:
                json.dump(data, f)
            self._secure_file_permissions(LOGIN_FILE)
        except Exception as e:
            self.log(f"Error storing login: {e}")

    def _clear_loaded_state(self):
        """Clear all loaded/cached data and reset UI menus."""
        try:
            try:
                for t in list(getattr(self, "_pending_timers", set())):
                    try:
                        t.stop()
                    except Exception:
                        pass
                if hasattr(self, "_pending_timers"):
                    self._pending_timers.clear()
            except Exception:
                pass

            try:
                self._override_cache.clear()
            except Exception:
                pass
            try:
                self._fw_items.clear()
                self._fw_base.clear()
            except Exception:
                pass

            try:
                self.firewalls_menu.clear()
                self.firewalls_menu.title = "Firewalls"
                self.firewalls_menu.add(rumps.MenuItem("No firewalls loaded"))
            except Exception:
                pass

            try:
                self.device_groups_menu.clear()
                self.device_groups_menu.title = "Device Groups"
                self.device_groups_menu.add(rumps.MenuItem("No device groups loaded"))
            except Exception:
                pass

            try:
                self.templates_menu.clear()
                self.templates_menu.title = "Templates"
                self.templates_menu.add(rumps.MenuItem("No templates loaded"))
            except Exception:
                pass

            try:
                self.overrides_menu.clear()
                self.overrides_menu.title = "Local Overrides"
                self.overrides_menu.add(rumps.MenuItem("None detected"))
            except Exception:
                pass
        except Exception as e:
            try:
                self.log(f"[logout] clear state failed: {e}")
            except Exception:
                pass

    def logout(self, _):
        """Logout and clear credentials from Keychain"""
        if KEYRING_AVAILABLE and self.panorama_url and self.username:
            try:
                keyring.delete_password(KEYRING_SERVICE, f"{self.panorama_url}_{self.username}")
            except Exception as e:
                self.log(f"Keyring deletion failed: {e}")
        
        try:
            if os.path.exists(LOGIN_FILE):
                os.remove(LOGIN_FILE)
        except Exception:
            pass
        
        self.panorama_url = None
        self.username = None
        self.password = None
        self.api_key = None

        try:
            if threading.current_thread().name == 'MainThread':
                self._clear_loaded_state()
            else:
                self._on_main(self._clear_loaded_state)
        except Exception:
            self._clear_loaded_state()

        rumps.alert("Logged out. All cached items and credentials cleared.")

    def login_to_panorama(self, _):
        from AppKit import NSApp, NSAlert, NSTextField, NSSecureTextField
        NSApp.activateIgnoringOtherApps_(True)

        # Step 1: Panorama host
        alert1 = NSAlert.alloc().init()
        alert1.setMessageText_("Panorama Login")
        alert1.setInformativeText_("Enter Panorama Hostname/IP:")
        alert1.addButtonWithTitle_("Continue")
        alert1.addButtonWithTitle_("Cancel")
        host_field = NSTextField.alloc().initWithFrame_(((0, 0), (300, 24)))
        host_field.setStringValue_("panorama.company.com")
        alert1.setAccessoryView_(host_field)
        host_field.becomeFirstResponder()
        NSApp.activateIgnoringOtherApps_(True)
        resp = alert1.runModal()
        if resp != 1000:
            return
        pan_url = host_field.stringValue().strip()
        if not pan_url:
            rumps.alert("Login cancelled or incomplete.")
            return

        # Step 2: Username
        alert2 = NSAlert.alloc().init()
        alert2.setMessageText_("Panorama Login")
        alert2.setInformativeText_("Enter Username:")
        alert2.addButtonWithTitle_("Continue")
        alert2.addButtonWithTitle_("Cancel")
        user_field = NSTextField.alloc().initWithFrame_(((0, 0), (300, 24)))
        alert2.setAccessoryView_(user_field)
        user_field.becomeFirstResponder()
        NSApp.activateIgnoringOtherApps_(True)
        resp = alert2.runModal()
        if resp != 1000:
            return
        username = user_field.stringValue().strip()
        if not username:
            rumps.alert("Login cancelled or incomplete.")
            return

        # Step 3: Password (secure)
        alert3 = NSAlert.alloc().init()
        alert3.setMessageText_("Panorama Login")
        alert3.setInformativeText_("Enter Password:")
        alert3.addButtonWithTitle_("Continue")
        alert3.addButtonWithTitle_("Cancel")
        pass_field = NSSecureTextField.alloc().initWithFrame_(((0, 0), (300, 24)))
        alert3.setAccessoryView_(pass_field)
        pass_field.becomeFirstResponder()
        NSApp.activateIgnoringOtherApps_(True)
        resp = alert3.runModal()
        if resp != 1000:
            return
        password = pass_field.stringValue().strip()
        if not password:
            rumps.alert("Login cancelled or incomplete.")
            return

        # Step 4: Try to get API key
        try:
            api_url = f"https://{pan_url}/api/?type=keygen&user={username}&password={password}"
            r = self._make_request(api_url, timeout=10)
            root = ET.fromstring(r.text)
            key = root.findtext(".//key")
            if key:
                self.panorama_url = pan_url
                self.username = username
                self.password = password
                self.api_key = key
                self.store_login()
                rumps.alert("Login successful! API key acquired and stored securely.")
                # Auto-fetch key objects after successful login
                self.fetch_firewalls(None)
                self.fetch_templates(None)
                self.fetch_device_groups(None)
            else:
                msg = root.findtext(".//msg") or "Unknown error"
                rumps.alert(f"Login failed: {msg}")
        except Exception as e:
            rumps.alert(f"Error during login: {e}")

    def _show_vpn_info(self, serial: str, which: str):
        """Run predefined show commands with the tabbed Raw/Human popup."""
        mapping = {
            'ike': "show vpn ike-sa",
            'ipsec': "show vpn ipsec-sa",
            'prisma_ike': "show vpn ike-sa",
            'prisma_ipsec': "show vpn ipsec-sa",
        }
        cli = mapping.get(which)
        if not cli:
            return self._show_monospaced_alert("Unsupported", f"Unknown VPN show: {which}")
        try:
            self._execute_cli_command(serial, cli, popup_title=f"{which.replace('_', ' ').title()}")
        except Exception as e:
            self._show_monospaced_alert("VPN Show Error", f"{which}: {e}")

    def _pretty_xml(self, text_or_bytes):
        """Pretty-print XML without extra blank lines."""
        try:
            raw = text_or_bytes if isinstance(text_or_bytes, (bytes, bytearray)) else str(text_or_bytes).encode("utf-8")
            root = ET.fromstring(raw)
            for el in root.iter():
                if el.text is not None and el.text.strip() == "":
                    el.text = None
                if el.tail is not None and el.tail.strip() == "":
                    el.tail = None
            xml_bytes = ET.tostring(root, encoding="utf-8")
            from xml.dom import minidom
            parsed = minidom.parseString(xml_bytes)
            return parsed.toprettyxml(indent="  ", newl="\n")
        except Exception:
            return text_or_bytes.decode("utf-8", "ignore") if isinstance(text_or_bytes, (bytes, bytearray)) else str(text_or_bytes)

    def run_op_cmd(self, serial: str, xml_cmd: str) -> str:
        """Execute a Panorama operational command with raw XML."""
        if not self.api_key:
            rumps.alert("Please login first.")
            return "Error: not logged in"
        try:
            from urllib.parse import quote
            url = f"https://{self.panorama_url}/api/?key={self.api_key}&type=op&cmd={quote(xml_cmd)}&target={serial}"
            self.log(f"[op] GET (sanitized URL)")
            r = self._make_request(url, timeout=30)
            return r.text
        except Exception as e:
            return f"Error: {e}"

    def _humanize_generic_xml(self, xml_text: str, max_cols: int = 10, max_rows: int = 500) -> str:
        """Generic fallback: render sibling <entry> lists as a table."""
        try:
            root = ET.fromstring(xml_text)
            entries = root.findall('.//entry')
            if entries:
                cols = []
                for e in entries:
                    for c in e:
                        if c.tag not in cols:
                            cols.append(c.tag)
                    if len(cols) >= max_cols:
                        break
                rows = []
                for e in entries[:max_rows]:
                    rows.append([ (e.findtext(col) or '').strip() for col in cols ])
                widths = [ max(len(col), max((len(r[i]) for r in rows), default=0)) for i,col in enumerate(cols) ]
                out = [" ".join(col.ljust(widths[i]) for i, col in enumerate(cols))]
                out.append("-" * (sum(widths)+len(widths)-1))
                for r in rows:
                    out.append(" ".join(r[i].ljust(widths[i]) for i in range(len(cols))))
                return "\n".join(out)
            lines = []
            def walk(node, path):
                text = (node.text or '').strip()
                if text:
                    lines.append(f"{'/'.join(path+[node.tag])}: {text}")
                for child in list(node):
                    walk(child, path+[node.tag])
            walk(root, [])
            return "\n".join(lines) if lines else self._pretty_xml(xml_text)
        except Exception:
            return self._pretty_xml(xml_text)

    def _humanize_ike_summary(self, xml_text: str, only_gateway: str = None) -> str:
        """Render IKE SA summary into a table."""
        try:
            def gv(elem, names, default=""):
                for n in names:
                    v = elem.findtext(n)
                    if v is not None and str(v).strip() != "":
                        return str(v).strip()
                return default

            root = ET.fromstring(xml_text)
            entries = root.findall(".//entry")
            rows = []

            for e in entries:
                gw = gv(e, ["gateway", "name"]) or (e.get("name") or "").strip()
                if only_gateway and gw != only_gateway:
                    continue

                state = gv(e, ["state", "sa", "status", "phase1-state"])
                version = gv(e, ["version", "mode"])
                local_ip = gv(e, ["local-ip", "local", "local_ip"])
                peer_ip = gv(e, ["peer-ip", "peer", "peer_ip", "remote"])
                established = gv(e, ["established", "created", "start-time", "start_time"])

                role = gv(e, ["role"])
                algo = gv(e, ["algo", "algorithm"])

                if not any([state, local_ip, peer_ip, established]) and (role or algo):
                    state = role
                    established = established or gv(e, ["expires"])
                    version = version or ""
                    if not peer_ip and algo:
                        peer_ip = algo

                rows.append((gw, state, version, local_ip, peer_ip, established))

            if not rows:
                return "No IKE SAs found."

            headers = ("Gateway", "State", "Version", "Local IP", "Peer IP", "Established/Created")
            widths = [max(len(h), max((len(r[i]) for r in rows), default=0)) for i, h in enumerate(headers)]

            out = [" ".join(h.ljust(widths[i]) for i, h in enumerate(headers))]
            out.append("-" * (sum(widths) + len(widths) - 1))
            for r in rows:
                out.append(" ".join((r[i] or "").ljust(widths[i]) for i in range(len(headers))))
            return "\n".join(out)
        except Exception:
            return self._pretty_xml(xml_text)

    def _humanize_ipsec_summary(self, xml_text: str, only_tunnel: str = None) -> str:
        """Render IPsec SA summary into a table."""
        try:
            def gv(elem, names, default=""):
                for n in names:
                    v = elem.findtext(n)
                    if v is not None and str(v).strip() != "":
                        return str(v).strip()
                return default

            root = ET.fromstring(xml_text)
            entries = root.findall(".//entry")
            rows = []

            for e in entries:
                tun = gv(e, ["tunnel", "name"]) or (e.get("name") or "").strip()
                if only_tunnel and tun != only_tunnel:
                    continue
                state = gv(e, ["state", "status"])
                spi_in = gv(e, ["spi-in", "spi_in", "inbound-spi", "spiIn"])
                spi_out = gv(e, ["spi-out", "spi_out", "outbound-spi", "spiOut"])
                enc = gv(e, ["encryption", "enc", "cipher"])
                auth = gv(e, ["authentication", "auth", "integrity"])
                bytes_in = gv(e, ["bytes-in", "bytes_in", "inbound-bytes", "inBytes"])
                bytes_out = gv(e, ["bytes-out", "bytes_out", "outbound-bytes", "outBytes"])

                rows.append((tun, state, enc, auth, spi_in, spi_out, bytes_in, bytes_out))

            if not rows:
                return "No IPsec SAs found."

            headers = ("Tunnel", "State", "Enc", "Auth", "SPI In", "SPI Out", "Bytes In", "Bytes Out")
            widths = [max(len(h), max((len(r[i]) for r in rows), default=0)) for i, h in enumerate(headers)]

            out = [" ".join(h.ljust(widths[i]) for i, h in enumerate(headers))]
            out.append("-" * (sum(widths) + len(widths) - 1))
            for r in rows:
                out.append(" ".join((r[i] or "").ljust(widths[i]) for i in range(len(headers))))
            return "\n".join(out)
        except Exception:
            return self._pretty_xml(xml_text)

    def show_ike_summary(self, serial):
        xml = self.run_op_cmd(serial, "<show><vpn><ike-sa><summary></summary></ike-sa></vpn></show>")
        raw = self._pretty_xml(xml)
        human = self._humanize_ike_summary(xml) or self._humanize_generic_xml(xml)
        self._show_tabbed_alert("IKE Gateways (Summary)", [("Raw", raw), ("Human Readable", human)], width=1000, height=500)

    def show_ipsec_summary(self, serial):
        xml = self.run_op_cmd(serial, "<show><vpn><ipsec-sa><summary></summary></ipsec-sa></vpn></show>")
        raw = self._pretty_xml(xml)
        human = self._humanize_ipsec_summary(xml) or self._humanize_generic_xml(xml)
        self._show_tabbed_alert("IPsec Tunnels (Summary)", [("Raw", raw), ("Human Readable", human)], width=1000, height=500)

    def show_prisma_ike_gw(self, serial):
        xml = self.run_op_cmd(serial, "<show><vpn><ike-sa><gateway>prisma-ike-gw</gateway></ike-sa></vpn></show>")
        raw = self._pretty_xml(xml)
        human = self._humanize_ike_summary(xml, only_gateway="prisma-ike-gw") or self._humanize_generic_xml(xml)
        self._show_tabbed_alert("Prisma IKE Gateway", [("Raw", raw), ("Human Readable", human)], width=1000, height=500)

    def show_prisma_ipsec_tunnel(self, serial):
        xml = self.run_op_cmd(serial, "<show><vpn><ipsec-sa><tunnel>prisma-tunnel</tunnel></ipsec-sa></vpn></show>")
        raw = self._pretty_xml(xml)
        human = self._humanize_ipsec_summary(xml, only_tunnel="prisma-tunnel") or self._humanize_generic_xml(xml)
        self._show_tabbed_alert("Prisma IPsec Tunnel", [("Raw", raw), ("Human Readable", human)], width=1000, height=500)

    def fetch_firewalls(self, _):
        if not self.api_key or not self.panorama_url:
            rumps.alert("Please login first.")
            return
        try:
            api_url = f"https://{self.panorama_url}/api/?type=op&cmd=<show><devices><all></all></devices></show>&key={self.api_key}"
            r = self._make_request(api_url, timeout=10)
            xml_filename = os.path.join(WORK_DIR, "firewall_sync_log.xml")
            with open(xml_filename, "w") as f:
                f.write(r.text)
            self._secure_file_permissions(xml_filename)
            self.log(f"[Firewalls] Fetched successfully")
            root = ET.fromstring(r.text)
            entries = root.findall('.//entry')
            if not entries:
                self.firewalls_menu.clear()
                self.firewalls_menu.add(rumps.MenuItem("No firewalls found"))
                return
            valid_entries = [
                entry for entry in entries
                if entry.findtext('hostname') not in (None, 'N/A') and
                   entry.findtext('ip-address') not in (None, 'N/A') and
                   entry.findtext('serial') not in (None, 'N/A')
            ]
            valid_entries.sort(key=lambda e: e.findtext('hostname'))
            self.firewalls_menu.clear()
            self.firewalls_menu.title = "Firewalls"
            self._fw_items.clear(); self._fw_base.clear()
            for entry in valid_entries:
                hostname = entry.findtext('hostname')
                mgmt_ip = entry.findtext('ip-address')
                serial = entry.findtext('serial')
                norm_serial = self._norm_serial(serial)

                connected_field_candidates = [
                    entry.findtext('connected'),
                    entry.findtext('connection-status'),
                    entry.findtext('connected-to-panorama'),
                    entry.findtext('ha/peer/connected'),
                ]
                connected_raw = next((c for c in connected_field_candidates if c is not None), "")
                val = str(connected_raw).strip().lower()
                is_connected = val in ("yes", "true", "connected", "up", "1")
                icon = "🟢" if is_connected else "🔴"

                self._fw_base[norm_serial] = (icon, hostname)

                item = rumps.MenuItem(f"{icon} {hostname}")
                item.add(rumps.MenuItem(
                    "Open in Browser",
                    callback=lambda _, ip=mgmt_ip: webbrowser.open(f"https://{ip}")
                ))
                item.add(rumps.MenuItem(
                    "Show System Info",
                    callback=lambda _, ip=mgmt_ip, s=serial: self.fetch_system_info(ip, s)
                ))
                item.add(None)
                item.add(rumps.MenuItem(
                    "Check Local Overrides",
                    callback=lambda _, s=serial: self._show_override_details(s)
                ))
                item.add(rumps.MenuItem(
                    "Show Local Overrides",
                    callback=lambda _, s=serial: self._show_override_details(s)
                ))
                item.add(None)
                custom_menu = rumps.MenuItem("Custom Commands")
                custom_menu.add(rumps.MenuItem(
                    "Send Custom Command…",
                    callback=lambda _, ip=mgmt_ip, s=serial: self.send_cli_command(ip, s)
                ))
                custom_menu.add(None)
                for cc in self._load_custom_commands():
                    lbl = cc.get('label') or cc.get('cmd')
                    cmd = cc.get('cmd')
                    custom_menu.add(rumps.MenuItem(lbl,
                                                   callback=lambda _, s=serial, l=lbl, c=cmd: self._run_custom_command(s, l, c)))
                custom_menu.add(None)
                custom_menu.add(rumps.MenuItem("Add Custom Command", callback=self._add_custom_command_ui))
                custom_menu.add(rumps.MenuItem("Delete Custom Command", callback=self._delete_custom_command_ui))
                item.add(custom_menu)
                item.add(None)
                skypath_menu = rumps.MenuItem("Skypath commands")
                skypath_menu.add(rumps.MenuItem(
                    "Show IKE GW",
                    callback=lambda _, s=serial: self.show_ike_summary(s)
                ))
                skypath_menu.add(rumps.MenuItem(
                    "Show IPSec tunnel",
                    callback=lambda _, s=serial: self.show_ipsec_summary(s)
                ))
                skypath_menu.add(rumps.MenuItem(
                    "Show Prisma IKE GW",
                    callback=lambda _, s=serial: self.show_prisma_ike_gw(s)
                ))
                skypath_menu.add(rumps.MenuItem(
                    "Show Prisma IPSec tunnel",
                    callback=lambda _, s=serial: self.show_prisma_ipsec_tunnel(s)
                ))
                skypath_menu.add(None)
                skypath_menu.add(rumps.MenuItem(
                    "Clear Prisma IKE GW",
                    callback=lambda _, s=serial: self._vpn_stub("Clear Prisma IKE GW", s)
                ))
                skypath_menu.add(rumps.MenuItem(
                    "Clear Prisma IPSec tunnel",
                    callback=lambda _, s=serial: self._vpn_stub("Clear Prisma IPSec tunnel", s)
                ))
                skypath_menu.add(rumps.MenuItem(
                    "Test Prisma IKE GW",
                    callback=lambda _, s=serial: self._vpn_stub("Test Prisma IKE GW", s)
                ))
                skypath_menu.add(rumps.MenuItem(
                    "Test Prisma IPSec tunnel",
                    callback=lambda _, s=serial: self._vpn_stub("Test Prisma IPSec tunnel", s)
                ))
                item.add(skypath_menu)
                self._fw_items[norm_serial] = item
                self.firewalls_menu.add(item)
        except Exception as e:
            self.log(f"[Firewalls] Error: {e}")
            rumps.alert(f"Error fetching firewalls: {e}")

    def fetch_connected_firewalls(self, _):
        """Fetch only firewalls that are currently connected to Panorama."""
        if not self.api_key or not self.panorama_url:
            rumps.alert("Please login first.")
            return
        try:
            api_url = (
                f"https://{self.panorama_url}/api/?type=op&cmd="
                f"<show><devices><connected></connected></devices></show>&key={self.api_key}"
            )
            r = self._make_request(api_url, timeout=10)
            xml_filename = os.path.join(WORK_DIR, "firewall_connected_log.xml")
            with open(xml_filename, "w") as f:
                f.write(r.text)
            self._secure_file_permissions(xml_filename)
            self.log(f"[Firewalls Connected] Fetched successfully")

            root = ET.fromstring(r.text)
            entries = root.findall('.//entry')
            self.firewalls_menu.clear()
            self.firewalls_menu.title = "Firewalls (Connected)"

            if not entries:
                self.firewalls_menu.add(rumps.MenuItem("No connected firewalls"))
                return

            valid_entries = [
                entry for entry in entries
                if entry.findtext('hostname') not in (None, 'N/A') and
                   entry.findtext('ip-address') not in (None, 'N/A') and
                   entry.findtext('serial') not in (None, 'N/A')
            ]
            valid_entries.sort(key=lambda e: e.findtext('hostname'))
            self._fw_items.clear(); self._fw_base.clear()
            for entry in valid_entries:
                hostname = entry.findtext('hostname')
                mgmt_ip = entry.findtext('ip-address')
                serial = entry.findtext('serial')
                norm_serial = self._norm_serial(serial)

                icon = "🟢"
                self._fw_base[norm_serial] = (icon, hostname)

                item = rumps.MenuItem(f"{icon} {hostname}")
                item.add(rumps.MenuItem(
                    "Open in Browser",
                    callback=lambda _, ip=mgmt_ip: webbrowser.open(f"https://{ip}")
                ))
                item.add(rumps.MenuItem(
                    "Show System Info",
                    callback=lambda _, ip=mgmt_ip, s=serial: self.fetch_system_info(ip, s)
                ))
                item.add(None)
                item.add(rumps.MenuItem(
                    "Check Local Overrides",
                    callback=lambda _, s=serial: self._show_override_details(s)
                ))
                item.add(rumps.MenuItem(
                    "Show Local Overrides",
                    callback=lambda _, s=serial: self._show_override_details(s)
                ))
                item.add(None)
                custom_menu = rumps.MenuItem("Custom Commands")
                custom_menu.add(rumps.MenuItem(
                    "Send Custom Command…",
                    callback=lambda _, ip=mgmt_ip, s=serial: self.send_cli_command(ip, s)
                ))
                custom_menu.add(None)
                for cc in self._load_custom_commands():
                    lbl = cc.get('label') or cc.get('cmd')
                    cmd = cc.get('cmd')
                    custom_menu.add(rumps.MenuItem(lbl,
                                                   callback=lambda _, s=serial, l=lbl, c=cmd: self._run_custom_command(s, l, c)))
                custom_menu.add(None)
                custom_menu.add(rumps.MenuItem("Add Custom Command", callback=self._add_custom_command_ui))
                custom_menu.add(rumps.MenuItem("Delete Custom Command", callback=self._delete_custom_command_ui))
                item.add(custom_menu)
                item.add(None)
                skypath_menu = rumps.MenuItem("Skypath commands")
                skypath_menu.add(rumps.MenuItem(
                    "Show IKE GW",
                    callback=lambda _, s=serial: self.show_ike_summary(s)
                ))
                skypath_menu.add(rumps.MenuItem(
                    "Show IPSec tunnel",
                    callback=lambda _, s=serial: self.show_ipsec_summary(s)
                ))
                skypath_menu.add(rumps.MenuItem(
                    "Show Prisma IKE GW",
                    callback=lambda _, s=serial: self.show_prisma_ike_gw(s)
                ))
                skypath_menu.add(rumps.MenuItem(
                    "Show Prisma IPSec tunnel",
                    callback=lambda _, s=serial: self.show_prisma_ipsec_tunnel(s)
                ))
                skypath_menu.add(None)
                skypath_menu.add(rumps.MenuItem(
                    "Clear Prisma IKE GW",
                    callback=lambda _, s=serial: self._vpn_stub("Clear Prisma IKE GW", s)
                ))
                skypath_menu.add(rumps.MenuItem(
                    "Clear Prisma IPSec tunnel",
                    callback=lambda _, s=serial: self._vpn_stub("Clear Prisma IPSec tunnel", s)
                ))
                skypath_menu.add(rumps.MenuItem(
                    "Test Prisma IKE GW",
                    callback=lambda _, s=serial: self._vpn_stub("Test Prisma IKE GW", s)
                ))
                skypath_menu.add(rumps.MenuItem(
                    "Test Prisma IPSec tunnel",
                    callback=lambda _, s=serial: self._vpn_stub("Test Prisma IPSec tunnel", s)
                ))
                item.add(skypath_menu)
                self._fw_items[norm_serial] = item
                self.firewalls_menu.add(item)
        except Exception as e:
            self.log(f"[Firewalls Connected] Error: {e}")
            rumps.alert(f"Error fetching connected firewalls: {e}")

    def _vpn_stub(self, title: str, serial: str):
        """Placeholder for VPN/Prisma helpers."""
        try:
            self._show_monospaced_alert(title, f"Action '{title}' for serial {serial} is not implemented in this build.")
        except Exception as e:
            try:
                rumps.alert(f"{title}: {e}")
            except Exception:
                print(f"{title}: {e}")

    def fetch_device_groups(self, _):
        if not self.api_key or not self.panorama_url:
            rumps.alert("Please login first.")
            return
        try:
            api_url = f"https://{self.panorama_url}/api/?type=op&cmd=<show><devicegroups></devicegroups></show>&key={self.api_key}"
            r = self._make_request(api_url, timeout=10)
            self.log(f"[Device Groups] Fetched successfully")
            xml_filename = os.path.join(WORK_DIR, "device_group_sync_log.xml")
            with open(xml_filename, "w") as f:
                f.write(r.text)
            self._secure_file_permissions(xml_filename)
            root = ET.fromstring(r.text)
            entries = root.findall('.//entry')
            if not entries:
                self.device_groups_menu.clear()
                self.device_groups_menu.add(rumps.MenuItem("No device groups found"))
                return
            self.device_groups_menu.clear()
            self.device_groups_menu.title = "Device Groups"
            items = []

            for entry in entries:
                dg_name = entry.attrib.get("name")
                multi_vsys = entry.findtext("multi-vsys") or "yes"
                devices = entry.find("devices")
                if devices is not None:
                    for dev in devices.findall("entry"):
                        hostname = dev.findtext("hostname")
                        if not hostname:
                            continue
                        if hostname == "vsys1" and multi_vsys.lower() == "no":
                            continue
                        sync_status = dev.findtext("shared-policy-status") or "unknown"
                        if sync_status.lower() == "in sync":
                            icon = "🟢"
                        elif sync_status.lower() == "out of sync":
                            icon = "🔴"
                        else:
                            icon = "🟡"
                        label = f"{icon} {hostname}: {sync_status}"
                        item = rumps.MenuItem(label)
                        sync_item = rumps.MenuItem(
                            "Push to Device",
                            callback=lambda _, dg=dg_name: self.sync_device_group_to_panorama(dg)
                        )
                        item.add(sync_item)
                        items.append(item)

            for item in sorted(items,
                               key=lambda x: x.title.lower().replace("🟢 ", "").replace("🔴 ", "").replace("🟡 ", "")):
                self.device_groups_menu.add(item)
        except Exception as e:
            self.log(f"[Device Groups] Error: {e}")
            rumps.alert(f"Error fetching device groups: {e}")

    def fetch_templates(self, _):
        if not self.api_key or not self.panorama_url:
            rumps.alert("Please login first.")
            return
        try:
            api_url = f"https://{self.panorama_url}/api/?type=op&cmd=<show><templates></templates></show>&key={self.api_key}"
            r = self._make_request(api_url, timeout=10)
            self.log(f"[Templates] Fetched successfully")
            xml_filename = os.path.join(WORK_DIR, "template_sync_log.xml")
            with open(xml_filename, "w") as f:
                f.write(r.text)
            self._secure_file_permissions(xml_filename)
            root = ET.fromstring(r.text)
            entries = root.findall(".//entry")
            if not entries:
                self.templates_menu.clear()
                self.templates_menu.add(rumps.MenuItem("No templates found"))
                return
            self.templates_menu.clear()
            self.templates_menu.title = "Templates"
            items = []
            for entry in entries:
                entry_name = entry.attrib.get("name") or ""
                if entry_name.endswith("_stack"):
                    hostname = entry_name.replace("_stack", "")
                    if not hostname or hostname == "vsys1":
                        continue
                    sync_status = entry.findtext("template-status") or entry.findtext(".//template-status") or "unknown"
                    if sync_status.lower() == "in sync":
                        icon = "🟢"
                    elif sync_status.lower() == "out of sync":
                        icon = "🔴"
                    else:
                        icon = "🟡"
                    label = f"{icon} {hostname}: {sync_status}"
                    item = rumps.MenuItem(label)
                    sync_item = rumps.MenuItem("Push to Device",
                                               callback=lambda _, h=hostname: self.push_template(h))
                    item.add(sync_item)
                    items.append(item)
                    continue
                devices = entry.find("devices")
                if devices is not None:
                    for dev in devices.findall("entry"):
                        hostname = dev.findtext("hostname")
                        if not hostname or hostname == "vsys1":
                            continue
                        sync_status = dev.findtext("device-template-sync") or "unknown"
                        if sync_status.lower() == "in sync":
                            icon = "🟢"
                        elif sync_status.lower() == "out of sync":
                            icon = "🔴"
                        else:
                            icon = "🟡"
                        label = f"{icon} {hostname}: {sync_status}"
                        item = rumps.MenuItem(label)
                        sync_item = rumps.MenuItem("Push to Device",
                                                   callback=lambda _, h=hostname: self.push_template(h))
                        item.add(sync_item)
                        items.append(item)
            items.sort(key=lambda x: x.title.lower().replace("🟢 ", "").replace("🔴 ", "").replace("🟡 ", ""))
            for item in items:
                self.templates_menu.add(item)
        except Exception as e:
            self.log(f"[Templates] Error: {e}")
            rumps.alert(f"Error fetching templates: {e}")

    def fetch_all(self, _):
        self.fetch_firewalls(_)

    def fetch_system_info(self, ip, serial):
        """System info popup with two tabs: Raw and Human Readable."""
        if not self.api_key:
            rumps.alert("Please login first.")
            return
        try:
            cmd = "<show><system><info></info></system></show>"
            url = f"https://{self.panorama_url}/api/?type=op&target={serial}&cmd={cmd}&key={self.api_key}"
            r = self._make_request(url, timeout=15)

            raw_text = r.text or ""
            try:
                import xml.dom.minidom as minidom
                dom = minidom.parseString(raw_text)
                pretty_xml = dom.toprettyxml(indent="  ")
                pretty_lines = [line for line in pretty_xml.splitlines() if line.strip()]
                raw_pretty = "\n".join(pretty_lines)
            except Exception:
                raw_pretty = raw_text

            try:
                root = ET.fromstring(r.text)
                sysinfo = root.find(".//result/system")
            except Exception:
                sysinfo = None

            if sysinfo is not None:
                vals = lambda tag: (sysinfo.findtext(tag) or "N/A").strip()
                output_lines = [
                    f"{'Hostname:':15} {vals('hostname')}",
                    f"{'IP Address:':15} {vals('ip-address')}",
                    f"{'Default Gateway:':15} {vals('default-gateway')}",
                    f"{'Netmask:':15} {vals('netmask')}",
                    f"{'DHCP Enabled:':15} {vals('is-dhcp')}",
                    f"{'IPv6 Address:':15} {vals('ipv6-address')}",
                    f"{'IPv6 Link Local:':15} {vals('ipv6-link-local-address')}",
                    f"{'MAC Address:':15} {vals('mac-address')}",
                    f"{'Model:':15} {vals('model')}",
                    f"{'SW Version:':15} {vals('sw-version')}",
                    f"{'Serial:':15} {vals('serial')}",
                    f"{'Uptime:':15} {vals('uptime')}",
                ]
                human_text = "\n".join(output_lines)
            else:
                human_text = "Unable to parse system info. See Raw tab."

            tabs = [("Raw", raw_pretty), ("Human Readable", human_text)]
            self._show_tabbed_alert("System Info", tabs, width=760, height=420)

            self.log(f"[System Info for {ip}] Retrieved successfully")

        except Exception as e:
            self.log(f"[System Info Error for {ip}] {e}")
            self._show_monospaced_alert("System Info Error", f"Error: {e}")

    def _cli_show_to_xml(self, cmd: str) -> Optional[str]:
        """Convert plain CLI 'show ...' into nested op-XML with a self-closing leaf."""
        try:
            parts = [p for p in (cmd or '').strip().split() if p]
            if not parts or parts[0].lower() != 'show':
                return None
            tokens = parts[1:]
            if not tokens:
                return '<show/>'
            xml = ['<show>']
            for t in tokens[:-1]:
                xml.append(f'<{t}>')
            xml.append(f'<{tokens[-1]}/>')
            for t in reversed(tokens[:-1]):
                xml.append(f'</{t}>')
            xml.append('</show>')
            return ''.join(xml)
        except Exception:
            return None

    def _execute_cli_command(self, serial: str, cli_cmd: str, popup_title: str = "CLI Output"):
        if not self.api_key:
            rumps.alert("Please login first.")
            return

        cmd = (cli_cmd or "").strip()
        cmd_lower = cmd.lower()

        xml_cmd = None
        humanizer = None

        if cmd.startswith("<") and cmd.endswith(">"):
            xml_text = self.run_op_cmd(serial, cmd)
            raw_pretty = self._pretty_xml(xml_text)
            human_text = self._humanize_generic_xml(xml_text)
            self._show_tabbed_alert(popup_title, [("Raw", raw_pretty), ("Human Readable", human_text)], width=760, height=420)
            return

        if cmd_lower == "show interface all":
            xml_cmd = "<show><interface>all</interface></show>"
            def _human_if(xml_text: str):
                try:
                    root = ET.fromstring(xml_text)
                    result_elem = root.find('.//result')
                    hw_section = result_elem.find('hw') if result_elem is not None else None
                    if hw_section is None:
                        return "(No human-readable formatter for this command)\nSee Raw tab."
                    rows = []
                    for entry in hw_section.findall('entry'):
                        parts = []
                        for child in entry:
                            key = child.tag.strip(); val = (child.text or '').strip()
                            parts.append(f"{key}: {val}")
                        rows.append(" | ".join(parts))
                    return "\n".join(rows) if rows else "No entries."
                except Exception:
                    return "(No human-readable formatter for this command)\nSee Raw tab."
            humanizer = _human_if

        elif cmd_lower == "show arp all":
            xml_cmd = "<show><arp><entry name='all'/></arp></show>"
            def _human_arp(xml_text: str):
                try:
                    root = ET.fromstring(xml_text)
                    result_elem = root.find('.//result')
                    entries = []
                    if result_elem is not None:
                        entries_section = result_elem.find('entries')
                        entries = entries_section.findall('entry') if entries_section is not None else []
                    rows = []
                    for e in entries:
                        parts = []
                        for child in e:
                            key = child.tag.strip(); val = (child.text or '').strip()
                            parts.append(f"{key}: {val}")
                        rows.append(" | ".join(parts))
                    return "\n".join(rows) if rows else "No ARP entries."
                except Exception:
                    return "(No human-readable formatter for this command)\nSee Raw tab."
            humanizer = _human_arp

        elif cmd_lower.startswith("show "):
            xml_cmd = self._cli_show_to_xml(cmd)
            if xml_cmd:
                xml_text = self.run_op_cmd(serial, xml_cmd)
                raw_pretty = self._pretty_xml(xml_text)
                human_text = self._humanize_generic_xml(xml_text)
                self._show_tabbed_alert(popup_title, [("Raw", raw_pretty), ("Human Readable", human_text)], width=760, height=420)
                return

        elif cmd_lower in ("show vpn ike-sa", "show vpn ike sa", "show ike gw", "show ike gateway"):
            xml_cmd = "<show><vpn><ike-sa></ike-sa></vpn></show>"
            humanizer = self._humanize_ike_summary

        elif cmd_lower in ("show vpn ipsec-sa", "show vpn ipsec sa", "show ipsec tunnel", "show ipsec tunnels"):
            xml_cmd = "<show><vpn><ipsec-sa></ipsec-sa></vpn></show>"
            humanizer = self._humanize_ipsec_summary

        elif cmd_lower in ("show routing protocol bgp summary", "show bgp summary"):
            xml_cmd = "<show><routing><protocol><bgp><summary></summary></bgp></protocol></routing></show>"
            humanizer = None

        else:
            plaintext = cmd
            xml_wrapped = f"<request><cli><raw>{plaintext}</raw></cli></request>"
            xml_text = self.run_op_cmd(serial, xml_wrapped)
            raw_pretty = self._pretty_xml(xml_text)
            human_text = self._humanize_generic_xml(xml_text)
            self._show_tabbed_alert(popup_title + " (raw)", [("Raw", raw_pretty), ("Human Readable", human_text)], width=760, height=420)
            return

        xml_text = self.run_op_cmd(serial, xml_cmd)
        raw_pretty = self._pretty_xml(xml_text)

        try:
            human_text = (humanizer(xml_text) if humanizer else None)
        except Exception:
            human_text = None
        if not human_text:
            human_text = self._humanize_generic_xml(xml_text)

        self._show_tabbed_alert(popup_title, [("Raw", raw_pretty), ("Human Readable", human_text)], width=760, height=420)

    def send_cli_command(self, ip, serial):
        """Firewall CLI runner using an NSAlert."""
        if not self.api_key:
            rumps.alert("Please login first.")
            return

        preset_cmds = [
            "show interface all",
            "show arp all",
            "show routing protocol bgp summary",
            "show routing route"
        ]

        while True:
            try:
                from AppKit import (
                    NSApp, NSAlert, NSView, NSMakeRect, NSTextField,
                    NSPopUpButton, NSImage
                )

                alert = NSAlert.alloc().init()
                alert.setMessageText_("Firewall CLI")
                alert.setInformativeText_("Choose a preset or enter a CLI/op-XML command. If both are provided, the text field is used.")

                if os.path.exists(ICON_PATH):
                    icon_image = NSImage.alloc().initWithContentsOfFile_(ICON_PATH)
                    if icon_image:
                        alert.setIcon_(icon_image)

                view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 500, 74))

                popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 48, 500, 24), False)
                popup.addItemWithTitle_("— Presets —")
                for cmd in preset_cmds:
                    popup.addItemWithTitle_(cmd)

                field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 500, 28))
                try:
                    field.setPlaceholderString_("Enter CLI or op-XML (e.g., <show>...</show>)")
                except Exception:
                    pass

                view.addSubview_(popup)
                view.addSubview_(field)
                alert.setAccessoryView_(view)

                alert.addButtonWithTitle_("Run")
                alert.addButtonWithTitle_("Cancel")

                NSApp.activateIgnoringOtherApps_(True)
                resp = alert.runModal()
                if resp != 1000:
                    break

                user_text = (field.stringValue() or "").strip()
                if user_text:
                    cli_cmd = user_text
                else:
                    sel = popup.titleOfSelectedItem()
                    if sel and not sel.startswith("—"):
                        cli_cmd = sel
                    else:
                        break

                if cli_cmd.lstrip().startswith("<") and cli_cmd.rstrip().endswith(">"):
                    try:
                        xml_text = self.run_op_cmd(serial, cli_cmd)
                        raw_pretty = self._pretty_xml(xml_text)
                        human_text = self._humanize_generic_xml(xml_text)
                        self._show_tabbed_alert(f"CLI (op-XML): {cli_cmd[:60]}...", [("Raw", raw_pretty), ("Human Readable", human_text)], width=760, height=420)
                        self.log(f"[CLI Output for {ip}] (op-XML)")
                    except Exception as e:
                        self._show_monospaced_alert("CLI Error (op-XML)", f"{e}")
                    continue

                try:
                    self._execute_cli_command(serial, cli_cmd, popup_title=f"CLI: {cli_cmd}")
                    self.log(f"[CLI Output for {ip}] Command executed")
                except Exception as e:
                    self._show_monospaced_alert("CLI Error", f"{cli_cmd}: {e}")

                continue

            except Exception as e:
                prompt = (
                    "Choose a preset command number or enter a custom command.\n"
                    + "\n".join([f"{i+1}. {c}" for i, c in enumerate(preset_cmds)])
                    + "\n\nType the number of a preset, enter op-XML, or a CLI command."
                )
                res = rumps.Window(prompt, title="Firewall CLI (fallback)", dimensions=(520, 200)).run()
                if not res.clicked:
                    break
                val = (res.text or "").strip()
                if not val:
                    break
                if val.isdigit():
                    idx = int(val) - 1
                    if 0 <= idx < len(preset_cmds):
                        cli_cmd = preset_cmds[idx]
                    else:
                        cli_cmd = val
                else:
                    cli_cmd = val
                try:
                    if cli_cmd.lstrip().startswith("<") and cli_cmd.rstrip().endswith(">"):
                        xml_text = self.run_op_cmd(serial, cli_cmd)
                        raw_pretty = self._pretty_xml(xml_text)
                        human_text = self._humanize_generic_xml(xml_text)
                        self._show_tabbed_alert(f"CLI (op-XML): {cli_cmd[:60]}...", [("Raw", raw_pretty), ("Human Readable", human_text)], width=760, height=420)
                        self.log(f"[CLI Output for {ip}] (op-XML)")
                    else:
                        self._execute_cli_command(serial, cli_cmd, popup_title=f"CLI: {cli_cmd}")
                        self.log(f"[CLI Output for {ip}] Command executed")
                except Exception as ex:
                    self._show_monospaced_alert("CLI Error", f"{cli_cmd}: {ex}")
                continue

    def _url_x(self, xpath):
        from urllib.parse import quote
        return quote(xpath, safe="/[]@'=:-")

    def sync_device_group_to_panorama(self, device_group_name):
        """Push Device Group policies using the correct Panorama op API and XML structure."""
        if not self.api_key:
            rumps.alert("Please login first.")
            return

        response = rumps.alert(
            title="Confirm Device Group Push",
            message=f"Are you sure you want to push the device group '{device_group_name}'?",
            ok="Yes, Push",
            cancel="Cancel"
        )
        if response != 1:
            return

        try:
            from urllib.parse import quote
            xml_cmd = (
                f"<commit-all><shared-policy>"
                f"<device-group><entry name='{device_group_name}'/></device-group>"
                f"</shared-policy></commit-all>"
            )
            url = (
                f"https://{self.panorama_url}/api/?type=commit&action=all"
                f"&key={self.api_key}&cmd={quote(xml_cmd)}"
            )
            self.log(f"[Device Group Push] Initiating push")
            r = self._make_request(url, timeout=30)
            self.log(f"[Device Group Push] Response received")

            root = ET.fromstring(r.text)
            status = root.get("status", "unknown")
            job_id = root.findtext(".//job")
            msg = root.findtext(".//msg") or root.findtext(".//line") or ""

            if status == "success" and job_id:
                output = (
                    f"Device Group push started for '{device_group_name}'.\n"
                    f"Job ID: {job_id}\n\n"
                    f"Check Panorama Task Manager for progress."
                )
                try:
                    from AppKit import NSAlert, NSImage, NSApp
                    alert = NSAlert.alloc().init()
                    alert.setMessageText_(f"Device Group Push — {device_group_name}")
                    alert.setInformativeText_(output)
                    if os.path.exists(ICON_PATH):
                        icon_image = NSImage.alloc().initWithContentsOfFile_(ICON_PATH)
                        if icon_image:
                            alert.setIcon_(icon_image)
                    NSApp.activateIgnoringOtherApps_(True)
                    alert.runModal()
                except Exception:
                    rumps.alert(output)
                rumps.notification("Panorama Sync", "Device Group Push", f"Job {job_id} for '{device_group_name}' started")
                return

            err_out = f"Push failed for '{device_group_name}'. Status: {status}\n{msg or 'Unknown error'}"
            self.log(f"[Device Group Push] ERROR: {err_out}")
            rumps.alert(title="Push Failed", message=err_out)

        except Exception as e:
            self.log(f"[Device Group Push] Exception: {e}")
            rumps.alert(f"Error pushing device group '{device_group_name}': {e}")

    def push_template(self, template_name, sync=True, force=False):
        """Push a template to devices using Panorama Commit-All on the template-stack."""
        if not self.api_key:
            rumps.alert("Please login first.")
            return

        try:
            from urllib.parse import quote
            stack_name = f"{template_name}_stack"
            xml_cmd = (
                f"<commit-all><template-stack>"
                f"<name>{stack_name}</name>"
                f"</template-stack></commit-all>"
            )
            url = (
                f"https://{self.panorama_url}/api/?type=commit&action=all"
                f"&key={self.api_key}&cmd={quote(xml_cmd)}"
            )
            self.log(f"[Template Push] Initiating push")
            r = self._make_request(url, timeout=30)
            self.log(f"[Template Push] Response received")

            root = ET.fromstring(r.text)
            status = root.get("status", "unknown")
            job_id = root.findtext(".//job")
            msg = root.findtext(".//msg") or root.findtext(".//line") or ""

            if status == "success" and job_id:
                output = (
                    f"Template push started for '{template_name}' (stack '{stack_name}').\n"
                    f"Job ID: {job_id}\n\n"
                    f"Check Panorama Task Manager for progress."
                )
                try:
                    from AppKit import NSAlert, NSImage, NSApp
                    alert = NSAlert.alloc().init()
                    alert.setMessageText_(f"Template Push — {template_name}")
                    alert.setInformativeText_(output)
                    if os.path.exists(ICON_PATH):
                        icon_image = NSImage.alloc().initWithContentsOfFile_(ICON_PATH)
                        if icon_image:
                            alert.setIcon_(icon_image)
                    NSApp.activateIgnoringOtherApps_(True)
                    alert.runModal()
                except Exception:
                    rumps.alert(output)
                rumps.notification("Panorama Sync", "Template Push", f"Job {job_id} for '{template_name}' started")
                return

            err_out = f"Push failed for '{template_name}'. Status: {status}\n{msg or 'Unknown error'}"
            self.log(f"[Template Push] ERROR: {err_out}")
            rumps.alert(title="Push Failed", message=err_out)

        except Exception as e:
            self.log(f"[Template Push] Exception: {e}")
            rumps.alert(f"Error pushing template '{template_name}': {e}")


if __name__ == "__main__":
    import urllib3
    urllib3.disable_warnings()
    warnings.filterwarnings("ignore", category=InsecureRequestWarning)
    warnings.filterwarnings("ignore", category=NotOpenSSLWarning)
    PanoramaSyncMonitor().run()
