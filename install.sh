#!/bin/bash
# copycmd installer — inline copy buttons for terminal commands
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="${HOME}/.copycmd"
GHOSTTY_CONFIG="${HOME}/.config/ghostty/config"

echo "Installing copycmd..."
mkdir -p "$INSTALL_DIR"

# 1. Install the PTY proxy
cp "$SCRIPT_DIR/copycmd-proxy.py" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/copycmd-proxy.py"

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

# 3. Configure Ghostty to use the proxy
if [ -f "$GHOSTTY_CONFIG" ]; then
  if grep -q "copycmd" "$GHOSTTY_CONFIG" 2>/dev/null; then
    echo "Ghostty already configured for copycmd"
  else
    echo "" >> "$GHOSTTY_CONFIG"
    echo "# copycmd — inline copy buttons for commands in terminal output" >> "$GHOSTTY_CONFIG"
    echo "command = ${INSTALL_DIR}/copycmd-proxy.py" >> "$GHOSTTY_CONFIG"
    echo "Added copycmd proxy to Ghostty config"
  fi
else
  echo ""
  echo "⚠ Ghostty config not found at $GHOSTTY_CONFIG"
  echo "  Add this line to your Ghostty config manually:"
  echo "  command = ${INSTALL_DIR}/copycmd-proxy.py"
fi

# 4. Remove old ZSH plugin from .zshrc if present
ZSHRC="${HOME}/.zshrc"
if grep -q "copycmd" "$ZSHRC" 2>/dev/null; then
  # Remove old copycmd lines
  sed -i '' '/copycmd/d' "$ZSHRC"
  echo "Removed old copycmd ZSH plugin from .zshrc (no longer needed)"
fi

echo ""
echo "✓ copycmd installed!"
echo ""
echo "Restart Ghostty and every command's output will have clickable"
echo " copy  buttons next to detected commands."
echo ""
echo "Works with everything — cat, claude, npm, any program."
echo "Cmd+click the  copy  button to copy the command to clipboard."
