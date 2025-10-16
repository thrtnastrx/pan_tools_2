import warnings
import sys
import os

# Suppress early urllib3/ssl warnings on import (e.g., NotOpenSSLWarning)
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

ICON_FILENAME = "pan-logo-1.png"
VERSION = "v2.0"
BUNDLE_ID = "com.yourcompany.panoramatools"

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
else:
    # Running as script
    ICON_PATH = os.path.join(os.path.dirname(__file__), ICON_FILENAME)
    LOGIN_FILE = os.path.expanduser("~/.panorama_sync_login")
    LOG_FILE = os.path.join(os.getcwd(), "panorama_sync_log.txt")
    CUSTOM_CMDS_FILE = os.path.join(os.getcwd(), "custom_cli_commands.json")
    CLI_LOG_FILE = os.path.join(os.getcwd(), "cli_debug.log")
    WORK_DIR = os.getcwd()

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
        self.panorama_url = None
        self.username = None
        self.password = None
        self.api_key = None
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
            rumps.MenuItem("Quit", callback=rumps.quit_application),
        ]

        # Defer auto-fetch so the menu bar item appears immediately (avoid blocking UI)
        if self.panorama_url and self.username and self.api_key:
            try:
                # Auto-fetch connected FWs, templates, and device groups shortly after startup
                self._initial_fetch_timer = rumps.Timer(self._deferred_fetch_on_launch, 0.2)
                self._initial_fetch_timer.start()
            except Exception as e:
                logging.warning("Failed to start deferred fetch timer: %s", e)

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
            # xml.etree.ElementTree elements don't keep parent refs; fall back to tag-only path
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
                # Summarize tag and key attribs
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
        """Return True if there is real configuration under <network>.
        'Real' means any descendant element has non-whitespace text or any attribute (e.g., entry name="..."),
        not just empty containers like <ethernet/>, <units/>, <vlan/>, empty crypto/profile stubs, etc.
        """
        if net_elem is None:
            return False
        for e in net_elem.iter():
            if e is net_elem:
                continue
            # Attribute presence (e.g., <entry name="..."></entry>) is meaningful config
            if e.attrib:
                return True
            # Any non-whitespace text is meaningful config (e.g., <profile>test</profile>)
            if e.text and e.text.strip():
                return True
        return False

    def _fetch_config_network(self, serial, which):
        """Return XML text of the Network subtree for the device. which in {"running", "pushed-template"}.
        - running: uses type=config&action=get with the Network xpath on the target firewall
        - pushed-template: uses op show config pushed-template and extracts the same Network xpath
        """
        try:
            network_xp = "/config/devices/entry[@name='localhost.localdomain']/network"
            if which == "running":
                url = (
                    f"https://{self.panorama_url}/api/?key={self.api_key}"
                    f"&type=config&action=get&xpath={self._url_x(network_xp)}&target={serial}"
                )
                r = self._http.get(url, verify=False, timeout=12)
                return r.text
            elif which == "pushed-template":
                # Fetch the pushed-template config and extract the Network subtree
                cmd = "&cmd=<show><config><pushed-template></pushed-template></config></show>"
                url = f"https://{self.panorama_url}/api/?key={self.api_key}&type=op{cmd}&target={serial}"
                r = self._http.get(url, verify=False, timeout=12)
                try:
                    root = ET.fromstring(r.text)
                    net_elem = root.find(
                        ".//config/devices/entry[@name='localhost.localdomain']/network"
                    )
                    if net_elem is not None:
                        return ET.tostring(net_elem, encoding="unicode")
                    # If not found, return the original text for troubleshooting
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
        """Execute function on main thread. If already on main, run synchronously.
        Otherwise, schedule via a short rumps.Timer, and keep a strong reference
        until the callback runs to avoid GC cancelling the timer.
        """
        try:
            if threading.current_thread().name == 'MainThread':
                # Already on main thread; run immediately
                return fn(*args, **kwargs)
        except Exception:
            # Fallback to timer path if thread name is unavailable
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
                        # Remove strong reference
                        try:
                            self._pending_timers.discard(t)
                        except Exception:
                            pass
                except Exception:
                    pass

        # Create a short timer and keep a strong reference so it won't be GC'd
        timer = rumps.Timer(cb, 0.01)
        cb._timer_ref = timer
        try:
            self._pending_timers.add(timer)
        except Exception:
            pass
        timer.start()

    def _fetch_config_xml(self, serial, which):
        """Return XML text for firewall config via op cmd. which in {"running", "pushed-template"}."""
        try:
            if which not in {"running", "pushed-template"}:
                return None
            cmd = f"&cmd=<show><config><{which}></{which}></config></show>"
            url = f"https://{self.panorama_url}/api/?key={self.api_key}&type=op{cmd}&target={serial}"
            r = self._http.get(url, verify=False, timeout=8)
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
                with open(os.path.join(WORK_DIR, f"override_running_{serial}.xml"), "w") as f:
                    f.write(running_net or "")
            except Exception as e2:
                self.log(f"[override] write running network xml failed: {e2}")

            # Parse and decide meaningfulness
            def to_elem(xml_text):
                try:
                    return ET.fromstring(xml_text) if xml_text else None
                except Exception:
                    return None

            run_elem = to_elem(running_net)
            has_meaning = self._has_meaningful_network_config(run_elem)

            if has_meaning:
                # Provide a brief preview of meaningful nodes for context
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
                # Remove placeholder if present
                placeholders = [mi for mi in self.overrides_menu if getattr(mi, 'title', '') == 'None detected']
                for mi in placeholders:
                    self.overrides_menu.remove(mi)
                # Remove any existing item for this serial
                to_remove = []
                for mi in self.overrides_menu:
                    if f"({norm})" in getattr(mi, 'title', ''):
                        to_remove.append(mi)
                for mi in to_remove:
                    self.overrides_menu.remove(mi)
                # Add if flagged
                if has_override:
                    host = self._fw_base.get(norm, ("", norm))[1]
                    mi = rumps.MenuItem(f"⚠️ {host} ({norm})")
                    mi.add(rumps.MenuItem("Check Local Overrides", callback=lambda _, s=norm: self._show_override_details(s)))
                    self.overrides_menu.add(mi)
                # If empty after removals and not adding, restore placeholder
                if not list(self.overrides_menu):
                    self.overrides_menu.add(rumps.MenuItem("None detected"))
                # Nudge redraw if supported
                if hasattr(self.overrides_menu, 'update'):
                    self.overrides_menu.update()
            try:
                # Rebuild immediately so the submenu reflects newly added/removed entries
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
                self.log(f"[override] No menu item found for serial '{norm_serial}' (available: {list(self._fw_items.keys())})")
                return

            if base:
                icon, hostname = base
                override_icon = " ⚠️" if has_override else ""
                new_title = f"{icon}{override_icon} {hostname}"

                self.log(f"[override] Setting title for {norm_serial}: '{new_title}'")
                item.title = new_title
            else:
                # Fallback if base missing
                clean_title = item.title.replace(" ⚠️", "").strip()
                new_title = f"{clean_title} ⚠️" if has_override else clean_title
                item.title = new_title
                self.log(f"[override] Fallback title for {norm_serial}: '{new_title}'")

            # Update the override cache to ensure consistency
            current_cache = self._override_cache.get(norm_serial)
            if isinstance(current_cache, tuple):
                self._override_cache[norm_serial] = (has_override, current_cache[1])
            else:
                self._override_cache[norm_serial] = (has_override, "Icon updated")

            # Immediate override list update
            self._ensure_override_listed(norm_serial, has_override)

            # Force refresh of both menus
            self._rebuild_firewalls_menu_icons()
            self._rebuild_overrides_menu()

        except Exception as e:
            self.log(f"[override] Error updating icon for {serial}: {e}")

    def _notify(self, title, subtitle, message):
        """Post a macOS notification, but never crash if Info.plist/CFBundleIdentifier is missing."""
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

            # Clear cache to force fresh checks
            self._override_cache.clear()

            serials = list(self._fw_items.keys())
            if not serials:
                self.log("[override] No firewalls to check.")
                self._notify("Panorama Sync", "Override Check", "No firewalls loaded to check")
                return

            self._notify("Panorama Sync", "Override Check", f"Checking {len(serials)} firewalls…")

            results = []  # (serial, has_override)

            def worker(s):
                try:
                    has_override, summary = self._detect_local_override(s)
                    return (s, has_override)
                except Exception as e:
                    self.log(f"[override] worker error for {s}: {e}")
                    return (s, False)

            # Use existing executor but bound concurrency; increase workers if needed
            max_workers = getattr(self, '_max_override_workers', None) or 12
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(worker, s): s for s in serials}
                batch = []
                for fut in futures:
                    try:
                        s, has_override = fut.result(timeout=40)
                        results.append((s, has_override))
                        batch.append((s, has_override))
                        # Batch UI updates every 10 items to cut timer overhead
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

                # Flush remaining updates
                if batch:
                    upd = list(batch)
                    def do_updates_last():
                        for ss, hov in upd:
                            self._update_override_icon(ss, hov)
                        self._rebuild_overrides_menu()
                        self._rebuild_firewalls_menu_icons()
                    self._on_main(do_updates_last)

            override_count = sum(1 for _, hov in results if hov)
            self.log(f"[override] Bulk check complete (fast). Found {override_count} overrides")

            if override_count > 0:
                self._notify("Panorama Sync", "Override Check Complete", f"Found {override_count} firewalls with local overrides")
            else:
                self._notify("Panorama Sync", "Override Check Complete", "No local overrides detected")

        except Exception as e:
            self.log(f"[override] bulk check error (fast): {e}")
            rumps.alert(f"Override check failed: {e}")

    def _show_override_details(self, serial):
        has_override, summary = self._detect_local_override(serial)
        self._update_override_icon(serial, has_override)
        # Point to saved response files
        saved_run = os.path.join(WORK_DIR, f"override_running_{serial}.xml")
        # Decide what content to display in the popup body
        detail_text = None
        if has_override:
            try:
                # Prefer pretty-printed XML if possible; fall back to raw file contents
                with open(saved_run, "r") as f:
                    run_xml = f.read()
                try:
                    import xml.dom.minidom as minidom
                    dom = minidom.parseString(run_xml)
                    pretty = dom.toprettyxml(indent="  ")
                    # Remove excessive blank lines and trailing whitespace
                    lines = [line for line in pretty.splitlines() if line.strip()]
                    detail_text = "\n".join(lines)
                except Exception:
                    detail_text = run_xml
            except Exception as e:
                detail_text = f"(Failed to load running config: {e})\n\n" + (summary or "")
            # Append a short footer with file paths for clarity
            detail_text += f"\n\n— Showing running /network —\nSaved file:\n- running: {saved_run}"
        else:
            detail_text = (summary or "") + f"\n\nSaved /network XML file:\n- running: {saved_run}"
        
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Local Override Check")

        # Short heading only; put the long text in a scrollable view
        heading = ("⚠️ Local overrides detected — showing running /network") if has_override else ("No local overrides detected.")
        alert.setInformativeText_(heading)

        # Build a fixed-size scrollable text view for the detailed summary
        try:
            from AppKit import NSScrollView, NSTextView, NSFont, NSMakeSize
            # 600x320 fits well under the alert buttons on most screens
            scroll_frame = ((0, 0), (600, 320))
            scroll = NSScrollView.alloc().initWithFrame_(scroll_frame)
            scroll.setHasVerticalScroller_(True)
            scroll.setHasHorizontalScroller_(True)
            scroll.setAutohidesScrollers_(False)
            # Use a monospaced, non-wrapping text view so XML columns line up and can scroll horizontally
            text_view = NSTextView.alloc().initWithFrame_(scroll_frame)
            text_view.setEditable_(False)
            text_view.setSelectable_(True)
            try:
                mono = NSFont.monospacedSystemFontOfSize_weight_(11.0, 0.0)
                text_view.setFont_(mono)
            except Exception:
                pass
            text_view.setString_(detail_text or "")
            # Disable line wrapping and allow arbitrarily wide lines; enable horizontal scrolling
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
            # Apply XML highlighting when we're showing XML (i.e., when overrides are detected and we display running /network)
            try:
                # Heuristic: if it looks like XML (starts with '<' or '<?xml'), colorize it
                sample = (detail_text or "").lstrip()[:5]
                if sample.startswith("<"):
                    self._apply_xml_highlighting(text_view)
            except Exception:
                pass
            scroll.setDocumentView_(text_view)
            alert.setAccessoryView_(scroll)
        except Exception as e:
            # Fallback: if for any reason the scroll view fails, include trimmed text
            trimmed = (detail_text[:1800] + "\n... (truncated)") if len(detail_text) > 1800 else (detail_text or "")
            alert.setInformativeText_(f"{heading}\n\n{trimmed}")

        # Add icon if available
        if os.path.exists(ICON_PATH):
            icon_image = NSImage.alloc().initWithContentsOfFile_(ICON_PATH)
            if icon_image:
                alert.setIcon_(icon_image)
        
        from AppKit import NSApp
        # Buttons: OK and Copy Visible
        alert.addButtonWithTitle_("OK")
        alert.addButtonWithTitle_("Copy Visible")

        NSApp.activateIgnoringOtherApps_(True)
        resp = alert.runModal()
        # If user pressed Copy Visible (second button), copy content
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
        """Apply simple XML syntax coloring (blue tags, white attributes/values, red for errors) to the NSTextView."""
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

            # Start with an attributed string (default: white text, monospaced font)
            attr = {
                NSForegroundColorAttributeName: NSColor.whiteColor(),
            }
            attributed = NSMutableAttributedString.alloc().initWithString_attributes_(full_text, attr)

            # Regex patterns
            tag_pattern = re.compile(r"</?([A-Za-z_][\w:.-]*)([^>]*)>")  # <tag ...> or </tag>
            attr_pattern = re.compile(r"([A-Za-z_][\w:.-]*)(\s*=\s*)\"([^\"]*)\"")  # name="value"
            comment_pattern = re.compile(r"<!--.*?-->", re.DOTALL)
            error_word_pattern = re.compile(r"\berror\b", re.IGNORECASE)

            # Helper to apply color to a range
            def color_range(start, length, color):
                try:
                    attributed.addAttribute_value_range_(NSForegroundColorAttributeName, color, (start, length))
                except Exception:
                    pass

            text_str = full_text

            # Color XML comments gray
            for m in comment_pattern.finditer(text_str):
                color_range(m.start(), m.end() - m.start(), NSColor.grayColor())

            # Color tags and tag names blue; attributes stay white by default; values slightly lighter
            for m in tag_pattern.finditer(text_str):
                start, end = m.start(), m.end()
                # Entire tag brackets blue
                color_range(start, end - start, NSColor.systemBlueColor() if hasattr(NSColor, 'systemBlueColor') else NSColor.blueColor())
                # Inside attributes, recolor quoted values to a softer white (using controlHighlightColor fallback)
                inner = m.group(2) or ""
                if inner:
                    inner_start = start + text_str[start:end].find(inner)
                    for am in attr_pattern.finditer(inner):
                        val_group = am.group(3)
                        # Compute absolute positions of the quoted value within the full string
                        rel = am.start(3)
                        abs_start = inner_start + rel
                        abs_end = abs_start + len(val_group)
                        color_range(abs_start, abs_end - abs_start, NSColor.controlHighlightColor())

            # Highlight common error indicators in red
            for m in error_word_pattern.finditer(text_str):
                color_range(m.start(), m.end() - m.start(), NSColor.systemRedColor() if hasattr(NSColor, 'systemRedColor') else NSColor.redColor())

            # Apply back to the text view
            storage.setAttributedString_(attributed)
        except Exception as e:
            # If highlighting fails, leave plain text
            try:
                print(f"XML highlighting skipped: {e}")
            except Exception:
                pass

    def _apply_human_highlighting(self, text_view):
        """Apply simple 'Human Readable' coloring: keys (before ':') in blue, values in white."""
        try:
            from AppKit import NSColor
            from Foundation import NSMutableAttributedString, NSMakeRange
            from AppKit import NSForegroundColorAttributeName

            storage = text_view.textStorage()
            # Get full string whether via textStorage or direct string()
            full_text = str(storage.string()) if hasattr(storage, 'string') else text_view.string()
            if not full_text:
                return

            # Start with all text white
            attributed = NSMutableAttributedString.alloc().initWithString_attributes_(
                full_text,
                {NSForegroundColorAttributeName: NSColor.whiteColor()}
            )

            # Color the "key:" portion (up to first ':') on each line blue.
            offset = 0
            for line in full_text.splitlines(keepends=True):
                # Find first ':' that appears after at least one non-space character
                colon_idx = -1
                for i, ch in enumerate(line):
                    if ch == ':':
                        colon_idx = i
                        break
                if colon_idx > 0:
                    # Determine actual colored range: from line start to position *including* ':'.
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

            # Apply result back to the view
            storage.setAttributedString_(attributed)
        except Exception as e:
            try:
                print(f"Human highlighting skipped: {e}")
            except Exception:
                pass

    def _show_tabbed_alert(self, heading: str, tabs: list, width: int = 720, height: int = 400):
        """Show an NSAlert with a tabbed accessory view.
        `tabs` should be a list of (tab_title:str, content_text:str) tuples.
        """
        try:
            from AppKit import (
                NSAlert, NSTabView, NSTabViewItem, NSScrollView, NSTextView,
                NSFont, NSImage, NSApp, NSMakeRect
            )

            alert = NSAlert.alloc().init()
            alert.setMessageText_(heading)

            # Build tab view
            tab_view = NSTabView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))

            for (tab_title, content_text) in tabs:
                # Scrollable, monospaced text view per tab
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
                # Disable line wrapping and enable horizontal scrolling for wide, columnar text
                try:
                    from AppKit import NSMakeSize
                    text_view.setRichText_(False)
                    text_view.setHorizontallyResizable_(True)
                    text_view.setVerticallyResizable_(True)
                    tc = text_view.textContainer()
                    if tc:
                        # Allow arbitrarily wide lines; do not wrap to view width
                        tc.setContainerSize_(NSMakeSize(10_000_000.0, 10_000_000.0))
                        tc.setWidthTracksTextView_(False)
                    # Ensure the scroll view actually shows a horizontal scroller
                    scroll.setHasHorizontalScroller_(True)
                    scroll.setAutohidesScrollers_(False)
                except Exception:
                    pass
                # Apply coloring on tabs:
                # - Raw: XML-style highlighting (blue tags, white attributes/values)
                # - Human Readable: keys (before ':') in blue, values in white
                tab_lower = (tab_title or "").strip().lower()
                if tab_lower == "raw":
                    self._apply_xml_highlighting(text_view)
                elif tab_lower == "human readable":
                    # Keep human-readable in monospace, no-wrap with horizontal scrolling
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

            # Add icon if available
            if os.path.exists(ICON_PATH):
                icon_image = NSImage.alloc().initWithContentsOfFile_(ICON_PATH)
                if icon_image:
                    alert.setIcon_(icon_image)

            # Buttons: OK and Copy Visible
            alert.addButtonWithTitle_("OK")
            alert.addButtonWithTitle_("Copy Visible")

            NSApp.activateIgnoringOtherApps_(True)
            resp = alert.runModal()
            # If user pressed Copy Visible (second button), copy current tab content
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
            # Fallback: single-panel monospaced alert with concatenated sections
            joined = []
            for (t, txt) in tabs:
                joined.append(f"===== {t} =====\n{txt}")
            self._show_monospaced_alert(heading, "\n\n".join(joined), width=width, height=height)

    def _show_monospaced_alert(self, heading: str, detail_text: str, width: int = 640, height: int = 360):
        """
        Show a nicely formatted, human-readable block of text using a monospaced font
        inside an NSAlert with a scrollable NSTextView. This avoids truncation and
        preserves alignment for tabular data.
        """
        try:
            from AppKit import NSAlert, NSScrollView, NSView, NSMakeRect, NSSize, NSTextView, NSFont, NSImage, NSApp

            alert = NSAlert.alloc().init()
            alert.setMessageText_(heading)

            # Create a scroll view with a monospaced text view for alignment
            scroll_frame = NSMakeRect(0, 0, width, height)
            scroll = NSScrollView.alloc().initWithFrame_(scroll_frame)
            scroll.setHasVerticalScroller_(True)
            scroll.setHasHorizontalScroller_(True)
            scroll.setBorderType_(0)  # No border

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

            # Add icon if available
            if os.path.exists(ICON_PATH):
                icon_image = NSImage.alloc().initWithContentsOfFile_(ICON_PATH)
                if icon_image:
                    alert.setIcon_(icon_image)

            NSApp.activateIgnoringOtherApps_(True)
            alert.runModal()
        except Exception as e:
            # Very small fallback: keep it readable even if the scroll view fails
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
                # Nothing else we can do here
                print(detail_text)

    def log(self, message):
        try:
            handler = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=5)
            handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            logger = logging.getLogger(f"PanoramaSyncLogger_{id(self)}")
            if not logger.handlers:
                logger.addHandler(handler)
                logger.setLevel(logging.INFO)
            logger.info(message)
            # Also print to console for immediate debugging
            print(f"[{time.strftime('%H:%M:%S')}] {message}")
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
        if os.path.exists(LOGIN_FILE):
            try:
                with open(LOGIN_FILE, "r") as f:
                    data = json.load(f)
                    self.panorama_url = data.get("panorama_url")
                    self.username = data.get("username")
                    self.api_key = data.get("api_key")
                if self.panorama_url and self.username and self.api_key:
                    try:
                        self._notify("Panorama Sync", "Auto-login", f"Logged in as {self.username} to {self.panorama_url}")
                    except Exception as e:
                        logging.warning("Auto-login notify failed: %s", e)
                        try:
                            self.title = "Panorama Tools"
                        except Exception:
                            pass
            except Exception as e:
                print(f"Error loading stored login: {e}")

    def show_about(self, _):
        try:
            rumps.alert(VERSION)
        except Exception:
            # Fallback if rumps.alert fails for any reason
            print(VERSION)

    def store_login(self):
        data = {
            "panorama_url": self.panorama_url,
            "username": self.username,
            "api_key": self.api_key
        }
        try:
            with open(LOGIN_FILE, "w") as f:
                json.dump(data, f)
        except Exception as e:
            print(f"Error storing login: {e}")

    def _clear_loaded_state(self):
        """Clear all loaded/cached data and reset UI menus to their empty placeholders."""
        try:
            # Stop any pending timers we scheduled for UI updates
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

            # Clear caches and per-firewall state
            try:
                self._override_cache.clear()
            except Exception:
                pass
            try:
                self._fw_items.clear()
                self._fw_base.clear()
            except Exception:
                pass

            # Reset Firewalls menu
            try:
                self.firewalls_menu.clear()
                self.firewalls_menu.title = "Firewalls"
                self.firewalls_menu.add(rumps.MenuItem("No firewalls loaded"))
            except Exception:
                pass

            # Reset Device Groups menu
            try:
                self.device_groups_menu.clear()
                self.device_groups_menu.title = "Device Groups"
                self.device_groups_menu.add(rumps.MenuItem("No device groups loaded"))
            except Exception:
                pass

            # Reset Templates menu
            try:
                self.templates_menu.clear()
                self.templates_menu.title = "Templates"
                self.templates_menu.add(rumps.MenuItem("No templates loaded"))
            except Exception:
                pass

            # Reset Local Overrides submenu
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
        # Remove stored login and clear in-memory creds
        try:
            if os.path.exists(LOGIN_FILE):
                os.remove(LOGIN_FILE)
        except Exception:
            pass
        self.panorama_url = None
        self.username = None
        self.password = None
        self.api_key = None

        # Clear all loaded/cached objects and reset menus
        # Ensure this runs on the main thread to avoid AppKit issues
        try:
            if threading.current_thread().name == 'MainThread':
                self._clear_loaded_state()
            else:
                self._on_main(self._clear_loaded_state)
        except Exception:
            # Best-effort fallback
            self._clear_loaded_state()

        rumps.alert("Logged out. All cached items cleared. Please login again.")

    def login_to_panorama(self, _):
        # Bring app to front
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
            r = requests.get(api_url, verify=False, timeout=10)
            root = ET.fromstring(r.text)
            key = root.findtext(".//key")
            if key:
                self.panorama_url = pan_url
                self.username = username
                self.password = password
                self.api_key = key
                self.store_login()
                rumps.alert("Login successful! API key acquired.")
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
        """Run predefined show commands with the tabbed Raw/Human popup.
        which: one of 'ike', 'ipsec', 'prisma_ike', 'prisma_ipsec'
        """
        mapping = {
            'ike': "show vpn ike-sa",
            'ipsec': "show vpn ipsec-sa",
            # For Prisma variants, default to standard SA commands; adjust later if needed
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
        """Pretty-print XML without extra blank lines. Returns string."""
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
        """Execute a Panorama operational command with a **raw XML** cmd payload (no CLI wrapper)."""
        if not self.api_key:
            rumps.alert("Please login first.")
            return "Error: not logged in"
        try:
            from urllib.parse import quote
            url = f"https://{self.panorama_url}/api/?key={self.api_key}&type=op&cmd={quote(xml_cmd)}&target={serial}"
            self.log(f"[op] GET {url}")
            r = requests.get(url, verify=False, timeout=30)
            return r.text
        except Exception as e:
            return f"Error: {e}"

    def _humanize_generic_xml(self, xml_text: str, max_cols: int = 10, max_rows: int = 500) -> str:
        """Generic fallback: render sibling <entry> lists as a table; otherwise path->value pairs."""
        try:
            root = ET.fromstring(xml_text)
            entries = root.findall('.//entry')
            if entries:
                # collect column names
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
            # tree fallback
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
        """
        Render IKE SA summary into a table, supporting multiple PAN-OS schema variants.
        """
        try:
            def gv(elem, names, default=""):
                """Get first non-empty text from a list of candidate tag names."""
                for n in names:
                    v = elem.findtext(n)
                    if v is not None and str(v).strip() != "":
                        return str(v).strip()
                return default

            root = ET.fromstring(xml_text)
            entries = root.findall(".//entry")
            rows = []

            for e in entries:
                # Gateway / name
                gw = gv(e, ["gateway", "name"]) or (e.get("name") or "").strip()
                if only_gateway and gw != only_gateway:
                    continue

                # State may be absent in some schemas
                state = gv(e, ["state", "sa", "status", "phase1-state"])

                # Version/mode (IKEv1/IKEv2/etc.)
                version = gv(e, ["version", "mode"])

                # Local / Peer IPs (if present)
                local_ip = gv(e, ["local-ip", "local", "local_ip"])
                peer_ip = gv(e, ["peer-ip", "peer", "peer_ip", "remote"])

                # Established time; fall back to created/start-time if provided
                established = gv(e, ["established", "created", "start-time", "start_time"])

                # If the response uses role/algo instead of state/ips/times, still show useful info.
                role = gv(e, ["role"])
                algo = gv(e, ["algo", "algorithm"])

                # Prefer classic columns; if most are blank, populate with alternate fields
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
        """
        Render IPsec SA summary into a table, supporting multiple PAN-OS schema variants.
        """
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

    # === Exact menu actions to match Panorama Prisma Tools v1.3 ===
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
            r = requests.get(api_url, verify=False, timeout=10)
            xml_filename = os.path.join(WORK_DIR, "firewall_sync_log.xml")
            with open(xml_filename, "w") as f:
                f.write(r.text)
            self.log(f"[Firewalls] Raw response:\n{r.text}")
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

                # Determine connection status from available fields
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

                # Record base label
                self._fw_base[norm_serial] = (icon, hostname)

                item = rumps.MenuItem(f"{icon} {hostname}")
                # Build per-firewall submenu
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
                # Custom Commands submenu
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
                # Skypath commands submenu
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
            r = requests.get(api_url, verify=False, timeout=10)
            xml_filename = os.path.join(WORK_DIR, "firewall_connected_log.xml")
            with open(xml_filename, "w") as f:
                f.write(r.text)
            self.log(f"[Firewalls Connected] Raw response:\n{r.text}")

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

                # These are connected-only results; show green icon
                icon = "🟢"
                self._fw_base[norm_serial] = (icon, hostname)

                item = rumps.MenuItem(f"{icon} {hostname}")
                # Build per-firewall submenu (same as fetch_firewalls)
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
        """Temporary placeholder for VPN/Prisma helpers until fully implemented."""
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
            r = requests.get(api_url, verify=False, timeout=10)
            self.log(f"[Device Groups] Raw response:\n{r.text}")
            xml_filename = os.path.join(WORK_DIR, "device_group_sync_log.xml")
            with open(xml_filename, "w") as f:
                f.write(r.text)
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
                        # Add status icon based on sync status
                        if sync_status.lower() == "in sync":
                            icon = "🟢"
                        elif sync_status.lower() == "out of sync":
                            icon = "🔴"
                        else:
                            icon = "🟡"
                        label = f"{icon} {hostname}: {sync_status}"
                        item = rumps.MenuItem(label)
                        # Add submenu for push action
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
            r = requests.get(api_url, verify=False, timeout=10)
            self.log(f"[Templates] Raw response:\n{r.text}")
            xml_filename = os.path.join(WORK_DIR, "template_sync_log.xml")
            with open(xml_filename, "w") as f:
                f.write(r.text)
            root = ET.fromstring(r.text)
            entries = root.findall(".//entry")
            if not entries:
                self.templates_menu.clear()
                self.templates_menu.add(rumps.MenuItem("No templates found"))
                return
            self.templates_menu.clear()
            self.templates_menu.title = "Templates"
            items = []
            # Add all hostnames with sync status
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
        # Fetch All should only fetch all firewalls
        self.fetch_firewalls(_)

    def fetch_system_info(self, ip, serial):
        """System info popup with two tabs: Raw and Human Readable."""
        if not self.api_key:
            rumps.alert("Please login first.")
            return
        try:
            cmd = "<show><system><info></info></system></show>"
            url = f"https://{self.panorama_url}/api/?type=op&target={serial}&cmd={cmd}&key={self.api_key}"
            r = requests.get(url, verify=False, timeout=15)

            # Build RAW (pretty-printed XML if possible)
            raw_text = r.text or ""
            try:
                import xml.dom.minidom as minidom
                dom = minidom.parseString(raw_text)
                pretty_xml = dom.toprettyxml(indent="  ")
                pretty_lines = [line for line in pretty_xml.splitlines() if line.strip()]
                raw_pretty = "\n".join(pretty_lines)
            except Exception:
                raw_pretty = raw_text

            # Build Human Readable
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

            self.log(f"[System Info for {ip}]\n-- Human Readable --\n{human_text}\n\n-- Raw --\n{raw_pretty}")

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

        # If user provided a full op-XML payload, execute it directly
        if cmd.startswith("<") and cmd.endswith(">"):
            xml_text = self.run_op_cmd(serial, cmd)
            raw_pretty = self._pretty_xml(xml_text)
            human_text = self._humanize_generic_xml(xml_text)
            self._show_tabbed_alert(popup_title, [("Raw", raw_pretty), ("Human Readable", human_text)], width=760, height=420)
            return

        # Supported commands
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
            # Fallback: execute unmapped plain-text commands via PAN-OS CLI wrapper
            plaintext = cmd
            xml_wrapped = f"<request><cli><raw>{plaintext}</raw></cli></request>"
            xml_text = self.run_op_cmd(serial, xml_wrapped)
            raw_pretty = self._pretty_xml(xml_text)
            human_text = self._humanize_generic_xml(xml_text)
            self._show_tabbed_alert(popup_title + " (raw)", [("Raw", raw_pretty), ("Human Readable", human_text)], width=760, height=420)
            return

        # Execute using op-XML
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

                # Use app icon
                if os.path.exists(ICON_PATH):
                    icon_image = NSImage.alloc().initWithContentsOfFile_(ICON_PATH)
                    if icon_image:
                        alert.setIcon_(icon_image)

                # Accessory view: presets dropdown + text field
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
                        self.log(f"[CLI Output for {ip}] (op-XML) {cli_cmd[:120]}")
                    except Exception as e:
                        self._show_monospaced_alert("CLI Error (op-XML)", f"{e}")
                    continue

                try:
                    self._execute_cli_command(serial, cli_cmd, popup_title=f"CLI: {cli_cmd}")
                    self.log(f"[CLI Output for {ip}] Command: {cli_cmd} (shown in popup)")
                except Exception as e:
                    self._show_monospaced_alert("CLI Error", f"{cli_cmd}: {e}")

                continue

            except Exception as e:
                # Fallback using rumps if AppKit fails
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
                        self.log(f"[CLI Output for {ip}] (op-XML) {cli_cmd[:120]}")
                    else:
                        self._execute_cli_command(serial, cli_cmd, popup_title=f"CLI: {cli_cmd}")
                        self.log(f"[CLI Output for {ip}] Command: {cli_cmd} (shown in popup)")
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
            self.log(f"[Device Group Push] URL: {url}")
            r = requests.get(url, verify=False, timeout=30)
            self.log(f"[Device Group Push] Response:\n{r.text}")

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
            self.log(f"[Template Push] URL: {url}")
            r = requests.get(url, verify=False, timeout=30)
            self.log(f"[Template Push] Response:\n{r.text}")

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
    # Disable urllib3 SSL warnings
    urllib3.disable_warnings()
    warnings.filterwarnings("ignore", category=InsecureRequestWarning)
    warnings.filterwarnings("ignore", category=NotOpenSSLWarning)
    PanoramaSyncMonitor().run()
