#!/usr/bin/env python3
"""
copycmd-proxy — PTY proxy that detects commands in terminal output
and injects clickable copy buttons (OSC 8 hyperlinks).

Sits between the terminal emulator and the shell. All programs see
a real TTY, so nothing breaks — not even TUI apps like claude.

Usage:
  copycmd-proxy              # runs your default $SHELL
  copycmd-proxy /bin/bash     # runs a specific shell

Configure Ghostty to use it:
  command = /path/to/copycmd-proxy
"""

import base64
import fcntl
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import tty

# ── Command patterns to detect ─────────────────────────────────────
PATTERNS = [
    # Package managers
    r'npm install\b.*', r'npm i\s+\S.*', r'npm cache\b.*', r'npm run\b.*',
    r'npm ci\b', r'npx\s+\S.*',
    r'yarn add\b.*', r'yarn install\b.*',
    r'pnpm add\b.*', r'pnpm install\b.*',
    r'pip3? install\b.*',
    r'brew install\b.*', r'brew tap\b.*', r'brew upgrade\b.*',
    r'apt(?:-get)? install\b.*',
    r'cargo install\b.*', r'cargo add\b.*',
    r'gem install\b.*',
    r'go install\b.*', r'go get\b.*',

    # Git
    r'git clone\b.*', r'git checkout\b.*', r'git switch\b.*',
    r'git pull\b.*', r'git push\b.*', r'git merge\b.*',
    r'git rebase\b.*', r'git reset\b.*', r'git stash\b.*',
    r'git cherry-pick\b.*', r'git remote add\b.*',
    r'git submodule\b.*', r'git fetch\b.*',

    # CLI tools
    r'curl\s+-.*', r'curl\s+https?://\S+',
    r'wget\b.*',
    r'scp\b.*', r'rsync\b.*',
    r'docker run\b.*', r'docker compose\b.*',
    r'docker pull\b.*', r'docker build\b.*',
    r'kubectl\b.*', r'terraform\b.*',
    r'chmod\b.*', r'chown\b.*',
    r'ln -s\b.*', r'make\s+\S.*',

    # Shell
    r'export\s+\S.*', r'source\s+\S.*',
    r'mkdir -p\b.*', r'sudo\s+\S.*',
    r'cd\s+\S.*',
]

COMPILED_PATTERNS = [re.compile(p) for p in PATTERNS]

# Backtick pattern: `command here`
BACKTICK_RE = re.compile(r'`([^`]{3,})`')
BACKTICK_CMD_WORDS = {
    'npm', 'yarn', 'pnpm', 'pip', 'pip3', 'brew', 'apt', 'cargo', 'gem',
    'go', 'git', 'curl', 'wget', 'ssh', 'scp', 'rsync', 'docker',
    'kubectl', 'terraform', 'chmod', 'chown', 'mkdir', 'sudo', 'export',
    'source', 'ln', 'cd', 'make', 'npx', 'bunx',
}

# Strip ANSI SGR (color/style) sequences for command extraction
SGR_RE = re.compile(r'\x1b\[\d*(?:;\d+)*m')

# ── TUI Detection (Layer 2: escape sequence scoring) ───────────────
CSI_CURSOR_POS = re.compile(rb'\x1b\[\d+;\d+[Hf]')
CSI_CURSOR_REL = re.compile(rb'\x1b\[\d*[ABCD]')
CSI_LINE_CLEAR = re.compile(rb'\x1b\[\d*K')
CSI_SCREEN_CLEAR = re.compile(rb'\x1b\[2J')
CSI_CURSOR_VIS = re.compile(rb'\x1b\[\?25[hl]')
CSI_LINE_MANIP = re.compile(rb'\x1b\[\d*[LM]')
CSI_SCROLL_REGION = re.compile(rb'\x1b\[\d+;\d+r')
PROMPT_PATTERN = re.compile(rb'[\$%#>] \s*$', re.MULTILINE)

# macOS ioctl to get foreground process group
TIOCGPGRP = 0x40047477


# ── Proxy State ────────────────────────────────────────────────────

