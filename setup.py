"""
Setup script for building Panorama Tools as a macOS .app bundle
Usage: python setup.py py2app
"""

from setuptools import setup

APP = ['panorama_tools_v2.0_secure.py']
DATA_FILES = [('', ['pan-logo-1.png'])]

OPTIONS = {
    'argv_emulation': False,
    'iconfile': 'pan-logo-1.png',
    'plist': {
        'CFBundleName': 'Panorama Tools',
        'CFBundleDisplayName': 'Panorama Tools',
        'CFBundleIdentifier': 'com.yourcompany.panoramatools',
        'CFBundleVersion': '2.0.0',
        'CFBundleShortVersionString': '2.0',
        'LSUIElement': True,
        'NSHighResolutionCapable': True,
        'NSRequiresAquaSystemAppearance': False,
        'LSMinimumSystemVersion': '10.14.0',
    },
    'packages': [
        'rumps',
        'requests',
        'urllib3',
        'keyring',
        'objc',
        'Foundation',
        'AppKit',
        'CoreFoundation',  # Explicitly include CoreFoundation
    ],
    'includes': [
        'xml.etree.ElementTree',
        'xml.dom.minidom',
        'logging.handlers',
        'urllib3.util.retry',
        'urllib3.exceptions',
        'requests.adapters',
        'concurrent.futures',
        # Explicitly include PyObjC modules
        'objc._objc',
        'CoreFoundation._CoreFoundation',
        'Foundation._Foundation',
        'AppKit._AppKit',
    ],
    'excludes': [
        'matplotlib',
        'numpy',
        'scipy',
        'pandas',
        'PIL',
        'tkinter',
    ],
    # Use system frameworks instead of bundling
    'frameworks': [],
    'semi_standalone': False,
    'site_packages': True,  # Include site-packages
    'strip': False,  # Don't strip debug symbols (helps with debugging)
    'optimize': 0,  # Don't optimize for easier debugging
}

setup(
    name='Panorama Tools',
    app=APP,
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
    install_requires=[
        'rumps>=0.4.0',
        'requests>=2.31.0',
        'urllib3>=2.0.0',
        'keyring>=24.0.0',
        'pyobjc-framework-Cocoa>=9.0',
        'pyobjc-core>=9.0',
    ],
)
