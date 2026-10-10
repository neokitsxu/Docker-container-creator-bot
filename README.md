# Docker VPS Maker Discord Bot

Discord bot for creating and managing Docker-based Ubuntu containers through an authorized Docker host.

## Features
- `?create <hostname> <ram> <cpu> <disk> @user <os>` (admin only)
- `?manage` shows the caller's VPS containers with buttons: Start, Stop, Restart, Live Stats, SSHX, Reinstall, Delete, management menu
- Sends the created user a DM with container details and management guidance
- `?admin-add @user` and `?admin-remove @user`
- `?help`, `?os`, `?list`, `?status`, `?start`, `?stop`, `?restart`, `?stats`, `?sshx`, `?reinstall`, `?delete`, `?rename`, `?limits`, `?admins`, `?ping`, `?uptime`
- 12 OS/template choices (Ubuntu/Debian/Alpine/Arch/Fedora/Rocky/AlmaLinux/ openSUSE)
- `.env` config for bot token, prefix, admin IDs, Docker host, and limits

## Important safety setup
Run this bot only on a Docker host you own or are authorized to administer. Docker access is effectively host-root access. The bot enforces configured admin IDs and resource limits, but Discord permissions are not a substitute for securing the host. Keep the bot token and Docker socket private.

The bot creates **unprivileged containers** and does not mount the host filesystem or Docker socket into user containers. SSHX is installed automatically inside the selected running container on first use using the official sshx.io installer; the session link is sent to the owner by DM. Anyone with the link may access the shared terminal, so keep it private. Reinstall replaces the container root filesystem and destroys data in that container; the bot asks for confirmation first.

## Install (Ubuntu 24.04 host)
1. Install Docker Engine and Python 3.11+.
2. Copy `.env.example` to `.env`, fill in values.
3. Add the bot to Discord with Message Content Intent enabled.
4. Install dependencies: `python3 -m pip install -r requirements.txt`
5. Start: `python3 bot.py`
6. For systemd, use `docker-vps-bot.service.example`.

## Command syntax
`?create <hostname> <ram> <cpu> <disk> @user <os>`

Example: `?create demo-vps 2G 2 20G @SomeUser ubuntu-24.04`

RAM accepts e.g. `512M`, `2G`, `4G`; CPU is a number of cores; disk accepts e.g. `10G`, `20G`, `50G`. The OS name must be one of `?os`.

The bot uses Docker's `--memory`, `--cpus`, and `--storage-opt size=...`. Note: disk quota support depends on the host filesystem/storage driver (commonly XFS with project quotas). If unsupported, creation will report the error rather than silently pretending a limit is enforced.

The SSHX feature installs the official SSHX client inside the selected container on demand; curl and a supported package manager may be installed first. If you prefer not to permit external session tooling, disable it with `SSHX_ENABLED=false`.
