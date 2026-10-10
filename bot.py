import os
import re
import asyncio
import time
from datetime import datetime, timezone

import discord
from discord.ext import commands
from dotenv import load_dotenv
import docker
from docker.errors import DockerException, NotFound, APIError

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "").strip()
PREFIX = os.getenv("PREFIX", "?")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
SSHX_ENABLED = os.getenv("SSHX_ENABLED", "true").lower() in ("1", "true", "yes")
MAX_RAM = os.getenv("MAX_RAM", "32G")
MAX_CPU = float(os.getenv("MAX_CPU", "8"))
MAX_DISK = os.getenv("MAX_DISK", "100G")

# Friendly labels and official base images. Availability depends on image architecture.
OS_IMAGES = {
    "ubuntu-24.04": ("Ubuntu 24.04", "ubuntu:24.04"),
    "ubuntu-22.04": ("Ubuntu 22.04", "ubuntu:22.04"),
    "ubuntu-20.04": ("Ubuntu 20.04", "ubuntu:20.04"),
    "debian-13": ("Debian 13", "debian:trixie"),
    "debian-12": ("Debian 12", "debian:bookworm"),
    "debian-11": ("Debian 11", "debian:bullseye"),
    "alpine-3.22": ("Alpine 3.22", "alpine:3.22"),
    "alpine-3.21": ("Alpine 3.21", "alpine:3.21"),
    "fedora-42": ("Fedora 42", "fedora:42"),
    "rocky-9": ("Rocky Linux 9", "rockylinux:9"),
    "almalinux-9": ("AlmaLinux 9", "almalinux:9"),
    "opensuse-leap": ("openSUSE Leap", "opensuse/leap:15.6"),
}
BOT_START = time.time()
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)
try:
    client = docker.from_env()
except DockerException as exc:
    client = None
    print(f"[ERROR] Cannot connect to Docker: {exc}")

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def human_bytes(value):
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(value)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"

def parse_size(value: str, max_value: str, label: str) -> str:
    value = value.strip().upper()
    match = re.fullmatch(r"([1-9]\d*)(M|G)", value)
    max_match = re.fullmatch(r"([1-9]\d*)(M|G)", max_value.strip().upper())
    if not match or not max_match:
        raise ValueError(f"{label} must look like 512M, 2G, or 20G.")
    n, unit = int(match.group(1)), match.group(2)
    max_n, max_unit = int(max_match.group(1)), max_match.group(2)
    mib = n if unit == "M" else n * 1024
    max_mib = max_n if max_unit == "M" else max_n * 1024
    if mib > max_mib:
        raise ValueError(f"{label} exceeds configured maximum ({max_value}).")
    if label == "RAM" and mib < 128:
        raise ValueError("RAM minimum is 128M.")
    if label == "Disk" and mib < 512:
        raise ValueError("Disk minimum is 512M.")
    return value

def container_owner(container):
    return str(container.labels.get("vps_owner", ""))

def owned_containers(user_id: int):
    if client is None:
        return []
    result = []
    for c in client.containers.list(all=True, filters={"label": f"vps_owner={user_id}"}):
        result.append(c)
    return result

def find_owned(container_id: str, user_id: int):
    if client is None:
        raise RuntimeError("Docker is unavailable.")
    try:
        c = client.containers.get(container_id)
    except NotFound:
        return None
    if not is_admin(user_id) and container_owner(c) != str(user_id):
        return None
    if c.labels.get("vps_managed") != "true":
        return None
    return c

