# Installed by court-onboard --dotfiles. Re-run onboard to refresh, edits
# kept in $HOME/.config/fish/config.fish.bak.<ts> on each install.

set -g fish_greeting ""

if status is-interactive
    if command -q starship
        starship init fish | source
    end
end
