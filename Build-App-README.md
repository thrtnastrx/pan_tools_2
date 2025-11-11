# Panorama Tools - macOS .app Build Package

This package contains everything you need to build **Panorama Tools** as a native macOS application (.app bundle).

## 📦 What's Included

- **setup.py** - py2app configuration for building the .app
- **build_app.sh** - Automated build script (easiest method)
- **BUILD_INSTRUCTIONS.md** - Complete build guide with troubleshooting
- **QUICK_REFERENCE.md** - Quick command reference

## 🚀 Quick Start (3 Steps)

### 1. Prepare Your Project

Make sure you have these files in your project directory:
```
your-project/
├── panorama_tools_v2.0_secure.py  ← Your main script
├── requirements.txt               ← Python dependencies
├── pan-logo-1.png                 ← App icon (optional)
├── setup.py                       ← BUILD CONFIG (from this package)
└── build_app.sh                   ← BUILD SCRIPT (from this package)
```

### 2. Run the Build Script

```bash
cd ~/py/pan_tools_2_app  # or wherever your project is
chmod +x build_app.sh
./build_app.sh
```

### 3. Install the App

```bash
# Option 1: Drag and drop
open dist  # Then drag "Panorama Tools.app" to Applications

# Option 2: Command line
cp -r "dist/Panorama Tools.app" /Applications/
```

## 🎯 First Launch

macOS will likely block the app since it's not signed by an Apple Developer ID.

**Fix it:**
```bash
xattr -cr "/Applications/Panorama Tools.app"
```

Or right-click the app → Open → Open

## 📋 Requirements

- macOS 10.13 or later
- Python 3.7 or later
- All dependencies in requirements.txt

## 🛠️ What the Build Does

The build process:
1. Creates a virtual environment
2. Installs all Python dependencies
3. Packages everything using py2app
4. Creates a self-contained .app bundle
5. Includes your icon (if present)
6. Sets up proper macOS app structure

## 📁 Build Output

After building successfully:

```
dist/
└── Panorama Tools.app/
    ├── Contents/
    │   ├── Info.plist          ← App metadata
    │   ├── MacOS/
    │   │   └── Panorama Tools  ← Main executable
    │   ├── Resources/
    │   │   ├── pan-logo-1.png  ← Your icon
    │   │   └── lib/            ← Python + dependencies
    │   └── Frameworks/         ← System frameworks
```

## 🔧 Customization

### Change App Name
Edit in `setup.py`:
```python
'CFBundleName': 'Your App Name',
```

### Change Bundle ID
Edit in `setup.py`:
```python
'CFBundleIdentifier': 'com.yourcompany.yourapp',
```

### Add More Resources
Add files to `DATA_FILES` in `setup.py`:
```python
DATA_FILES = [
    ('', ['pan-logo-1.png', 'config.json', 'other-file.txt'])
]
```

## 📖 Documentation

- **QUICK_REFERENCE.md** - Fast command lookup
- **BUILD_INSTRUCTIONS.md** - Detailed guide with troubleshooting

## 🐛 Common Issues

### "ModuleNotFoundError" when running app
Add missing modules to `packages` in setup.py

### App is too large (>100MB)
Add exclusions to setup.py to remove unused packages

### Unicode errors during build
```bash
export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8
```

### Want to distribute the app?
See "Distribution" section in BUILD_INSTRUCTIONS.md for:
- Creating DMG files
- Code signing
- Notarization

## 🔄 Rebuild

To rebuild after changes:
```bash
rm -rf build dist
source venv/bin/activate
python setup.py py2app
```

Or just run `./build_app.sh` again

## 💾 Where App Data is Stored

When running as .app:
- **Application data**: `~/Library/Application Support/PanoramaTools/`
- **Logs**: Same location
- **Credentials**: macOS Keychain (secure)

When running as script:
- **Application data**: Current directory
- **Logs**: Current directory
- **Credentials**: `.panorama_sync_login` file

## ✅ Success Checklist

- [ ] Copy setup.py and build_app.sh to your project
- [ ] Have pan-logo-1.png in your project (optional)
- [ ] Run `./build_app.sh`
- [ ] Find app in `dist/` folder
- [ ] Copy to Applications
- [ ] Run `xattr -cr` command if blocked
- [ ] Launch and test!

## 🆘 Need Help?

1. Check QUICK_REFERENCE.md for common commands
2. Check BUILD_INSTRUCTIONS.md for detailed troubleshooting
3. Look at Console.app for app crash logs
4. Check build/temp files for build errors

## 📝 Notes

- First build takes 2-5 minutes
- Subsequent builds are faster
- The .app is completely self-contained
- No need for users to install Python
- Works on any modern Mac (10.13+)

---

**Ready to build?** Run `./build_app.sh` and you're done!