def container_embed(c):
    try:
        c.reload()
        attrs = c.attrs
        state = attrs.get("State", {})
        config = attrs.get("Config", {})
        host = attrs.get("HostConfig", {})
        mem = host.get("Memory", 0)
        cpus = host.get("NanoCpus", 0) / 1e9 if host.get("NanoCpus") else host.get("CpuQuota", 0) / max(host.get("CpuPeriod", 100000), 1)
        embed = discord.Embed(title=f"🖥️ {c.name}", color=discord.Color.blurple())
        embed.add_field(name="Container ID", value=f"`{c.short_id}`", inline=False)
        embed.add_field(name="Status", value=state.get("Status", c.status).title(), inline=True)
        embed.add_field(name="OS", value=config.get("Image", "Unknown"), inline=True)
        embed.add_field(name="RAM Limit", value=human_bytes(mem) if mem else "Not set", inline=True)
        embed.add_field(name="CPU Limit", value=f"{cpus:g} cores" if cpus else "Not set", inline=True)
        embed.add_field(name="Uptime", value=state.get("StartedAt", "—")[:19].replace("T", " ") + " UTC" if state.get("Running") else "Not running", inline=False)
        embed.set_footer(text=f"Owner ID: {container_owner(c)}")
        return embed
    except Exception as e:
        return discord.Embed(title=c.name, description=f"Could not read details: {e}", color=discord.Color.red())

def start_sshx_session(c):
    """Install official sshx.io client inside a running container and start a session."""
    c.reload()
    if c.status != "running":
        raise RuntimeError("Start the container first.")
    script = r"""
set -eu
export PATH="/root/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
if ! command -v curl >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive; apt-get update -qq; apt-get install -y -qq curl ca-certificates
  elif command -v apk >/dev/null 2>&1; then apk add --no-cache curl ca-certificates
  elif command -v dnf >/dev/null 2>&1; then dnf install -y curl ca-certificates
  elif command -v microdnf >/dev/null 2>&1; then microdnf install curl ca-certificates
  elif command -v zypper >/dev/null 2>&1; then zypper --non-interactive refresh; zypper --non-interactive install curl ca-certificates
  else echo 'No supported package manager to install curl.' > /tmp/sshx-session.log; exit 20; fi
fi
if ! command -v sshx >/dev/null 2>&1 && [ ! -x /root/.local/bin/sshx ]; then curl -sSf https://sshx.io/get | sh; fi
SSHX_BIN="$(command -v sshx || true)"
[ -n "$SSHX_BIN" ] || [ ! -x /root/.local/bin/sshx ] || SSHX_BIN=/root/.local/bin/sshx
[ -n "$SSHX_BIN" ] || [ ! -x /usr/local/bin/sshx ] || SSHX_BIN=/usr/local/bin/sshx
if [ -z "$SSHX_BIN" ]; then echo 'SSHX binary not found after installation.' > /tmp/sshx-session.log; exit 21; fi
rm -f /tmp/sshx-session.log
nohup sh -lc 'export PATH="/root/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"; exec '"$SSHX_BIN"' ' </dev/null >/tmp/sshx-session.log 2>&1 &
echo SSHX_STARTED
"""
    result=c.exec_run(["sh","-lc",script], tty=True, demux=True)
    if result.exit_code not in (0, None):
        out=result.output
        if isinstance(out, tuple): out=b"".join(x or b"" for x in out)
        raise RuntimeError((out or b"SSHX install/launch failed").decode("utf-8","replace")[-600:])
    deadline=time.time()+25; last=""
    while time.time()<deadline:
        log=c.exec_run(["sh","-lc","cat /tmp/sshx-session.log 2>/dev/null || true"], demux=True)
        out=log.output
        if isinstance(out, tuple): out=b"".join(x or b"" for x in out)
        last=(out or b"").decode("utf-8","replace")
        match=re.search(r"https?://[^\\s\"'<>]+",last)
        if match: return match.group(0).rstrip(".,)")
        if "No supported package manager" in last or "binary not found" in last: break
        time.sleep(1)
    raise RuntimeError("No SSHX link appeared within 25 seconds. Output: "+(last[-400:] or "No output yet."))

