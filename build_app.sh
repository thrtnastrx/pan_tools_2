#!/bin/bash
# Build script for Panorama Tools macOS app

set -e  # Exit on error

echo "================================================"
echo "Panorama Tools - macOS .app Builder"
echo "================================================"
echo ""

# Check if we're in the right directory
if [ ! -f "panorama_tools_v2.0_secure.py" ]; then
    echo "❌ Error: panorama_tools_v2.0_secure.py not found"
    echo "Please run this script from the project directory"
    exit 1
fi

# Check if icon exists
if [ ! -f "pan-logo-1.png" ]; then
    echo "⚠️  Warning: pan-logo-1.png not found"
    echo "The app will build without an icon"
fi

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
echo "🔄 Activating virtual environment..."
source venv/bin/activate

# Install/upgrade dependencies
echo "📥 Installing dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# Clean previous builds
echo "🧹 Cleaning previous builds..."
rm -rf build dist

# Build the app
echo "🔨 Building Panorama Tools.app..."
python setup.py py2app

# Check if build succeeded
if [ -d "dist/Panorama Tools.app" ]; then
    echo ""
    echo "================================================"
    echo "✅ Build successful!"
    echo "================================================"
    echo ""
    echo "Your app is located at:"
    echo "  $(pwd)/dist/Panorama Tools.app"
    echo ""
    echo "To install:"
    echo "  1. Open Finder and navigate to the 'dist' folder"
    echo "  2. Drag 'Panorama Tools.app' to your Applications folder"
    echo "  3. Launch from Applications or Spotlight"
    echo ""
    echo "Note: On first launch, you may need to:"
    echo "  - Right-click the app and select 'Open'"
    echo "  - Or go to System Preferences > Security & Privacy"
    echo "    and click 'Open Anyway'"
    echo ""
else
    echo "❌ Build failed - app not found in dist folder"
    exit 1
fi
