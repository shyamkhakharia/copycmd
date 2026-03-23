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
import sys
import termios
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

# CSI escape sequence pattern — indicates TUI activity
# If a chunk has cursor movement, don't process it
TUI_INDICATORS = re.compile(
    rb'\x1b\[\d*[ABCDHJ]'   # cursor up/down/forward/back, erase
    rb'|\x1b\[\d*;\d*[Hf]'  # cursor position
    rb'|\x1b\[\?(?:1049|47|1047)[hl]'  # alt screen buffer
    rb'|\x1b\[2J'            # clear screen
)


def make_copy_button(cmd: str) -> str:
    """Generate an OSC 8 hyperlink copy button for a command."""
    encoded = base64.b64encode(cmd.encode()).decode()
    return f'  \x1b]8;;copycmd://{encoded}\x07\x1b[48;5;238;38;5;117m copy \x1b[0m\x1b]8;;\x07'


def extract_command(line: str) -> str | None:
    """Extract a copyable command from a line of text, if any."""
    # Strip leading whitespace and common prompt prefixes
    stripped = line.strip()
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
    for m in BACKTICK_RE.finditer(line):
        inner = m.group(1)
        first_word = inner.split()[0] if inner.split() else ''
        if first_word in BACKTICK_CMD_WORDS:
            return inner

    return None


def process_output(data: bytes, in_tui: list) -> bytes:
    """Process terminal output, injecting copy buttons where appropriate."""

    # Detect TUI mode (alt screen buffer)
    if b'\x1b[?1049h' in data or b'\x1b[?47h' in data:
        in_tui[0] = True
    if b'\x1b[?1049l' in data or b'\x1b[?47l' in data:
        in_tui[0] = False

    # Don't process in TUI mode
    if in_tui[0]:
        return data

    # Don't process chunks with cursor movement (partial TUI rendering)
    if TUI_INDICATORS.search(data):
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
        # Forward SIGWINCH to child
        os.kill(pid, signal.SIGWINCH)

    signal.signal(signal.SIGWINCH, handle_sigwinch)

    in_tui = [False]  # mutable flag for TUI mode tracking

    try:
        while True:
            try:
                rlist, _, _ = select.select([sys.stdin.fileno(), master_fd], [], [], 0.1)
            except (select.error, InterruptedError):
                continue

            if sys.stdin.fileno() in rlist:
                # Input from terminal → send to shell
                try:
                    data = os.read(sys.stdin.fileno(), 4096)
                except OSError:
                    break
                if not data:
                    break
                os.write(master_fd, data)

            if master_fd in rlist:
                # Output from shell → process and send to terminal
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break

                processed = process_output(data, in_tui)
                os.write(sys.stdout.fileno(), processed)

    except KeyboardInterrupt:
        pass
    finally:
        # Restore terminal
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSAFLUSH, old_attrs)
        os.close(master_fd)

        # Wait for child
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass


if __name__ == '__main__':
    main()