class ReinstallConfirm(discord.ui.View):
    def __init__(self, owner_id, container_id):
        super().__init__(timeout=45)
        self.owner_id = owner_id
        self.container_id = container_id

    @discord.ui.button(label="Confirm reinstall", style=discord.ButtonStyle.danger, emoji="♻️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id and not is_admin(interaction.user.id):
            return await interaction.response.send_message("This confirmation isn't for you.", ephemeral=True)
        c = find_owned(self.container_id, interaction.user.id)
        if c is None:
            return await interaction.response.send_message("Container not found or access denied.", ephemeral=True)
        try:
            image = c.labels.get("vps_image", "ubuntu:24.04")
            c.stop(timeout=5)
            c.remove(force=True)
            newc = client.containers.run(
                image, ["sh", "-lc", "while :; do sleep 3600; done"],
                name=c.name, detach=True, tty=True, stdin_open=True,
                mem_limit=c.labels.get("vps_ram", "2G"),
                nano_cpus=int(float(c.labels.get("vps_cpu", "2")) * 1e9),
                labels={
                    "vps_managed": "true", "vps_owner": str(self.owner_id),
                    "vps_image": image, "vps_ram": c.labels.get("vps_ram", "2G"),
                    "vps_cpu": c.labels.get("vps_cpu", "2"),
                    "vps_disk": c.labels.get("vps_disk", "20G"),
                },
                restart_policy={"Name": "unless-stopped"},
            )
            await interaction.response.edit_message(content=f"Reinstalled `{newc.name}`. **All data in the old container was deleted.**", embed=None, view=None)
        except Exception as e:
            await interaction.response.send_message(f"Reinstall failed: `{e}`", ephemeral=True)

class ManageView(discord.ui.View):
    def __init__(self, owner_id, container_id):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.container_id = container_id

    async def get_container(self, interaction):
        if interaction.user.id != self.owner_id and not is_admin(interaction.user.id):
            await interaction.response.send_message("You can only manage your own VPS.", ephemeral=True)
            return None
        c = find_owned(self.container_id, interaction.user.id)
        if c is None:
            await interaction.response.send_message("Container not found or access denied.", ephemeral=True)
        return c

    @discord.ui.button(label="Start", style=discord.ButtonStyle.success, emoji="▶️", row=0)
    async def start_btn(self, interaction, button):
        c = await self.get_container(interaction)
        if c:
            try:
                c.start()
                await interaction.response.send_message(f"Started `{c.name}`.", ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"Start failed: `{e}`", ephemeral=True)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.secondary, emoji="⏹️", row=0)
    async def stop_btn(self, interaction, button):
        c = await self.get_container(interaction)
        if c:
            try:
                c.stop(timeout=10)
                await interaction.response.send_message(f"Stopped `{c.name}`.", ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"Stop failed: `{e}`", ephemeral=True)

    @discord.ui.button(label="Restart", style=discord.ButtonStyle.primary, emoji="🔄", row=0)
    async def restart_btn(self, interaction, button):
        c = await self.get_container(interaction)
        if c:
            try:
                c.restart(timeout=10)
                await interaction.response.send_message(f"Restarted `{c.name}`.", ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"Restart failed: `{e}`", ephemeral=True)

    @discord.ui.button(label="Live Stats", style=discord.ButtonStyle.primary, emoji="📊", row=1)
    async def stats_btn(self, interaction, button):
        c = await self.get_container(interaction)
        if not c:
            return
        try:
            c.reload()
            stats = c.stats(stream=False)
            mem = stats.get("memory_stats", {})
            usage = mem.get("usage", 0)
            limit = mem.get("limit", 0)
            cpu_stats = stats.get("cpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0)
            await interaction.response.send_message(
                f"**{c.name} — live stats**\nStatus: `{c.status}`\nMemory: `{human_bytes(usage)}` / `{human_bytes(limit)}`\nCPU counter: `{cpu_stats}`",
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(f"Stats unavailable (container may be stopped): `{e}`", ephemeral=True)

    @discord.ui.button(label="SSHX", style=discord.ButtonStyle.primary, emoji="🔑", row=1)
    async def sshx_btn(self, interaction, button):
        c = await self.get_container(interaction)
        if not c: return
        if not SSHX_ENABLED:
            return await interaction.response.send_message("SSHX is disabled by the bot owner.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            url = await asyncio.to_thread(start_sshx_session, c)
            try:
                await interaction.user.send(f"🔑 **SSHX terminal for `{c.name}`**\n{url}\n\nAnyone with this link may access the shared terminal. Keep it private.")
                await interaction.followup.send("✅ SSHX installed/launched inside your VPS. Session link sent to your DM.", ephemeral=True)
            except discord.Forbidden:
                await interaction.followup.send(f"Couldn't DM you. Keep this SSHX link private: {url}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"SSHX setup failed: `{str(e)[:800]}`", ephemeral=True)

    @discord.ui.button(label="Reinstall", style=discord.ButtonStyle.danger, emoji="♻️", row=2)
    async def reinstall_btn(self, interaction, button):
        c = await self.get_container(interaction)
        if c:
            await interaction.response.send_message(
                f"Reinstall `{c.name}`? This deletes the container and its data, then creates a fresh root filesystem. Continue?",
                view=ReinstallConfirm(interaction.user.id, c.id), ephemeral=True
            )

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, emoji="🗑️", row=2)
    async def delete_btn(self, interaction, button):
        c = await self.get_container(interaction)
        if c:
            await interaction.response.send_message(
                f"Delete `{c.name}` permanently? All data inside it will be lost. Reply with `{PREFIX}delete {c.name}` in DM or ask an admin.",
                ephemeral=True
            )

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} | prefix={PREFIX} | admins={len(ADMIN_IDS)}")
    if client is None:
        print("WARNING: Docker client is unavailable; VPS commands will fail.")

@bot.command(name="help")
async def help_cmd(ctx):
    embed = discord.Embed(title="🖥️ Docker VPS Maker — Commands", color=discord.Color.blurple())
    embed.description = (
        f"`{PREFIX}create <hostname> <ram> <cpu> <disk> @user <os>` — Admin: create VPS\n"
        f"`{PREFIX}manage` — show your VPS controls\n"
        f"`{PREFIX}os` — list supported OS templates\n"
        f"`{PREFIX}list` — list your VPS containers\n"
        f"`{PREFIX}start <container>` — start a VPS\n"
        f"`{PREFIX}stop <container>` — stop a VPS\n"
        f"`{PREFIX}restart <container>` — restart a VPS\n"
        f"`{PREFIX}stats <container>` — show VPS stats\n"
        f"`{PREFIX}sshx <container>` — DM SSHX output if installed\n"
        f"`{PREFIX}reinstall <container>` — reinstall (deletes data)\n"
        f"`{PREFIX}delete <container>` — delete a VPS\n"
        f"`{PREFIX}rename <old> <new>` — rename a VPS\n"
        f"`{PREFIX}limits` — show resource caps\n"
        f"`{PREFIX}admin-add @user` — add bot admin (admin only)\n"
        f"`{PREFIX}admin-remove @user` — remove bot admin (admin only)\n"
        f"`{PREFIX}admins` — show configured admins\n"
        f"`{PREFIX}ping` — bot latency\n"
        f"`{PREFIX}uptime` — bot uptime"
    )
    embed.set_footer(text="Only run this bot on a Docker host you are authorized to administer.")
    await ctx.reply(embed=embed, mention_author=False)

@bot.command(name="os")
async def os_cmd(ctx):
    await ctx.reply("**Supported OS keys:**\n" + "\n".join(f"`{k}` — {v[0]}" for k, v in OS_IMAGES.items()), mention_author=False)

@bot.command()
async def create(ctx, hostname: str = None, ram: str = None, cpu: str = None, disk: str = None, member: discord.Member = None, os_key: str = None):
    if not is_admin(ctx.author.id):
        return await ctx.reply("❌ Admin only.", mention_author=False)
    if not all([hostname, ram, cpu, disk, member, os_key]):
        return await ctx.reply(f"Usage: `{PREFIX}create <hostname> <ram> <cpu> <disk> @user <os>`\nExample: `{PREFIX}create demo-vps 2G 2 20G @user ubuntu-24.04`", mention_author=False)
    if client is None:
        return await ctx.reply("Docker is unavailable on this host.", mention_author=False)
    hostname = hostname.lower()
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,30}", hostname):
        return await ctx.reply("Hostname must be 2–31 chars, start with a letter, and contain only lowercase letters, digits, or hyphens.", mention_author=False)
    try:
        ram = parse_size(ram, MAX_RAM, "RAM")
        disk = parse_size(disk, MAX_DISK, "Disk")
        cpu_val = float(cpu)
        if cpu_val <= 0 or cpu_val > MAX_CPU:
            raise ValueError(f"CPU must be greater than 0 and no more than {MAX_CPU}.")
        if os_key not in OS_IMAGES:
            raise ValueError(f"Unknown OS `{os_key}`. Run `{PREFIX}os`.")
        if any(c.name == hostname for c in client.containers.list(all=True)):
            raise ValueError(f"Container name `{hostname}` is already in use.")
        label_map = {
            "vps_managed": "true", "vps_owner": str(member.id), "vps_image": OS_IMAGES[os_key][1],
            "vps_ram": ram, "vps_cpu": str(cpu_val), "vps_disk": disk
        }
        # Docker storage quota support varies by driver. Fail closed if quota cannot be applied.
        try:
            c = client.containers.run(
                OS_IMAGES[os_key][1], ["sh", "-lc", "while :; do sleep 3600; done"],
                name=hostname, detach=True, tty=True, stdin_open=True,
                mem_limit=ram, nano_cpus=int(cpu_val * 1e9),
                storage_opt={"size": disk}, labels=label_map,
                restart_policy={"Name": "unless-stopped"}
            )
        except APIError as e:
            if "storage-opt" in str(e).lower() or "storage option" in str(e).lower() or "not supported" in str(e).lower():
                return await ctx.reply("Docker host does not support per-container disk quota with its current storage driver. Configure a supported quota driver (for example XFS project quotas) before creating VPS containers. No container was created.", mention_author=False)
            raise
        embed = container_embed(c)
        await ctx.reply(f"✅ Created VPS for {member.mention}.", embed=embed, view=ManageView(member.id, c.id), mention_author=False)
        try:
            await member.send(
                f"🎉 Your VPS was created on **{ctx.guild.name if ctx.guild else 'the server'}**.\n"
                f"Hostname: `{hostname}`\nOS: **{OS_IMAGES[os_key][0]}**\nRAM: `{ram}` • CPU: `{cpu_val:g} cores` • Disk quota: `{disk}`\n"
                f"Container ID: `{c.short_id}`\nUse `{PREFIX}manage` in the server to open your controls."
            )
        except discord.Forbidden:
            await ctx.send(f"⚠️ {member.mention}, your VPS is created, but I couldn't DM you. Enable server-member DMs.")
    except ValueError as e:
        await ctx.reply(f"❌ {e}", mention_author=False)
    except Exception as e:
        await ctx.reply(f"❌ VPS creation failed: `{str(e)[:1200]}`", mention_author=False)

@bot.command()
async def manage(ctx):
    containers = owned_containers(ctx.author.id)
    if is_admin(ctx.author.id):
        # Admins may use ?manage to see all managed containers.
        containers = [c for c in client.containers.list(all=True, filters={"label": "vps_managed=true"})] if client else []
    if not containers:
        return await ctx.reply("No VPS containers found for you.", mention_author=False)
    for c in containers[:10]:
        await ctx.reply(embed=container_embed(c), view=ManageView(ctx.author.id, c.id), mention_author=False)

@bot.command(name="list")
async def list_cmd(ctx):
    containers = owned_containers(ctx.author.id)
    if is_admin(ctx.author.id) and client:
        containers = client.containers.list(all=True, filters={"label": "vps_managed=true"})
    if not containers:
        return await ctx.reply("No VPS containers found.", mention_author=False)
    await ctx.reply("\n".join(f"• `{c.name}` — `{c.status}` — ID `{c.short_id}`" for c in containers[:25]), mention_author=False)

async def resolve_container(ctx, name):
    if client is None:
        await ctx.reply("Docker unavailable.", mention_author=False)
        return None
    try:
        c = client.containers.get(name)
    except NotFound:
        await ctx.reply("Container not found.", mention_author=False)
        return None
    if c.labels.get("vps_managed") != "true" or (not is_admin(ctx.author.id) and container_owner(c) != str(ctx.author.id)):
        await ctx.reply("Container not found or access denied.", mention_author=False)
        return None
    return c

@bot.command()
async def start(ctx, name: str):
    c = await resolve_container(ctx, name)
    if c:
        try:
            c.start()
            await ctx.reply(f"▶️ Started `{c.name}`.", mention_author=False)
        except Exception as e:
            await ctx.reply(f"Start failed: `{e}`", mention_author=False)

@bot.command()
async def stop(ctx, name: str):
    c = await resolve_container(ctx, name)
    if c:
        try:
            c.stop(timeout=10)
            await ctx.reply(f"⏹️ Stopped `{c.name}`.", mention_author=False)
        except Exception as e:
            await ctx.reply(f"Stop failed: `{e}`", mention_author=False)

@bot.command()
async def restart(ctx, name: str):
    c = await resolve_container(ctx, name)
    if c:
        try:
            c.restart(timeout=10)
            await ctx.reply(f"🔄 Restarted `{c.name}`.", mention_author=False)
        except Exception as e:
            await ctx.reply(f"Restart failed: `{e}`", mention_author=False)

@bot.command()
async def stats(ctx, name: str):
    c = await resolve_container(ctx, name)
    if c:
        try:
            c.reload()
            s = c.stats(stream=False)
            mem = s.get("memory_stats", {})
            await ctx.reply(f"**{c.name}** (`{c.status}`)\nRAM: `{human_bytes(mem.get('usage', 0))}` / `{human_bytes(mem.get('limit', 0))}`", mention_author=False)
        except Exception as e:
            await ctx.reply(f"Stats unavailable: `{e}`", mention_author=False)

@bot.command()
async def sshx(ctx, name: str):
    c = await resolve_container(ctx, name)
    if not c: return
    if not SSHX_ENABLED: return await ctx.reply("SSHX is disabled.", mention_author=False)
    msg=await ctx.reply("⏳ Installing/checking SSHX and starting your terminal session…", mention_author=False)
    try:
        url=await asyncio.to_thread(start_sshx_session,c)
        try:
            await ctx.author.send(f"🔑 **SSHX terminal for `{c.name}`**\n{url}\n\nAnyone with this link may access the shared terminal. Keep it private.")
            await msg.edit(content="✅ SSHX session link sent to your DM.")
        except discord.Forbidden:
            await msg.edit(content=f"Couldn't DM you. Keep this SSHX link private: {url}")
    except Exception as e:
        await msg.edit(content=f"SSHX setup failed: `{str(e)[:800]}`")

@bot.command()
async def reinstall(ctx, name: str):
    c = await resolve_container(ctx, name)
    if not c:
        return
    await ctx.reply(
        f"⚠️ Reinstall `{c.name}` destroys its container data. To confirm, type `{PREFIX}reinstall-confirm {c.name}` within 30 seconds.",
        mention_author=False
    )
    bot.pending_reinstalls = getattr(bot, "pending_reinstalls", {})
    bot.pending_reinstalls[(ctx.author.id, name)] = time.time() + 30

@bot.command(name="reinstall-confirm")
async def reinstall_confirm(ctx, name: str):
    pending = getattr(bot, "pending_reinstalls", {})
    expires = pending.pop((ctx.author.id, name), 0)
    if expires < time.time():
        return await ctx.reply("No active confirmation. Run reinstall again.", mention_author=False)
    c = await resolve_container(ctx, name)
    if not c:
        return
    labels = dict(c.labels)
    image = labels.get("vps_image", "ubuntu:24.04")
    try:
        c.remove(force=True)
        newc = client.containers.run(
            image, ["sh", "-lc", "while :; do sleep 3600; done"], name=name, detach=True,
            tty=True, stdin_open=True, mem_limit=labels.get("vps_ram", "2G"),
            nano_cpus=int(float(labels.get("vps_cpu", "2")) * 1e9),
            labels=labels, restart_policy={"Name": "unless-stopped"}
        )
        await ctx.reply(f"♻️ Reinstalled `{newc.name}`. Old container data was deleted.", mention_author=False)
    except Exception as e:
        await ctx.reply(f"Reinstall failed: `{e}`", mention_author=False)

@bot.command()
async def delete(ctx, name: str):
    c = await resolve_container(ctx, name)
    if not c:
        return
    await ctx.reply(f"⚠️ This permanently deletes `{c.name}` and its container data. Confirm with `{PREFIX}delete-confirm {name}` within 30 seconds.", mention_author=False)
    bot.pending_deletes = getattr(bot, "pending_deletes", {})
    bot.pending_deletes[(ctx.author.id, name)] = time.time() + 30

@bot.command(name="delete-confirm")
async def delete_confirm(ctx, name: str):
    pending = getattr(bot, "pending_deletes", {})
    expires = pending.pop((ctx.author.id, name), 0)
    if expires < time.time():
        return await ctx.reply("No active confirmation. Run delete again.", mention_author=False)
    c = await resolve_container(ctx, name)
    if c:
        try:
            c.remove(force=True)
            await ctx.reply(f"🗑️ Deleted `{name}`.", mention_author=False)
        except Exception as e:
            await ctx.reply(f"Delete failed: `{e}`", mention_author=False)

@bot.command()
async def rename(ctx, old: str, new: str):
    c = await resolve_container(ctx, old)
    if not c:
        return
    if not is_admin(ctx.author.id):
        return await ctx.reply("Only admins can rename VPS containers.", mention_author=False)
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,30}", new):
        return await ctx.reply("Invalid new hostname.", mention_author=False)
    try:
        c.rename(new)
        await ctx.reply(f"Renamed `{old}` to `{new}`.", mention_author=False)
    except Exception as e:
        await ctx.reply(f"Rename failed: `{e}`", mention_author=False)

