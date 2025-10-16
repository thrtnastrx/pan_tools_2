# 🚀 Quick Start Guide - Panorama Tools v2.0 Secure

## TL;DR - Build in 3 Steps

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Build the app
python setup.py py2app

# 3. Run it
open "dist/Panorama Tools.app"
```

---

## 📦 What You Have

After implementing all security improvements, your project should have:

```
PanoramaTools/
├── panorama_tools_v2.0_secure.py  # Main secure application
├── setup.py                        # Build configuration
├── requirements.txt                # Dependencies
├── pan-logo-1.png                  # App icon
├── BUILD_INSTRUCTIONS.md           # Detailed build guide
├── SECURITY_IMPROVEMENTS.md        # Security documentation
└── QUICK_START.md                  # This file
```

---

## 🔒 What Changed? (Executive Summary)

### Old Version → Secure Version

| Security Issue | Status |
|----------------|--------|
| 🔴 Plain text passwords | ✅ **FIXED** - Now uses macOS Keychain |
| 🔴 No SSL verification | ✅ **FIXED** - SSL enabled by default |
| 🟡 API keys in logs | ✅ **FIXED** - Logs fully sanitized |
| 🟡 World-readable files | ✅ **FIXED** - Files locked to owner (600) |
| 🟡 Writes to CWD | ✅ **FIXED** - Uses Application Support |

**Bottom Line**: The app is now production-ready and follows macOS security best practices.

---

## 🎯 First-Time Setup

### 1. Verify Prerequisites
```bash
# Check Python version (need 3.8+)
python3 --version

# Check for Xcode tools
xcode-select --version

# If missing, install:
xcode-select --install
```

### 2. Project Setup
```bash
# Create project folder
mkdir -p ~/Desktop/PanoramaTools
cd ~/Desktop/PanoramaTools

# Add your files:
# - panorama_tools_v2.0_secure.py
# - setup.py
# - requirements.txt
# - pan-logo-1.png
```

### 3. Virtual Environment (Optional but Recommended)
```bash
python3 -m venv venv
source venv/bin/activate
```

### 4. Install and Build
```bash
pip install -r requirements.txt
python setup.py py2app
```

### 5. Test
```bash
open "dist/Panorama Tools.app"
```

---

## ⚡ Common Quick Commands

### Clean and Rebuild
```bash
rm -rf build dist
python setup.py py2app
```

### Fast Development Build
```bash
python setup.py py2app -A  # Alias mode (faster)
```

### Install to Applications
```bash
cp -R "dist/Panorama Tools.app" /Applications/
```

### Remove Quarantine (if needed)
```bash
xattr -cr "dist/Panorama Tools.app"
```

### View Logs
```bash
tail -f ~/Library/Application\ Support/PanoramaTools/panorama_sync_log.txt
```

### Reset Everything
```bash
rm -rf ~/Library/Application\ Support/PanoramaTools/
```

---

## 🧪 Quick Test After Building

```bash
# 1. Start the app
open "dist/Panorama Tools.app"

# 2. Check menu bar - you should see "Panorama Tools" or icon

# 3. Try to start second instance
open "dist/Panorama Tools.app"
# Should show: "Another instance is already running"

# 4. Login and verify Keychain storage
# After login, check:
security find-generic-password -s "PanoramaTools"
# Should find your credential

# 5. Check file permissions
ls -la ~/Library/Application\ Support/PanoramaTools/
# Should show: -rw------- for *.json files

# 6. Check logs are sanitized
cat ~/Library/Application\ Support/PanoramaTools/panorama_sync_log.txt
# Should see ***REDACTED*** instead of actual keys
```

---

## 🐛 Quick Troubleshooting

### Build Fails
```bash
# Clear everything and try again
pip install --upgrade pip setuptools py2app
rm -rf build dist
python setup.py py2app
```

### App Won't Open
```bash
# Remove Gatekeeper quarantine
xattr -cr "dist/Panorama Tools.app"

