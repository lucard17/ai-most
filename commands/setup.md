---
description: Set up or check the l-most chat bridge on this machine
argument-hint: "[--check]"
---
Set up the l-most bridge between this machine's sessions and Mattermost, then report what is still missing.

1. Find the command: `lmost` if it is on PATH, otherwise the `bin/lmost` script of the l-most plugin directory
   (the directory that holds this plugin's `.claude-plugin/plugin.json`).
2. Run `lmost setup $ARGUMENTS` and show its output as is.
3. For every line starting with `✗` or `!`, say in one sentence what the person has to do. Connection values and bot
   tokens are personal: never ask for them in the chat and never write them anywhere except the files the output names
   (`~/.config/lmost/connections/<name>.env`, `~/.config/lmost/bots/<connection>/<role>`, mode 0600). A bot token is taken
   with the chat command `/bot-token <bot>`.
4. Do not run `sudo`, do not install packages, do not edit systemd units by hand.
