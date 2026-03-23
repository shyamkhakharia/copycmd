# copycmd - Inline copy buttons for commands detected in terminal output
# Works with Ghostty, iTerm2, Kitty, and any terminal supporting OSC 8 hyperlinks

# ── Configuration ──────────────────────────────────────────────────
COPYCMD_ENABLED=${COPYCMD_ENABLED:-true}
COPYCMD_TMPDIR="${TMPDIR:-/tmp}/copycmd"
COPYCMD_BUTTON=" \033[90m[\033[0m\033[36m⧉\033[0m\033[90m]\033[0m"  # styled [⧉] button

# Commands that are interactive/TUI — skip capture for these
typeset -a COPYCMD_SKIP_COMMANDS=(
  # Editors
  vim nvim vi nano emacs code micro helix joe ne
  # Pagers
  less more man bat
  # System monitors
  top htop btop nmon watch
  # Multiplexers
  tmux screen zellij byobu
  # Fuzzy finders
  fzf
  # Remote access
  ssh mosh telnet
  # REPLs & interpreters
  python3 python python2 node irb ruby lua ghci iex erl scala
  bash zsh fish sh
  # Databases
  mysql psql sqlite3 redis-cli mongosh
  # Git TUIs
  tig lazygit lazydocker
  # AI tools
  claude aider cursor
  # Shells
  su
)

mkdir -p "$COPYCMD_TMPDIR"

# ── Command patterns to detect ─────────────────────────────────────
typeset -a COPYCMD_PATTERNS=(
  # Package managers
  'npm install.*'
  'npm i[[:space:]].+'
  'npm cache[[:space:]].+'
  'npm run[[:space:]].+'
  'npm ci'
  'npx[[:space:]].+'
  'yarn add[[:space:]].+'
  'yarn install.*'
  'pnpm add[[:space:]].+'
  'pnpm install.*'
  'pip install[[:space:]].+'
  'pip3 install[[:space:]].+'
  'brew install[[:space:]].+'
  'brew tap[[:space:]].+'
  'brew upgrade[[:space:]].+'
  'brew uninstall[[:space:]].+'
  'apt install[[:space:]].+'
  'apt-get install[[:space:]].+'
  'cargo install[[:space:]].+'
  'cargo add[[:space:]].+'
  'gem install[[:space:]].+'
  'go install[[:space:]].+'
  'go get[[:space:]].+'

  # Git commands
  'git clone[[:space:]].+'
  'git checkout[[:space:]].+'
  'git switch[[:space:]].+'
  'git pull.*'
  'git push.*'
  'git merge[[:space:]].+'
  'git rebase[[:space:]].+'
  'git reset[[:space:]].+'
  'git stash.*'
  'git cherry-pick[[:space:]].+'
  'git remote add[[:space:]].+'
  'git submodule[[:space:]].+'
  'git fetch.*'

  # Common CLI tools
  'curl[[:space:]].+'
  'wget[[:space:]].+'
  'scp[[:space:]].+'
  'rsync[[:space:]].+'
  'docker run[[:space:]].+'
  'docker compose[[:space:]].+'
  'docker pull[[:space:]].+'
  'docker build[[:space:]].+'
  'kubectl[[:space:]].+'
  'terraform[[:space:]].+'
  'chmod[[:space:]].+'
  'chown[[:space:]].+'
  'ln -s[[:space:]].+'
  'make[[:space:]].+'

  # Shell operations
  'export[[:space:]].+'
  'source[[:space:]].+'
  'mkdir -p[[:space:]].+'
  'sudo[[:space:]].+'
  'cd[[:space:]].+'
)

# ── Core: Extract command from a line ──────────────────────────────

# Given a line of text, return the detected command (if any)
_copycmd_extract() {
  local line="$1"

  # Trim leading whitespace
  local trimmed="${line#"${line%%[![:space:]]*}"}"
  # Strip common prompt prefixes
  trimmed="${trimmed#\$ }"
  trimmed="${trimmed#> }"
  trimmed="${trimmed#% }"

  # Check for backtick-wrapped commands: `some command`
  local backtick_pattern='\`([^\`][^\`][^\`]+)\`'
  if [[ "$trimmed" =~ $backtick_pattern ]]; then
    local bt="${match[1]}"
    local cmd_word_pattern='^(npm|yarn|pnpm|pip|brew|apt|cargo|gem|go|git|curl|wget|ssh|scp|rsync|docker|kubectl|terraform|chmod|chown|mkdir|sudo|export|source|ln|cd|cat|echo|make|npx|bunx)'
    if [[ "$bt" =~ $cmd_word_pattern ]]; then
      echo "$bt"
      return 0
    fi
  fi

  # Strip backticks for pattern matching
  local clean="${trimmed//\`/}"

  for pattern in "${COPYCMD_PATTERNS[@]}"; do
    if [[ "$clean" =~ $pattern ]]; then
      local cmd="${MATCH}"
      # Clean up trailing noise
      cmd="${cmd%\.}"
      cmd="${cmd%,}"
      cmd="${cmd%[[:space:]]}"
      cmd="${cmd%%[[:space:]]if[[:space:]]*}"
      cmd="${cmd%%[[:space:]]and[[:space:]]*}"
      cmd="${cmd%%[[:space:]]or[[:space:]]*}"
      cmd="${cmd%%[[:space:]]then[[:space:]]*}"
      cmd="${cmd%%[[:space:]]to[[:space:]]*}"
      echo "$cmd"
      return 0
    fi
  done

  return 1
}