# Check for errors
Console.app → User Reports
```

### "Another instance running" but it's not
```bash
# Remove stale lock
rm ~/Library/Application\ Support/PanoramaTools/panorama_tools.lock
```

### Keyring Not Working
```bash
# Verify keyring is installed
pip install keyring

# The app will fall back to JSON storage automatically
# Check "About" menu to see status
```

### SSL Errors
```
In app: Options → SSL Settings → Disable SSL Verification (temporarily)
Or: Set Custom CA Path to your certificate
```

---

## 📊 What to Check for Security

### ✅ Checklist
- [ ] API key NOT in `panorama_sync_login.json` (only in Keychain)
- [ ] Logs don't contain real API keys (grep for "***REDACTED***")
- [ ] Files in Application Support have 600 permissions
- [ ] SSL verification is enabled (Options → SSL Settings)
- [ ] App prevents multiple instances
- [ ] "About" shows "Secure credential storage: Enabled (Keychain)"

### Quick Verify Script
```bash
#!/bin/bash
echo "🔍 Security Check..."

# Check for credentials in JSON
if grep -q "api_key" ~/Library/Application\ Support/PanoramaTools/*.json 2>/dev/null; then
    echo "⚠️  WARNING: Found api_key in JSON file!"
else
    echo "✅ No API keys in JSON files"
fi

# Check file permissions
perms=$(stat -f "%Op" ~/Library/Application\ Support/PanoramaTools/panorama_sync_login.json 2>/dev/null | tail -c 4)
if [ "$perms" = "0600" ]; then
    echo "✅ File permissions correct (600)"
else
    echo "⚠️  WARNING: File permissions are $perms (should be 600)"
fi

# Check for keychain entry
if security find-generic-password -s "PanoramaTools" >/dev/null 2>&1; then
    echo "✅ Keychain entry found"
else
    echo "ℹ️  No keychain entry (login first)"
fi

echo "✅ Security check complete"
```

---

## 🚢 Ready for Production?

### Before Distribution

1. **Test thoroughly**
   - [ ] Fresh macOS user account test
   - [ ] SSL validation works
   - [ ] Credentials survive app restart
   - [ ] Logs are sanitized

2. **Consider code signing** (Optional)
   ```bash
   codesign -s "Developer ID Application: Your Name" "dist/Panorama Tools.app"
   ```

3. **Create DMG** (Optional)
   ```bash
   hdiutil create -volname "Panorama Tools" -srcfolder "dist/Panorama Tools.app" -ov -format UDZO PanoramaTools.dmg
   ```

4. **Document known issues**
   - macOS 10.14+ prompts for network access (expected)
   - First SSL connection may be slow (cert validation)

---

## 📚 Next Steps

1. **Read the full documentation**:
   - `BUILD_INSTRUCTIONS.md` - Complete build guide
   - `SECURITY_IMPROVEMENTS.md` - Security details

2. **Customize for your environment**:
   - Update Bundle ID in `setup.py`
   - Replace icon with your company logo
   - Adjust default Panorama hostname

3. **Set up distribution**:
   - Consider using Jamf, Munki, or similar for deployment
   - Create user documentation
   - Set up feedback channel

---

## 🆘 Need Help?

### Resources
- **Build Issues**: See `BUILD_INSTRUCTIONS.md` → Troubleshooting
- **Security Questions**: See `SECURITY_IMPROVEMENTS.md`
- **Python Errors**: Check logs in `~/Library/Application Support/PanoramaTools/`

### Common Gotchas
- Forgot to activate venv → `source venv/bin/activate`
- Missing dependencies → `pip install -r requirements.txt`
- Icon not showing → Check `pan-logo-1.png` is in project root
- SSL errors → Normal for self-signed certs; use custom CA

---

## 🎉 Success!

If you've built successfully:
1. ✅ App appears in menu bar
2. ✅ Can login to Panorama
3. ✅ Credentials stored in Keychain
4. ✅ SSL validation working
5. ✅ All data in Application Support

**You're ready to deploy!** 🚀

---

**Version**: 2.0 Secure  
**Build Time**: ~5 minutes  
**Security Level**: Production-ready ✅