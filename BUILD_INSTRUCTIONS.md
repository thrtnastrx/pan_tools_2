# Panorama Tools - macOS App Build Instructions

## Quick Start

The easiest way to build the app:

```bash
chmod +x build_app.sh
./build_app.sh
```

The script will:
1. Create a virtual environment (if needed)
2. Install all dependencies
3. Clean previous builds
4. Build the .app bundle
5. Show you where to find it

## Manual Build Process

If you prefer to build manually:

### 1. Install Dependencies

```bash
# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate

# Install requirements
pip install -r requirements.txt
```

### 2. Build the App

```bash
# Clean previous builds
rm -rf build dist

# Build
python setup.py py2app
```

### 3. Install the App

```bash
# Copy to Applications
cp -r "dist/Panorama Tools.app" /Applications/

# Or just open the dist folder in Finder and drag it
open dist
```

## Troubleshooting

### "Cannot be opened because the developer cannot be verified"

macOS Gatekeeper blocks unsigned apps. To run:

**Option 1: Right-click method (easiest)**
1. Right-click (or Control-click) the app
2. Select "Open"
3. Click "Open" in the dialog

**Option 2: System Preferences**
1. Try to open the app normally
2. Go to System Preferences > Security & Privacy
3. Click "Open Anyway"

**Option 3: Remove quarantine attribute**
```bash
xattr -cr "/Applications/Panorama Tools.app"
```

### Code Signing (Optional but Recommended)

To properly sign your app:

```bash
# Find your signing identity
security find-identity -v -p codesigning

# Sign the app
codesign --deep --force --sign "Your Developer ID" "dist/Panorama Tools.app"

# Verify signature
codesign --verify --verbose "dist/Panorama Tools.app"
```

### Unicode/Encoding Errors

If you see Unicode errors during build, set:

```bash
export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8
python setup.py py2app
```

### Missing Icon

If `pan-logo-1.png` is missing, the app will build but won't have an icon. Either:
- Add the icon file to the project directory
- Or comment out the `iconfile` line in `setup.py`

### Import Errors at Runtime

If the app launches but crashes with import errors:

1. Check the Console.app for error messages
2. Add missing modules to the `packages` list in `setup.py`
3. Rebuild the app

Common modules to add:
```python
'packages': [
    'requests.adapters',
    'urllib3.util.retry',
    'urllib3.exceptions',
    # ... add others as needed
],
```

### App is Too Large

To reduce size:

1. Use `--optimize 2` (already set in setup.py)
2. Add more exclusions:
```python
'excludes': [
    'matplotlib', 'numpy', 'scipy', 'tkinter',
    'PyQt5', 'PIL', 'pygame', 'django',
],
```

3. Strip debug symbols:
```bash
find "dist/Panorama Tools.app" -name "*.so" -exec strip -x {} \;
```

## Development Mode

To test without building a full .app:

```bash
# Alias mode (faster, for testing)
python setup.py py2app -A

# This creates a symlinked app that runs from your source directory
```

## Distribution

### Creating a DMG

To distribute your app in a disk image:

```bash
# Install create-dmg
brew install create-dmg

# Create DMG
create-dmg \
  --volname "Panorama Tools" \
  --window-pos 200 120 \
  --window-size 600 400 \
  --icon-size 100 \
  --icon "Panorama Tools.app" 175 120 \
  --hide-extension "Panorama Tools.app" \
  --app-drop-link 425 120 \
  "Panorama-Tools-v2.0.dmg" \
  "dist/"
```

### Notarization (for Distribution)

For apps distributed outside the Mac App Store:

1. Code sign with Developer ID certificate
2. Create a signed .zip or .dmg
3. Submit for notarization:
```bash
xcrun notarytool submit Panorama-Tools-v2.0.dmg \
  --apple-id "your@email.com" \
  --team-id "TEAM_ID" \
  --password "app-specific-password"
```

## Files Created

After building, you'll see:

```
build/              # Temporary build files (can delete)
dist/
  └── Panorama Tools.app/   # Your finished app
      ├── Contents/
      │   ├── Info.plist
      │   ├── MacOS/
      │   │   └── Panorama Tools  # Executable
      │   ├── Resources/
      │   │   ├── pan-logo-1.png
      │   │   └── ... Python libs
      │   └── Frameworks/
```

## Runtime Behavior

When running as a .app bundle:
- **Working directory**: `~/Library/Application Support/PanoramaTools/`
- **Log files**: Stored in Application Support folder
- **Credentials**: Stored in macOS Keychain (secure)
- **Config files**: Also in Application Support folder

## Need Help?

Common issues:
- App won't launch → Check Console.app for crash logs
- SSL errors → Check "SSL Settings" in the app menu
- Missing credentials → Re-login through the app
- High CPU usage → Check if override detection is running

## Clean Build

To start fresh:

```bash
# Remove all build artifacts
rm -rf build dist venv
rm -rf ~/Library/Application\ Support/PanoramaTools/

# Rebuild from scratch
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python setup.py py2app
```
