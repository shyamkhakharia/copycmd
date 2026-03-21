# copycmd

A ZSH plugin that automatically detects commands in your terminal output and places a clickable **[⧉]** copy button next to each one. Click it, and the command is copied to your clipboard.

Works with any terminal that supports [OSC 8 hyperlinks](https://gist.github.com/egmontkob/eb114294efbcd5adb1944c9f3cb5feda) — including **Ghostty**, **iTerm2**, **Kitty**, **WezTerm**, and others.

## Demo

When a command's output contains recognizable commands, copy buttons appear inline:

```
$ cat README.md
To get started:
  npm install --save-dev typescript  [⧉]    ← click to copy
  git clone https://github.com/foo/bar.git  [⧉]    ← click to copy

Regular text has no button.
```

Clicking `[⧉]` copies that command to your clipboard and shows a macOS notification.

## How it works

1. **ZSH hooks** (`preexec`/`precmd`) intercept command output transparently — no prefix or wrapper needed
2. A **stream filter** processes output line-by-line, detects command patterns, and appends a clickable `[⧉]` using OSC 8 hyperlinks
3. Each `[⧉]` links to a `copycmd://` URL containing the base64-encoded command
4. A lightweight **macOS URL scheme handler** (AppleScript app) receives the click, decodes the command, copies it to your clipboard, and shows a notification

## Requirements

- macOS (for the URL scheme handler — the ZSH plugin itself is cross-platform)
- ZSH shell
- A terminal with OSC 8 hyperlink support:
  - **Ghostty** ✓
  - **iTerm2** ✓ (3.1+)
  - **Kitty** ✓
  - **WezTerm** ✓
  - **Alacritty** ✓ (0.11+)
  - **macOS Terminal.app** ✗ (buttons show visually but are not clickable)

## Installation

```bash
git clone https://github.com/shyamkhakharia/copycmd.git
cd copycmd
./install.sh
```

The installer will:

1. Copy the plugin to `~/.copycmd/`
2. Build and register the `CopyCmd.app` URL scheme handler
3. Add `source ~/.copycmd/copycmd.plugin.zsh` to your `~/.zshrc`

Then restart your shell or run:

```bash
source ~/.copycmd/copycmd.plugin.zsh
```

## Usage

**There's nothing to do.** Just use your terminal normally. Commands detected in output will automatically have a `[⧉]` button next to them.

### What it detects

- **Package managers**: `npm install`, `yarn add`, `pip install`, `brew install`, `cargo install`, `apt-get install`, etc.
- **Git**: `git clone`, `git checkout`, `git push`, `git pull`, `git stash`, etc.
- **CLI tools**: `curl`, `wget`, `docker run`, `kubectl`, `terraform`, `rsync`, `scp`, etc.
- **Shell operations**: `export`, `source`, `sudo`, `mkdir -p`, `chmod`, etc.
- **Backtick-wrapped commands**: `` `brew install node` `` in output text

### Helper commands

| Command | Description |
| --- | --- |
| `cc-toggle` | Enable/disable copycmd |

### Skipped commands

Interactive/TUI programs are automatically skipped to avoid breaking their display:

`vim`, `nvim`, `nano`, `less`, `top`, `htop`, `tmux`, `ssh`, `fzf`, `python`, `node`, `mysql`, `psql`, etc.

## Configuration

Set these in your `.zshrc` before sourcing the plugin:

```bash
# Disable copycmd on load (default: true)
COPYCMD_ENABLED=false

# Add commands to the skip list
COPYCMD_SKIP_COMMANDS+=(my-tui-app another-interactive-tool)
```

### Adding custom patterns

Edit the `COPYCMD_PATTERNS` array in `copycmd.plugin.zsh` to add your own detection patterns. Patterns use ZSH extended regex:

```bash
COPYCMD_PATTERNS+=(
  'my-cli-tool[[:space:]].+'
  'custom-command[[:space:]].+'
)
```

## How the URL handler works

The installer builds a minimal macOS app (`~/.copycmd/CopyCmd.app`) using AppleScript that:

1. Registers the `copycmd://` URL scheme with macOS Launch Services
2. When a `copycmd://` URL is opened (by clicking `[⧉]` in the terminal), it decodes the base64 payload
3. Copies the decoded command to your clipboard
4. Shows a macOS notification confirming the copy

The app runs as a background-only process (no Dock icon).

## Uninstalling

```bash
# Remove from .zshrc (delete the copycmd lines)
# Remove installed files
rm -rf ~/.copycmd
```

## License

MIT
