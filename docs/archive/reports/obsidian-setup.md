# Obsidian Vault — SSH-Mounted Setup

This is a single-user setup that points your local Obsidian client at `/root/openclaw/workspaces/default/` on the VPS over SSH. Read-write. Latency on file open is ~50-200 ms; full-text search and graph view are local once cached.

The seed `.obsidian/` config is already in place — Obsidian recognizes the folder as a vault on first open and applies our conventions (`[[wikilinks]] over markdown links, default new-note folder, Templates plugin pointing at `_templates/`, tag-coded graph colors).

## Prereqs

- Local: Obsidian (https://obsidian.md), latest stable
- Local: an SSH key already authorized on the VPS for `root@<vps-host>` (you've been using it)
- VPS: workspace lives at `/root/openclaw/workspaces/default/`

## Recommended: VS Code Remote SSH (zero-config, all platforms)

This is the simplest option — VS Code ssh-tunnels the whole filesystem; Obsidian just opens the folder via "Open Folder" pointed at a local path that VS Code has bridged.

Most users prefer one of the next two approaches; pick what matches your OS.

## macOS — sshfs via macFUSE

```bash
brew install --cask macfuse
brew install gromgit/fuse/sshfs-mac
mkdir -p ~/openclaw-vault
sshfs root@<vps-host>:/root/openclaw/workspaces/default \
    ~/openclaw-vault \
    -o defer_permissions,reconnect,follow_symlinks,kernel_cache,auto_cache,iosize=65536
```

Then in Obsidian: **Open folder as vault** → select `~/openclaw-vault`.

To unmount: `umount ~/openclaw-vault`.

To auto-mount on login: add the `sshfs` line to a `~/.config/launchd/openclaw-vault.plist` LaunchAgent, or just script it.

**Trade-off**: macFUSE requires kernel-extension approval (System Settings → Privacy & Security). One-time prompt.

## Linux — sshfs (native)

```bash
sudo apt install sshfs        # Debian/Ubuntu
sudo dnf install sshfs        # Fedora

mkdir -p ~/openclaw-vault
sshfs root@<vps-host>:/root/openclaw/workspaces/default \
    ~/openclaw-vault \
    -o reconnect,follow_symlinks,cache=yes,kernel_cache
```

Same Obsidian step. Add to `/etc/fstab` for auto-mount.

## Windows — WinFsp + SSHFS-Win

```powershell
# Install via winget
winget install -e --id WinFsp.WinFsp
winget install -e --id SSHFS-Win.SSHFS-Win

# Map a network drive
net use Z: \\sshfs\root@<vps-host>\openclaw\workspaces\default
```

Obsidian → **Open folder as vault** → `Z:\`.

## First-open checklist

1. Obsidian shows the vault. **Trust author** (you wrote this).
2. The 137-row pgvector index is already populated — agents already know your workspace. Nothing for you to import.
3. Open `WORKSPACE.md` first. That's the vault map.
4. **Settings → Community plugins → Browse** → install:
   - **Dataview** (must-have for tag-filtered tables)
   - **Templater** (better than core Templates; lets templates execute JS)
   - **Tag Wrangler** (rename tags safely)
   - **Frontmatter Title** (render note titles from frontmatter — the dated filenames are ugly)
5. **Settings → Files & Links** — confirm `Use Wikilinks: ON` and `Default location for new notes: results`.
6. **Cmd-G / Ctrl-G** to open graph view. You should see clusters tag-coded by note type.

## Smoke test

Try this Dataview query in any note (after installing the plugin):

````markdown
```dataview
TABLE strategy_id, status, date
FROM #strategy
SORT date DESC
LIMIT 10
```
````

If frontmatter backfill ran (commit a03f27d), you should see the 12 strategy memos with their `strategy_id` and `status: deployed` populated. If you see "no results", run `node bin/backfill-frontmatter.js` first.

## What happens when an agent writes a note

The system already does this:
- `memory-writer.js` appends to `memory/active_tasks.md` or `memory/fund_journal.md` → fires the embed-on-write hook → new chunks land in `memory_chunks` within ~30s.
- Agents using `fundjohn:obsidian-link` skill emit notes with proper frontmatter to `results/`.
- Mtime-aware idempotent backfill picks up changes; you don't need to do anything.

You'll just see new files appear in the file explorer. Click into them.

## Known friction

- **SSH disconnect** — sshfs reconnects in ~1s, but a long file save can fail mid-write. Save again.
- **Editing while an agent writes** — Obsidian's file-watcher will reload. Your in-flight unsaved edits live in Obsidian's editor; the on-disk content gets overwritten by the agent. Use `agent.md` for things you want preserved across agent runs.
- **Don't sync `.obsidian/` to git** — it has local cache state (workspace.json, recent files). The seed config in `src/workspace/template/_obsidian/` is the source of truth; copy from there to a fresh workspace if needed.

## Reverting

To wipe Obsidian config and start fresh:

```bash
rm -rf /root/openclaw/workspaces/default/.obsidian
cp -r /root/openclaw/src/workspace/template/_obsidian /root/openclaw/workspaces/default/.obsidian
```