@bot.command()
async def limits(ctx):
    await ctx.reply(f"Configured caps: RAM `{MAX_RAM}`, CPU `{MAX_CPU:g} cores`, Disk `{MAX_DISK}`. Disk quota enforcement depends on Docker storage driver.", mention_author=False)

@bot.command()
async def admins(ctx):
    await ctx.reply("Configured admins: " + (", ".join(f"`{x}`" for x in sorted(ADMIN_IDS)) if ADMIN_IDS else "none"), mention_author=False)

@bot.command(name="admin-add")
async def admin_add(ctx, member: discord.Member):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admin only. Initial admins must be set in `.env`.", mention_author=False)
    ADMIN_IDS.add(member.id)
    await ctx.reply(f"✅ {member.mention} added as admin for this bot process. Add their ID to `ADMIN_IDS` in `.env` too so it persists after restart.", mention_author=False)

@bot.command(name="admin-remove")
async def admin_remove(ctx, member: discord.Member):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admin only.", mention_author=False)
    if member.id in {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}:
        return await ctx.reply("This user is in the permanent `.env` admin list. Remove their ID from `.env` and restart the bot.", mention_author=False)
    ADMIN_IDS.discard(member.id)
    await ctx.reply(f"Removed {member.mention} from runtime admins.", mention_author=False)

@bot.command()
async def ping(ctx):
    await ctx.reply(f"🏓 Pong! `{round(bot.latency * 1000)} ms`", mention_author=False)

@bot.command()
async def uptime(ctx):
    seconds = int(time.time() - BOT_START)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    await ctx.reply(f"Bot uptime: `{hours}h {minutes}m {secs}s`", mention_author=False)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        return await ctx.reply(f"Missing argument. Run `{PREFIX}help`.", mention_author=False)
    if isinstance(error, commands.BadArgument):
        return await ctx.reply(f"Invalid argument. For user mentions, mention a server member. Run `{PREFIX}help`.", mention_author=False)
    if isinstance(error, commands.CommandInvokeError):
        error = error.original
    await ctx.reply(f"Command error: `{str(error)[:900]}`", mention_author=False)

if not TOKEN or TOKEN == "PUT_DISCORD_BOT_TOKEN_HERE":
    raise SystemExit("Set BOT_TOKEN in .env before starting.")
bot.run(TOKEN)