# Generate an OSC 8 clickable copy button for a command
_copycmd_button() {
  local cmd="$1"
  local encoded=$(echo -n "$cmd" | base64 | tr -d '\n')

  # OSC 8 hyperlink: \e]8;;URI\a VISIBLE_TEXT \e]8;;\a
  printf '\033]8;;copycmd://%s\a %s \033]8;;\a' "$encoded" "[⧉]"
}

# ── Stream Filter ──────────────────────────────────────────────────
# Reads stdin line by line, detects commands, appends clickable copy buttons

_copycmd_filter() {
  local _ccf_line _ccf_cmd _ccf_enc
  while IFS= read -r _ccf_line || [[ -n "$_ccf_line" ]]; do
    _ccf_cmd=$(_copycmd_extract "$_ccf_line" 2>/dev/null)
    if [[ -n "$_ccf_cmd" ]]; then
      _ccf_enc=$(printf '%s' "$_ccf_cmd" | base64 | tr -d '\n')
      printf '%s  \033]8;;copycmd://%s\a\033[48;5;238;38;5;117m copy \033[0m\033]8;;\a\n' "$_ccf_line" "$_ccf_enc"
    else
      printf '%s\n' "$_ccf_line"
    fi
  done
}

# ── Automatic Hook ─────────────────────────────────────────────────

autoload -Uz add-zsh-hook

typeset -g _copycmd_active=false

_copycmd_should_skip() {
  local cmd="$1"
  local base_cmd="${cmd##sudo }"
  base_cmd="${base_cmd##env }"
  base_cmd="${base_cmd%% *}"
  base_cmd="${base_cmd##*/}"

  for skip in "${COPYCMD_SKIP_COMMANDS[@]}"; do
    [[ "$base_cmd" == "$skip" ]] && return 0
  done

  # Skip pipes to interactive tools
  [[ "$cmd" == *"| less"* ]] && return 0
  [[ "$cmd" == *"| more"* ]] && return 0
  [[ "$cmd" == *"| fzf"* ]] && return 0
  [[ "$cmd" == *"| bat"* ]] && return 0

  # Skip long-running servers/watchers
  [[ "$cmd" == *"--watch"* ]] && return 0
  [[ "$cmd" == *"serve"* ]] && return 0
  [[ "$base_cmd" == "npm" && "$cmd" == *"start"* ]] && return 0
  [[ "$base_cmd" == "npm" && "$cmd" == *"dev"* ]] && return 0
  [[ "$base_cmd" == "yarn" && "$cmd" == *"start"* ]] && return 0
  [[ "$base_cmd" == "yarn" && "$cmd" == *"dev"* ]] && return 0

  return 1
}

_copycmd_preexec() {
  [[ "$COPYCMD_ENABLED" != "true" ]] && return

  local cmd="$1"

  # Don't capture interactive commands or our own helpers
  _copycmd_should_skip "$cmd" && return
  [[ "$cmd" == cc* ]] && return
  [[ "$cmd" == _copycmd_* ]] && return

  _copycmd_active=true

  # Redirect stdout through our filter
  exec {_copycmd_saved_fd}>&1
  exec 1> >(_copycmd_filter >&${_copycmd_saved_fd})
}

_copycmd_precmd() {
  [[ "$_copycmd_active" != "true" ]] && return
  _copycmd_active=false

  # Restore stdout
  if [[ -n "$_copycmd_saved_fd" ]] && [[ "$_copycmd_saved_fd" -gt 2 ]]; then
    exec 1>&${_copycmd_saved_fd}
    exec {_copycmd_saved_fd}>&-
    unset _copycmd_saved_fd
  fi
}

add-zsh-hook preexec _copycmd_preexec
add-zsh-hook precmd _copycmd_precmd

# ── User Commands ──────────────────────────────────────────────────

cc-toggle() {
  if [[ "$COPYCMD_ENABLED" == "true" ]]; then
    COPYCMD_ENABLED=false
    echo "\033[33mcopycmd disabled\033[0m"
  else
    COPYCMD_ENABLED=true
    echo "\033[32mcopycmd enabled\033[0m"
  fi
}