class ProxyState:
    """Tracks TUI detection state across output chunks."""

    KNOWN_TUI_PROCESSES = {
        # Editors
        'vim', 'nvim', 'vi', 'nano', 'emacs', 'micro', 'helix', 'joe', 'ne',
        'code',
        # Pagers
        'less', 'more', 'man', 'bat',
        # Monitors
        'top', 'htop', 'btop', 'nmon', 'watch',
        # Multiplexers
        'tmux', 'screen', 'zellij', 'byobu',
        # Fuzzy finders
        'fzf',
        # Remote
        'ssh', 'mosh', 'telnet',
        # REPLs
        'python3', 'python', 'python2', 'node', 'irb', 'ruby', 'lua',
        'ghci', 'iex', 'erl', 'scala',
        # Shells (when run as subshell)
        'bash', 'zsh', 'fish', 'sh',
        # Databases
        'mysql', 'psql', 'sqlite3', 'redis-cli', 'mongosh',
        # Git TUIs
        'tig', 'lazygit', 'lazydocker',
        # AI tools
        'claude', 'aider', 'cursor',
        # Other
        'su',
    }

    # Process names to check args for (they're generic runtimes)
    RUNTIME_PROCESSES = {'node', 'python3', 'python', 'ruby'}

    # Keywords in args that indicate a TUI
    TUI_ARG_KEYWORDS = {'claude', 'aider', 'cursor', 'ipython', 'bpython'}

    def __init__(self, master_fd, child_pid):
        self.master_fd = master_fd
        self.child_pid = child_pid
        self.in_alt_screen = False
        self.tui_score = 0.0
        self.last_output_time = time.monotonic()
        self.fg_cache_time = 0.0
        self.fg_cache_result = None
        # Track the shell's own PID so we don't skip it
        self.shell_pid = child_pid

    def get_foreground_process(self):
        """Get the name of the foreground process in the PTY."""
        now = time.monotonic()
        # Cache for 500ms
        if now - self.fg_cache_time < 0.5:
            return self.fg_cache_result

        try:
            # Get foreground process group from PTY master
            buf = struct.pack('i', 0)
            result = fcntl.ioctl(self.master_fd, TIOCGPGRP, buf)
            pgrp = struct.unpack('i', result)[0]

            # If it's the shell itself, not a TUI
            if pgrp == self.shell_pid:
                self.fg_cache_time = now
                self.fg_cache_result = None
                return None

            # Get process name
            out = subprocess.check_output(
                ['ps', '-o', 'comm=', '-p', str(pgrp)],
                timeout=0.1, stderr=subprocess.DEVNULL
            ).decode().strip()
            name = os.path.basename(out)

            # For runtime processes (node, python), check the full args
            if name in self.RUNTIME_PROCESSES:
                try:
                    args = subprocess.check_output(
                        ['ps', '-o', 'args=', '-p', str(pgrp)],
                        timeout=0.1, stderr=subprocess.DEVNULL
                    ).decode().strip().lower()
                    for kw in self.TUI_ARG_KEYWORDS:
                        if kw in args:
                            name = kw
                            break
                except Exception:
                    pass

            self.fg_cache_time = now
            self.fg_cache_result = name
            return name
        except Exception:
            self.fg_cache_time = now
            self.fg_cache_result = None
            return None

    def update_score(self, data):
        """Update TUI probability score based on escape sequences in data."""
        now = time.monotonic()
        elapsed = now - self.last_output_time
        self.last_output_time = now

        # Time-based decay (when output pauses, score drops)
        if elapsed > 0.05:
            decay_steps = min(elapsed / 0.1, 20)
            self.tui_score *= (0.85 ** decay_steps)

        # Count TUI-indicative sequences
        pos_count = len(CSI_CURSOR_POS.findall(data))
        rel_count = len(CSI_CURSOR_REL.findall(data))
        clear_count = len(CSI_LINE_CLEAR.findall(data))

        self.tui_score += pos_count * 0.4
        self.tui_score += rel_count * 0.1
        self.tui_score += clear_count * 0.2
        self.tui_score += len(CSI_SCREEN_CLEAR.findall(data)) * 0.5
        self.tui_score += len(CSI_CURSOR_VIS.findall(data)) * 0.3
        self.tui_score += len(CSI_LINE_MANIP.findall(data)) * 0.3
        self.tui_score += len(CSI_SCROLL_REGION.findall(data)) * 0.5

        # Decay signals: plain text output
        if b'\n' in data and pos_count == 0 and rel_count == 0:
            self.tui_score -= 0.3

        # Shell prompt is a strong signal we're back to plain text
        if PROMPT_PATTERN.search(data):
            self.tui_score -= 0.5

        # Clamp
        self.tui_score = max(0.0, min(1.0, self.tui_score))

    def is_tui_active(self, data):
        """Three-layer TUI detection. Returns True if output should pass through."""

        # Layer 3: Alt-screen buffer (fast path, definitive)
        if b'\x1b[?1049h' in data or b'\x1b[?47h' in data:
            self.in_alt_screen = True
        if b'\x1b[?1049l' in data or b'\x1b[?47l' in data:
            self.in_alt_screen = False
            self.tui_score = 0.0  # Reset score on alt-screen exit

        if self.in_alt_screen:
            return True

        # Layer 1: Known foreground process
        fg = self.get_foreground_process()
        if fg and fg in self.KNOWN_TUI_PROCESSES:
            return True

        # Layer 2: Escape sequence density scoring
        self.update_score(data)
        if self.tui_score > 0.5:
            return True

        return False


# ── Core Functions ─────────────────────────────────────────────────

