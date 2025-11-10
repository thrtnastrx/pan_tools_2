# Panorama Tools v2.0 Secure

**Panorama Tools v2.0 Secure** is a macOS menu-bar application for managing and monitoring **Palo Alto Networks Panorama** and its connected firewalls via the XML API.  
It provides a fast, secure, GUI-based interface for running operational commands, detecting local overrides, testing VPN/IPSec tunnels, and automating configuration checks — all without needing to open the Panorama web interface or CLI.

---

## 🧭 Features

### 🔑 Secure Authentication
- Log in directly from the menu bar.
- Credentials stored safely using macOS **Keychain** (via `keyring`).
- Auto-login on app restart.

### 🔍 Panorama Fetching
- Fetch all or connected **firewalls**, **device groups**, and **templates**.
- Quickly view system info or open firewalls in a browser.
- Auto-fetch key data after login.

### ⚙️ Operational Commands
- Run any operational (`type=op`) commands directly from the UI.
- Built-in summaries for:
  - IKE Gateways (`show vpn ike-sa summary`)
  - IPsec Tunnels (`show vpn ipsec-sa summary`)
  - Prisma-specific gateways/tunnels
- View results in a dual-tab **Raw XML / Human-Readable** popup.

### 🔬 VPN & IPSec Testing
- One-click **Test Prisma IKE GW** and **Test Prisma IPSec Tunnel** commands.
- Supports all `test ...` CLI commands, including custom ones.
- Converts CLI → XML automatically and runs through Panorama’s API.

### ⚠️ Local Override Detection
- Detects configuration drift between Panorama templates and running configs.
- Highlights devices with overrides using ⚠️ indicators in the menu.
- Saves full XML snapshots for audit/troubleshooting.

### 💬 Custom CLI Commands
- Add your own Panorama commands to the menu.
- Run against any connected firewall.
- Custom `test ...` commands are automatically routed through the API test engine.
- Add / Delete from the menu without editing code.

### 🔒 SSL & Security Controls
- Toggle SSL verification from the GUI.
- Optionally set a custom CA path.
- Secure file permissions enforced (600).
- Connection retry & SSL error recovery built-in.

### 🪟 macOS-Native Interface
- Uses **rumps** (Objective-C bridge) for native menu-bar integration.
- Popups, text views, and copy-to-clipboard actions built with AppKit.
- Fully functional even when the main Panorama web GUI is busy.

---

## 🧰 Installation

### Requirements
- macOS **12 (Monterey)** or newer
- Python **3.10+**
- Recommended virtual environment

### Dependencies
Install from `requirements.txt` (example):
```bash
pip install -r requirements.txt
