#!/bin/bash
# copycmd installer — inline copy buttons for terminal commands
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="${HOME}/.copycmd"
PLUGIN_FILE="copycmd.plugin.zsh"
ZSHRC="${HOME}/.zshrc"

echo "Installing copycmd..."
mkdir -p "$INSTALL_DIR"

# 1. Install plugin
cp "$SCRIPT_DIR/$PLUGIN_FILE" "$INSTALL_DIR/"

# 2. Build the URL scheme handler (macOS app for click-to-copy)
echo "Building URL handler..."
osacompile -o "$INSTALL_DIR/CopyCmd.app" "$SCRIPT_DIR/copycmd-handler.applescript" 2>/dev/null

PLIST="$INSTALL_DIR/CopyCmd.app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes array" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0 dict" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0:CFBundleURLName string 'CopyCmd URL'" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0:CFBundleURLSchemes array" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0:CFBundleURLSchemes:0 string 'copycmd'" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :LSBackgroundOnly bool true" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier com.copycmd.urlhandler" "$PLIST" 2>/dev/null || true

# Register with macOS
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -R "$INSTALL_DIR/CopyCmd.app"

# 3. Add to .zshrc
if grep -q "copycmd" "$ZSHRC" 2>/dev/null; then
  echo "copycmd already in .zshrc (updated plugin file)"
else
  echo "" >> "$ZSHRC"
  echo "# copycmd - inline copy buttons for commands in terminal output" >> "$ZSHRC"
  echo "source ${INSTALL_DIR}/${PLUGIN_FILE}" >> "$ZSHRC"
  echo "Added copycmd to $ZSHRC"
fi

echo ""
echo "✓ copycmd installed!"
echo ""
echo "Restart your shell or run: source ${INSTALL_DIR}/${PLUGIN_FILE}"
echo ""
echo "How it works:"
echo "  Just use your terminal normally. When output contains commands like"
echo "  'npm install' or 'git clone ...', a clickable [⧉] button appears"
echo "  next to them. Click it to copy the command to your clipboard."