def make_copy_button(cmd):
    """Generate an OSC 8 hyperlink copy button for a command."""
    encoded = base64.b64encode(cmd.encode()).decode()
    return '  \x1b]8;;copycmd://{}\x07\x1b[48;5;238;38;5;117m copy \x1b[0m\x1b]8;;\x07'.format(encoded)


def extract_command(line):
    """Extract a copyable command from a line of text, if any."""
    # Strip ANSI color codes first
    line_clean = SGR_RE.sub('', line)

    stripped = line_clean.strip()
    for prefix in ('$ ', '> ', '% '):
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):]

    # Strip backticks
    clean = stripped.replace('`', '')

    for pattern in COMPILED_PATTERNS:
        m = pattern.search(clean)
        if m:
            cmd = m.group(0)
            # Clean trailing noise
            for suffix in ('.', ','):
                cmd = cmd.rstrip(suffix)
            for word in (' if ', ' and ', ' or ', ' then ', ' to '):
                idx = cmd.find(word)
                if idx > 0:
                    cmd = cmd[:idx]
            return cmd.strip()

    # Check backtick-wrapped commands in original line
    for m in BACKTICK_RE.finditer(line_clean):
        inner = m.group(1)
        first_word = inner.split()[0] if inner.split() else ''
        if first_word in BACKTICK_CMD_WORDS:
            return inner

    return None


def process_output(data, state):
    """Process terminal output, injecting copy buttons where appropriate."""

    # Check all three TUI detection layers
    if state.is_tui_active(data):
        return data

    try:
        text = data.decode('utf-8', errors='replace')
    except Exception:
        return data

    # Only process if there are complete lines
    if '\n' not in text:
        return data

    lines = text.split('\n')
    result_lines = []

    for i, line in enumerate(lines):
        # Don't process the last element if it's incomplete (no trailing \n)
        if i == len(lines) - 1 and not text.endswith('\n'):
            result_lines.append(line)
            continue

        # Skip empty lines
        if not line.strip():
            result_lines.append(line)
            continue

        cmd = extract_command(line)
        if cmd:
            result_lines.append(line + make_copy_button(cmd))
        else:
            result_lines.append(line)

    return '\n'.join(result_lines).encode('utf-8', errors='replace')


# ── PTY Proxy ──────────────────────────────────────────────────────

def set_winsize(fd, rows, cols):
    """Set the window size of a PTY."""
    winsize = struct.pack('HHHH', rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def get_winsize(fd):
    """Get the window size of a terminal."""
    try:
        winsize = fcntl.ioctl(fd, termios.TIOCGWINSZ, b'\x00' * 8)
        rows, cols = struct.unpack('HHHH', winsize)[:2]
        return rows, cols
    except Exception:
        return 24, 80


def main():
    shell = sys.argv[1] if len(sys.argv) > 1 else os.environ.get('SHELL', '/bin/zsh')

    # Get current terminal size
    rows, cols = get_winsize(sys.stdin.fileno())

    # Create PTY
    master_fd, slave_fd = pty.openpty()

    # Set slave PTY size to match terminal
    set_winsize(slave_fd, rows, cols)

    # Fork
    pid = os.fork()

    if pid == 0:
        # Child: run the shell in the slave PTY
        os.close(master_fd)
        os.setsid()

        # Set controlling terminal
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

        # Redirect stdin/stdout/stderr to slave PTY
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)

        if slave_fd > 2:
            os.close(slave_fd)

        os.execvp(shell, [shell])
        sys.exit(1)

    # Parent: proxy between terminal and master PTY
    os.close(slave_fd)

    # Save and set terminal to raw mode
    try:
        old_attrs = termios.tcgetattr(sys.stdin.fileno())
        tty.setraw(sys.stdin.fileno())
    except termios.error:
        print("copycmd-proxy: must be run inside a terminal", file=sys.stderr)
        os.kill(pid, signal.SIGTERM)
        os.waitpid(pid, 0)
        sys.exit(1)

    # Handle window resize
    def handle_sigwinch(signum, frame):
        rows, cols = get_winsize(sys.stdin.fileno())
        set_winsize(master_fd, rows, cols)
        os.kill(pid, signal.SIGWINCH)

    signal.signal(signal.SIGWINCH, handle_sigwinch)

    state = ProxyState(master_fd, pid)

    try:
        while True:
            try:
                rlist, _, _ = select.select(
                    [sys.stdin.fileno(), master_fd], [], [], 0.1
                )
            except (select.error, InterruptedError):
                continue

            if sys.stdin.fileno() in rlist:
                try:
                    data = os.read(sys.stdin.fileno(), 4096)
                except OSError:
                    break
                if not data:
                    break
                os.write(master_fd, data)

            if master_fd in rlist:
                try:
                    data = os.read(master_fd, 16384)
                except OSError:
                    break
                if not data:
                    break

                processed = process_output(data, state)
                os.write(sys.stdout.fileno(), processed)

    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSAFLUSH, old_attrs)
        os.close(master_fd)

        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass


if __name__ == '__main__':
    main()
