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

# ── TUI Detection patterns ────────────────────────────────────────
# Any non-SGR CSI escape sequence indicates TUI-like output.
# SGR = \e[...m (colors/styles) — these are fine in plain text.
# Everything else (cursor movement, clearing, scrolling) = TUI.
#
# This regex matches CSI sequences that are NOT SGR:
#   - Cursor position: \e[H, \e[;H, \e[n;mH, \e[n;mf
#   - Cursor movement: \e[nA, \e[nB, \e[nC, \e[nD
#   - Erase: \e[nJ, \e[nK
#   - Scroll: \e[nS, \e[nT
#   - Line ops: \e[nL, \e[nM
#   - Cursor show/hide: \e[?25h, \e[?25l
#   - Alt screen: \e[?1049h, \e[?1049l, \e[?47h, \e[?47l
#   - Scroll region: \e[n;nr
#   - Any CSI ending in a letter that's not 'm'
CSI_NON_SGR = re.compile(rb'\x1b\[\??[\d;]*[A-LN-Za-ln-z]')

# macOS ioctl to get foreground process group
TIOCGPGRP = 0x40047477

# How long to buffer output when a new foreground process starts (seconds)
BUFFER_PEEK_TIME = 0.08


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
        # Databases
        'mysql', 'psql', 'sqlite3', 'redis-cli', 'mongosh',
        # Git TUIs
        'tig', 'lazygit', 'lazydocker',
        # AI tools
        'claude', 'aider', 'cursor',
        # Other
        'su',
    }

    # Runtime process names — check their args for TUI keywords
    RUNTIME_PROCESSES = {'node', 'python3', 'python', 'ruby'}
    TUI_ARG_KEYWORDS = {'claude', 'aider', 'cursor', 'ipython', 'bpython'}

    def __init__(self, master_fd, child_pid):
        self.master_fd = master_fd
        self.child_pid = child_pid
        self.in_alt_screen = False
        self.last_fg_pgrp = child_pid  # Track foreground process group changes
        self.fg_cache_time = 0.0
        self.fg_cache_result = None
        self.fg_cache_pgrp = 0

        # Buffering state: when a new foreground process starts,
        # we buffer output briefly to peek at whether it's a TUI
        self.buffering = False
        self.buffer_data = b''
        self.buffer_start = 0.0
        self.buffer_decided_tui = None  # None = undecided, True/False = decided

        # Once we decide a foreground process is TUI, remember it
        self.tui_pgrps = set()

    def _get_fg_pgrp(self):
        """Get the foreground process group ID from the PTY."""
        try:
            buf = struct.pack('i', 0)
            result = fcntl.ioctl(self.master_fd, TIOCGPGRP, buf)
            return struct.unpack('i', result)[0]
        except Exception:
            return self.child_pid

    def _get_process_name(self, pgrp):
        """Get the process name for a process group leader."""
        now = time.monotonic()
        if pgrp == self.fg_cache_pgrp and now - self.fg_cache_time < 1.0:
            return self.fg_cache_result

        try:
            out = subprocess.check_output(
                ['ps', '-o', 'comm=', '-p', str(pgrp)],
                timeout=0.1, stderr=subprocess.DEVNULL
            ).decode().strip()
            name = os.path.basename(out)

            # For runtime processes, check args
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
            self.fg_cache_pgrp = pgrp
            self.fg_cache_result = name
            return name
        except Exception:
            self.fg_cache_time = now
            self.fg_cache_pgrp = pgrp
            self.fg_cache_result = None
            return None

    def _data_looks_like_tui(self, data):
        """Check if raw data contains non-SGR CSI sequences (TUI indicator)."""
        return bool(CSI_NON_SGR.search(data))

    def _is_shell_prompt(self, pgrp):
        """Check if the foreground is the shell itself (not a child program)."""
        return pgrp == self.child_pid

    def process(self, data):
        """
        Main entry point. Takes raw output data, returns what should be
        written to the terminal. Handles TUI detection, buffering, and
        command button injection.
        """
        # Alt-screen: fast definitive toggle
        if b'\x1b[?1049h' in data or b'\x1b[?47h' in data:
            self.in_alt_screen = True
        if b'\x1b[?1049l' in data or b'\x1b[?47l' in data:
            self.in_alt_screen = False

        if self.in_alt_screen:
            return data

        # Check current foreground process group
        pgrp = self._get_fg_pgrp()

        # If it's the shell itself, process normally (no TUI)
        if self._is_shell_prompt(pgrp):
            # If we were buffering, flush as plain text
            if self.buffering:
                self.buffering = False
                buffered = self.buffer_data
                self.buffer_data = b''
                return _inject_buttons(buffered) + _inject_buttons(data)
            # Forget TUI pgrps when back to shell
            self.tui_pgrps.clear()
            return _inject_buttons(data)

        # A child process is in the foreground
        # If we already know this pgrp is a TUI, pass through
        if pgrp in self.tui_pgrps:
            return data

        # New foreground process detected — check by name first
        if pgrp != self.last_fg_pgrp:
            self.last_fg_pgrp = pgrp
            name = self._get_process_name(pgrp)
            if name and name in self.KNOWN_TUI_PROCESSES:
                self.tui_pgrps.add(pgrp)
                return data

            # Unknown process — start buffering to peek at output
            self.buffering = True
            self.buffer_data = data
            self.buffer_start = time.monotonic()
            self.buffer_decided_tui = None
            return b''  # Hold output while we peek

        # If we're currently buffering (peeking at a new process)
        if self.buffering:
            self.buffer_data += data
            elapsed = time.monotonic() - self.buffer_start

            # Check if accumulated data looks like TUI
            if self._data_looks_like_tui(self.buffer_data):
                # It's a TUI — pass through all buffered data raw
                self.buffering = False
                self.tui_pgrps.add(pgrp)
                result = self.buffer_data
                self.buffer_data = b''
                return result

            # If we've waited long enough, decide it's plain text
            if elapsed >= BUFFER_PEEK_TIME:
                self.buffering = False
                result = _inject_buttons(self.buffer_data)
                self.buffer_data = b''
                return result

            # Still waiting — hold the data
            return b''

        # Ongoing output from a non-TUI child process
        # Do a quick check — if this chunk has TUI sequences, switch
        if self._data_looks_like_tui(data):
            self.tui_pgrps.add(pgrp)
            return data

        return _inject_buttons(data)


def _inject_buttons(data):
    """Process raw bytes, injecting copy buttons on lines with commands."""
    try:
        text = data.decode('utf-8', errors='replace')
    except Exception:
        return data

    if '\n' not in text:
        return data

    lines = text.split('\n')
    result_lines = []

    for i, line in enumerate(lines):
        # Don't process the last element if incomplete (no trailing \n)
        if i == len(lines) - 1 and not text.endswith('\n'):
            result_lines.append(line)
            continue

        if not line.strip():
            result_lines.append(line)
            continue

        cmd = extract_command(line)
        if cmd:
            result_lines.append(line + make_copy_button(cmd))
        else:
            result_lines.append(line)

    return '\n'.join(result_lines).encode('utf-8', errors='replace')


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

    clean = stripped.replace('`', '')

    for pattern in COMPILED_PATTERNS:
        m = pattern.search(clean)
        if m:
            cmd = m.group(0)
            for suffix in ('.', ','):
                cmd = cmd.rstrip(suffix)
            for word in (' if ', ' and ', ' or ', ' then ', ' to '):
                idx = cmd.find(word)
                if idx > 0:
                    cmd = cmd[:idx]
            return cmd.strip()

    for m in BACKTICK_RE.finditer(line_clean):
        inner = m.group(1)
        first_word = inner.split()[0] if inner.split() else ''
        if first_word in BACKTICK_CMD_WORDS:
            return inner

    return None


# ── PTY Proxy ──────────────────────────────────────────────────────

def set_winsize(fd, rows, cols):
    winsize = struct.pack('HHHH', rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def get_winsize(fd):
    try:
        winsize = fcntl.ioctl(fd, termios.TIOCGWINSZ, b'\x00' * 8)
        rows, cols = struct.unpack('HHHH', winsize)[:2]
        return rows, cols
    except Exception:
        return 24, 80


def main():
    shell = sys.argv[1] if len(sys.argv) > 1 else os.environ.get('SHELL', '/bin/zsh')

    rows, cols = get_winsize(sys.stdin.fileno())
    master_fd, slave_fd = pty.openpty()
    set_winsize(slave_fd, rows, cols)

    pid = os.fork()

    if pid == 0:
        # Child: run the shell in the slave PTY
        os.close(master_fd)
        os.setsid()
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        if slave_fd > 2:
            os.close(slave_fd)
        os.execvp(shell, [shell])
        sys.exit(1)

    # Parent: proxy between terminal and master PTY
    os.close(slave_fd)

    try:
        old_attrs = termios.tcgetattr(sys.stdin.fileno())
        tty.setraw(sys.stdin.fileno())
    except termios.error:
        print("copycmd-proxy: must be run inside a terminal", file=sys.stderr)
        os.kill(pid, signal.SIGTERM)
        os.waitpid(pid, 0)
        sys.exit(1)

    def handle_sigwinch(signum, frame):
        rows, cols = get_winsize(sys.stdin.fileno())
        set_winsize(master_fd, rows, cols)
        os.kill(pid, signal.SIGWINCH)

    signal.signal(signal.SIGWINCH, handle_sigwinch)

    state = ProxyState(master_fd, pid)

    try:
        while True:
            # If buffering, use a short timeout to flush promptly
            timeout = 0.02 if state.buffering else 0.1

            try:
                rlist, _, _ = select.select(
                    [sys.stdin.fileno(), master_fd], [], [], timeout
                )
            except (select.error, InterruptedError):
                continue

            # Flush buffer if peek time has elapsed
            if state.buffering and not rlist:
                elapsed = time.monotonic() - state.buffer_start
                if elapsed >= BUFFER_PEEK_TIME:
                    state.buffering = False
                    if state.buffer_data:
                        if state._data_looks_like_tui(state.buffer_data):
                            pgrp = state._get_fg_pgrp()
                            state.tui_pgrps.add(pgrp)
                            os.write(sys.stdout.fileno(), state.buffer_data)
                        else:
                            os.write(sys.stdout.fileno(),
                                     _inject_buttons(state.buffer_data))
                        state.buffer_data = b''

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

                output = state.process(data)
                if output:
                    os.write(sys.stdout.fileno(), output)

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
