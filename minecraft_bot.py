import discord
from discord.ext import commands, tasks
from discord import app_commands
from typing import List
import json
import asyncio
import subprocess
import logging
import os
import re
import sys
from datetime import datetime, timedelta
import threading
import time
import queue
import glob
import pickle
from typing import Optional, Dict, Any
import platform

# Regex pour parser les messages du chat Minecraft (Vanilla & Forge)
# Exemples matchés :
#   [12:34:56] [Server thread/INFO]: <PlayerName> >> hello world
#   [12:34:56] [Server thread/INFO] [minecraft/DedicatedServer]: <PlayerName> >> hello
MC_CHAT_PATTERN = re.compile(
    r"^\[[\d:]+\] \[.*?(?:Server thread|Async Chat Thread).*?INFO.*?\].*?: <([^>]+)> >>(.+)$"
)

# Regex pour les events join/leave
MC_JOIN_PATTERN = re.compile(
    r"^\[[\d:]+\] \[.*?INFO.*?\].*?: (\w+) joined the game$"
)
MC_LEAVE_PATTERN = re.compile(
    r"^\[[\d:]+\] \[.*?INFO.*?\].*?: (\w+) left the game$"
)

PREFERENCES_FILE = "discord_users_preferences.data"


# ---------------------------------------------------------------------------
# User preferences manager
# ---------------------------------------------------------------------------

class UserPreferencesManager:
    """Manages persistent Discord user preferences using pickle serialization.

    Preferences are stored as a mapping of Discord user ID (int) to a dict
    of preference keys/values.  Currently only ``last_server`` is used, but
    the structure is generic enough to hold future preferences.

    The file is written synchronously; given the low write frequency (once per
    slash-command invocation at most) this is acceptable without an async
    wrapper.

    Parameters
    ----------
    filepath : str
        Path to the pickle file used for persistence.

    Examples
    --------
    >>> mgr = UserPreferencesManager("prefs.data")
    >>> mgr.set_last_server(123456789, "survival")
    >>> mgr.get_last_server(123456789)
    'survival'
    >>> mgr.cleanup_unknown_servers({"survival", "creative"})
    """

    def __init__(self, filepath: str = PREFERENCES_FILE) -> None:
        self._filepath = filepath
        # { user_id (int): { "last_server": str, ... } }
        self._data: Dict[int, Dict[str, Any]] = {}
        self._load()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load preferences from disk.  Silently starts fresh on any error."""
        if not os.path.exists(self._filepath):
            return
        try:
            with open(self._filepath, "rb") as fh:
                loaded = pickle.load(fh)
            if isinstance(loaded, dict):
                self._data = loaded
            else:
                logger.warning(
                    "Preferences file has unexpected format — starting fresh."
                )
        except Exception as exc:
            logger.error(
                f"Could not load preferences from '{self._filepath}': {exc} "
                "— starting fresh."
            )

    def _save(self) -> None:
        """Persist current preferences to disk.

        Raises
        ------
        OSError
            If the file cannot be written (logged, not re-raised).
        """
        try:
            with open(self._filepath, "wb") as fh:
                pickle.dump(self._data, fh, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as exc:
            logger.error(f"Could not save preferences to '{self._filepath}': {exc}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_last_server(self, user_id: int) -> Optional[str]:
        """Return the last server used by a Discord user, or ``None``.

        Parameters
        ----------
        user_id : int
            Discord user snowflake ID.

        Returns
        -------
        str or None
            The stored server name, or ``None`` if no preference exists.
        """
        return self._data.get(user_id, {}).get("last_server")

    def set_last_server(self, user_id: int, server_name: str) -> None:
        """Persist the last server used by a Discord user.

        Parameters
        ----------
        user_id : int
            Discord user snowflake ID.
        server_name : str
            Name of the Minecraft server as defined in ``config.json``.
        """
        if user_id not in self._data:
            self._data[user_id] = {}
        self._data[user_id]["last_server"] = server_name
        self._save()

    def cleanup_unknown_servers(self, known_servers: set) -> int:
        """Remove preferences that reference servers no longer in the config.

        Parameters
        ----------
        known_servers : set of str
            The set of valid server names currently loaded from ``config.json``.

        Returns
        -------
        int
            Number of user preferences cleared.

        Examples
        --------
        >>> mgr.cleanup_unknown_servers({"survival", "creative"})
        2
        """
        cleared = 0
        for user_id, prefs in list(self._data.items()):
            last = prefs.get("last_server")
            if last and last not in known_servers:
                del self._data[user_id]["last_server"]
                # Remove the user entry entirely if no prefs remain
                if not self._data[user_id]:
                    del self._data[user_id]
                cleared += 1
        if cleared:
            self._save()
        return cleared


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging():
    """Configure le système de logging avec rotation automatique"""
    if not os.path.exists('logs'):
        os.makedirs('logs')

    cleanup_old_logs()

    log_filename = f"logs/bot_{datetime.now().strftime('%Y-%m-%d')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format='[%(asctime)s] [%(levelname)s] %(message)s',
        handlers=[
            logging.FileHandler(log_filename, encoding='utf-8'),
            logging.StreamHandler(sys.stdout)
        ]
    )
    return logging.getLogger(__name__)


def cleanup_old_logs():
    """Supprime les fichiers de logs plus vieux que le nombre de jours spécifié"""
    try:
        config = load_config()
        retention_days = config.get('log_retention_days', 3)
        cutoff_date = datetime.now() - timedelta(days=retention_days)

        for log_file in glob.glob('logs/bot_*.log'):
            file_date_str = log_file.split('_')[1].replace('.log', '')
            file_date = datetime.strptime(file_date_str, '%Y-%m-%d')

            if file_date < cutoff_date:
                os.remove(log_file)
                print(f"Ancien fichier de log supprimé : {log_file}")
    except Exception as e:
        print(f"Erreur lors du nettoyage des logs : {e}")


def is_pid_running(pid: int) -> bool:
    """Vérifie si un processus avec le PID donné est en cours d'exécution"""
    try:
        if platform.system() == "Windows":
            output = subprocess.check_output(
                f"tasklist /FI \"PID eq {pid}\"", shell=True
            ).decode()
            return str(pid) in output
        else:
            os.kill(pid, 0)
            return True
    except OSError:
        return False


def load_config():
    """Charge la configuration depuis le fichier JSON"""
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        print("Erreur : fichier config.json non trouvé")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Erreur de format JSON : {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# MinecraftServer
# ---------------------------------------------------------------------------

class MinecraftServer:
    """Classe pour gérer un serveur Minecraft individuel"""

    def __init__(self, name: str, config: Dict[str, Any], template: Dict[str, Any]):
        self.name = name
        self.config = config
        self.template = template
        self.process = None
        self.is_running = False
        self.log_queue = queue.Queue()
        self.chat_queue = queue.Queue()
        self.log_thread = None
        self.output_thread = None

    def _get_executable_command(self) -> List[str]:
        """Construit la commande d'exécution basée sur le template"""
        executable = self.template.get('executable', {})
        exe_type = executable.get('type', 'jar')

        if exe_type == 'script':
            if platform.system() == 'Windows':
                script_name = executable.get('windows', 'start.bat')
                return ['cmd', '/c', script_name]
            else:
                script_name = executable.get('linux', 'start.sh')
                return ['bash', script_name]
        elif exe_type == 'jar':
            java_args: str = self.template.get(
                'java_args', '-Xmx{memory} -Xms{min_memory} -jar {jar_file} nogui'
            )
            jar_file = executable.get('file', 'server.jar')

            variables = self.config.get('variables', {})
            variables['jar_file'] = jar_file

            args = java_args.format(**variables)
            return ['java'] + args.split()

        raise ValueError(f"Type d'exécutable non supporté : {exe_type}")

    def start(self) -> bool:
        """Démarre le serveur Minecraft"""
        if self.is_running:
            return False

        try:
            path = self.config.get('path')
            if not os.path.exists(path):
                raise FileNotFoundError(f"Chemin du serveur non trouvé : {path}")

            cmd = self._get_executable_command()

            self.process = subprocess.Popen(
                cmd,
                cwd=path,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )

            self.is_running = True
            self._start_log_monitoring()

            logger.info(f"Serveur '{self.name}' démarré avec PID {self.process.pid}")
            return True

        except Exception as e:
            logger.error(f"Erreur lors du démarrage du serveur '{self.name}' : {e}")
            return False

    def stop(self) -> bool:
        """Arrête le serveur Minecraft"""
        if not self.is_running or not self.process:
            return False

        try:
            self.send_command('stop')

            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=10)
                if self.process.poll() is None:
                    self.process.kill()

            self.is_running = False
            self.process = None
            self._stop_log_monitoring()

            logger.info(f"Serveur '{self.name}' arrêté")
            return True

        except Exception as e:
            logger.error(f"Erreur lors de l'arrêt du serveur '{self.name}' : {e}")
            return False

    def send_command(self, command: str) -> bool:
        """Envoie une commande au serveur Minecraft"""
        if not self.is_running or not self.process:
            return False

        try:
            self.process.stdin.write(f"{command}\n")
            self.process.stdin.flush()
            logger.info(f"Commande envoyée au serveur '{self.name}' : {command}")
            return True
        except Exception as e:
            logger.error(
                f"Erreur lors de l'envoi de la commande au serveur '{self.name}' : {e}"
            )
            return False

    def _start_log_monitoring(self):
        """Démarre le monitoring des logs du serveur"""
        if self.output_thread and self.output_thread.is_alive():
            return

        self.output_thread = threading.Thread(target=self._monitor_output, daemon=True)
        self.output_thread.start()

    def _stop_log_monitoring(self):
        """Arrête le monitoring des logs du serveur"""
        pass

    def _monitor_output(self):
        """Thread de monitoring de la sortie du serveur"""
        try:
            while self.is_running and self.process and self.process.poll() is None:
                line = self.process.stdout.readline()
                if line:
                    line = line.strip()
                    if line:
                        self.log_queue.put(line)

                        chat_match = MC_CHAT_PATTERN.match(line)
                        if chat_match:
                            player, message = chat_match.group(1), chat_match.group(2)
                            self.chat_queue.put(
                                {"type": "chat", "player": player, "message": message}
                            )
                            continue

                        join_match = MC_JOIN_PATTERN.match(line)
                        if join_match:
                            self.chat_queue.put(
                                {"type": "join", "player": join_match.group(1)}
                            )
                            continue

                        leave_match = MC_LEAVE_PATTERN.match(line)
                        if leave_match:
                            self.chat_queue.put(
                                {"type": "leave", "player": leave_match.group(1)}
                            )
                else:
                    time.sleep(0.1)
        except Exception as e:
            logger.error(
                f"Erreur dans le monitoring des logs du serveur '{self.name}' : {e}"
            )

    def get_logs(self) -> List[str]:
        """Récupère les logs en attente"""
        logs = []
        while not self.log_queue.empty():
            try:
                logs.append(self.log_queue.get_nowait())
            except queue.Empty:
                break
        return logs

    def get_chat_events(self) -> List[Dict[str, str]]:
        """Récupère les events de chat en attente (messages, join, leave)."""
        events = []
        while not self.chat_queue.empty():
            try:
                events.append(self.chat_queue.get_nowait())
            except queue.Empty:
                break
        return events

    def send_message(self, pseudo: str, message: str) -> bool:
        """Envoie un message dans le chat Minecraft"""
        return self.send_command(
            f'tellraw @a ["",{{"text":"<{pseudo}>","color":"dark_aqua"}},'
            f'{{"text":" {message}   ","color":"gray"}}]'
        )


# ---------------------------------------------------------------------------
# MinecraftBot
# ---------------------------------------------------------------------------

class MinecraftBot(commands.Bot):
    """Bot Discord pour la gestion des serveurs Minecraft"""

    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True

        super().__init__(command_prefix='/', intents=intents)

        self.config = load_config()
        self.servers: Dict[str, MinecraftServer] = {}
        self.log_channels: Dict[str, int] = {}
        self.chat_channels: Dict[str, int] = {}
        self.guild_events: List = []
        self.bot_log_channel = None

        # User preferences (last server used per Discord user)
        self.user_prefs = UserPreferencesManager()

        self._initialize_servers()

    def _initialize_servers(self):
        """Initialise les serveurs Minecraft depuis la configuration"""
        templates = self.config.get('templates', {})
        servers_config = self.config.get('servers', [])

        for server_config in servers_config:
            name = server_config['name']
            template_name = server_config.get('template')

            if template_name not in templates:
                logger.error(
                    f"Template '{template_name}' non trouvé pour le serveur '{name}'"
                )
                continue

            template = templates[template_name]
            server = MinecraftServer(name, server_config, template)

            self.servers[name] = server

            log_channel = server_config.get('discord_log_channel')
            if log_channel:
                self.log_channels[name] = log_channel

            chat_channel = server_config.get('discord_chat_channel')
            if chat_channel:
                self.chat_channels[name] = chat_channel

    def get_server_choices(self) -> List[app_commands.Choice[str]]:
        """Génère la liste des choix de serveurs pour les commandes slash"""
        return [
            app_commands.Choice(name=server_name, value=server_name)
            for server_name in self.servers.keys()
        ]

    def _resolve_server(
        self,
        user_id: int,
        server_arg: Optional[str],
    ) -> Optional[str]:
        """Resolve the target server name for a slash command.

        Resolution order:
        1. Explicit ``server`` argument provided by the user.
        2. Last server stored in the user's preferences.
        3. First server declared in ``config.json`` (legacy fallback).

        Returns ``None`` when no servers are configured at all.

        Parameters
        ----------
        user_id : int
            Discord user snowflake ID of the invoking user.
        server_arg : str or None
            The ``server`` parameter value from the slash command, or ``None``
            when the user omitted it.

        Returns
        -------
        str or None
            Resolved server name, or ``None`` if no servers are configured.
        """
        if server_arg is not None:
            return server_arg

        if not self.servers:
            return None

        last = self.user_prefs.get_last_server(user_id)
        if last and last in self.servers:
            return last

        # Legacy fallback: first server in config
        return next(iter(self.servers))

    async def on_ready(self):
        """Événement déclenché quand le bot est prêt"""
        logger.info(f"Bot connecté en tant que {self.user}")

        # Clean up preferences that reference servers removed from the config
        known = set(self.servers.keys())
        cleared = self.user_prefs.cleanup_unknown_servers(known)
        if cleared:
            logger.info(
                f"Preferences cleanup: {cleared} user(s) had an unknown server cleared."
            )

        bot_log_channel_id = self.config.get('bot_log_channel')
        if bot_log_channel_id:
            self.bot_log_channel = self.get_channel(bot_log_channel_id)

        try:
            synced = await self.tree.sync()
            logger.info(f"{len(synced)} commandes slash synchronisées")
            await self._send_bot_log(
                f"🟢 Bot démarré avec {len(synced)} commandes disponibles"
            )
        except Exception as e:
            logger.error(f"Erreur lors de la synchronisation des commandes : {e}")

        self.update_status.start()
        self.monitor_server_logs.start()
        self.monitor_chat_messages.start()
        self.cleanup_logs.start()
        self.check_server_processes.start()

        await self._update_bot_status()

    async def _send_bot_log(self, message: str):
        """Envoie un message dans le salon de logs du bot"""
        if self.bot_log_channel:
            try:
                current_time = datetime.now().strftime("`[%H:%M:%S]` ")
                await self.bot_log_channel.send(current_time + message)
            except Exception as e:
                logger.error(f"Erreur lors de l'envoi du log bot : {e}")

    async def _update_bot_status(self):
        """Met à jour le statut du bot"""
        active_servers = [name for name, server in self.servers.items() if server.is_running]

        if not active_servers:
            await self.change_presence(
                status=discord.Status.idle,
                activity=discord.Game("Aucun serveur actif")
            )
        elif len(active_servers) == 1:
            await self.change_presence(
                status=discord.Status.online,
                activity=discord.Game(f"Serveur: {active_servers[0]}")
            )
        else:
            await self.change_presence(
                status=discord.Status.online,
                activity=discord.Game(f"{len(active_servers)} serveurs actifs")
            )

    def _has_permission(self, user: discord.Member, command_name: str) -> bool:
        """Vérifie si l'utilisateur a la permission d'exécuter la commande"""
        permissions = self.config.get('permissions', {})
        required_roles = permissions.get(command_name, [])

        if not required_roles:
            return True

        user_roles = [role.id for role in user.roles]
        return any(role_id in user_roles for role_id in required_roles)

    @tasks.loop(seconds=30)
    async def update_status(self):
        """Tâche périodique de mise à jour du statut"""
        await self._update_bot_status()

    @tasks.loop(seconds=0.5)
    async def monitor_server_logs(self):
        """Tâche de monitoring des logs des serveurs"""
        for server_name, server in self.servers.items():
            if not server.is_running:
                continue

            logs = server.get_logs()
            if logs and server_name in self.log_channels:
                channel_id = self.log_channels[server_name]
                channel = self.get_channel(channel_id)

                if channel:
                    log_batch = '\n'.join(logs[:10])
                    if len(log_batch) > 1900:
                        log_batch = log_batch[:1900] + "..."

                    try:
                        await channel.send(f"```\n{log_batch}\n```")
                    except Exception as e:
                        logger.error(
                            f"Erreur lors de l'envoi des logs pour {server_name} : {e}"
                        )

    @tasks.loop(hours=24)
    async def cleanup_logs(self):
        """Tâche de nettoyage quotidien des logs"""
        cleanup_old_logs()
        await self._send_bot_log("🧹 Nettoyage automatique des anciens logs effectué")

    @tasks.loop(seconds=0.5)
    async def monitor_chat_messages(self):
        """Tâche de relay des messages chat MC -> Discord."""
        for server_name, server in self.servers.items():
            if not server.is_running:
                continue

            events = server.get_chat_events()
            if not events:
                continue

            channel_id = self.chat_channels.get(server_name)
            if not channel_id:
                continue

            channel = self.get_channel(channel_id)
            if not channel:
                continue

            for event in events:
                try:
                    if event["type"] == "chat":
                        await channel.send(
                            f"**[{server_name}]** 🎮 `{event['player']}` : {event['message']}"
                        )
                    elif event["type"] == "join":
                        await channel.send(
                            f"**[{server_name}]** ➡️ `{event['player']}` a rejoint le serveur"
                        )
                    elif event["type"] == "leave":
                        await channel.send(
                            f"**[{server_name}]** ⬅️ `{event['player']}` a quitté le serveur"
                        )
                except Exception as e:
                    logger.error(
                        f"Erreur lors du relay chat pour {server_name} : {e}"
                    )

    async def on_message(self, message: discord.Message):
        """Relay des messages Discord -> MC pour les channels de chat configurés."""
        if message.author.bot:
            return

        for server_name, channel_id in self.chat_channels.items():
            if message.channel.id != channel_id:
                continue

            server = self.servers.get(server_name)
            if not server or not server.is_running:
                return

            display_name = message.author.display_name

            success = server.send_message(
                pseudo=f"[Discord] {display_name}", message=message.content
            )
            if not success:
                logger.warning(
                    f"Impossible de relayer le message Discord de "
                    f"{display_name} vers {server_name}"
                )
            return

        await self.process_commands(message)

    @tasks.loop(seconds=60)
    async def check_server_processes(self):
        """Tâche de vérification des processus des serveurs."""
        for server_name, server in self.servers.items():
            if not server.is_running or not server.process:
                continue

            if server.process.poll() is not None:
                return_code = server.process.returncode
                server.is_running = False
                server.process = None
                msg = (
                    f"⚠️ Le serveur `{server_name}` s'est arrêté "
                    f"(code retour : `{return_code}`)."
                )
                await self._send_bot_log(msg)
                logger.warning(
                    f"Serveur `{server_name}` arrêté, code retour : {return_code}"
                )


# ---------------------------------------------------------------------------
# Bot instantiation
# ---------------------------------------------------------------------------

logger = setup_logging()
bot = MinecraftBot()
bot.tree.clear_commands(guild=None)


# ---------------------------------------------------------------------------
# Autocomplete
# ---------------------------------------------------------------------------

async def server_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[str]]:
    """Fonction d'autocomplétion pour les noms de serveurs"""
    return [
        app_commands.Choice(name=server_name, value=server_name)
        for server_name in bot.servers.keys()
        if current.lower() in server_name.lower()
    ]


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

@bot.tree.command(name="start", description="Démarre un serveur Minecraft")
@app_commands.describe(server="Serveur à démarrer (optionnel, mémorise le dernier utilisé)")
@app_commands.autocomplete(server=server_autocomplete)
async def start_server(interaction: discord.Interaction, server: Optional[str] = None):
    """Commande pour démarrer un serveur"""
    if not bot._has_permission(interaction.user, 'start'):
        await interaction.response.send_message(
            "❌ Vous n'avez pas la permission d'utiliser cette commande.", ephemeral=True
        )
        return

    server = bot._resolve_server(interaction.user.id, server)

    if server is None:
        await interaction.response.send_message("❌ Aucun serveur configuré.", ephemeral=True)
        return

    if server not in bot.servers:
        await interaction.response.send_message(
            f"❌ Serveur `{server}` non trouvé.", ephemeral=True
        )
        return

    await interaction.response.defer()

    minecraft_server = bot.servers[server]

    if minecraft_server.is_running:
        await interaction.followup.send(
            f"⚠️ Le serveur `{server}` est déjà en cours d'exécution."
        )
        return

    success = minecraft_server.start()

    if success:
        bot.user_prefs.set_last_server(interaction.user.id, server)
        await interaction.followup.send(f"✅ Serveur `{server}` démarré avec succès!")
        await bot._send_bot_log(
            f"🟢 Serveur `{server}` démarré par {interaction.user.display_name}"
        )
        logger.info(f"Serveur `{server}` démarré par {interaction.user}")
    else:
        await interaction.followup.send(f"❌ Échec du démarrage du serveur `{server}`.")
        await bot._send_bot_log(
            f"🔴 Échec du démarrage du serveur `{server}` par {interaction.user.display_name}"
        )


@bot.tree.command(name="stop", description="Arrête un serveur Minecraft")
@app_commands.describe(server="Serveur à arrêter (optionnel, mémorise le dernier utilisé)")
@app_commands.autocomplete(server=server_autocomplete)
async def stop_server(interaction: discord.Interaction, server: Optional[str] = None):
    """Commande pour arrêter un serveur"""
    if not bot._has_permission(interaction.user, 'stop'):
        await interaction.response.send_message(
            "❌ Vous n'avez pas la permission d'utiliser cette commande.", ephemeral=True
        )
        return

    server = bot._resolve_server(interaction.user.id, server)

    if server is None:
        await interaction.response.send_message("❌ Aucun serveur configuré.", ephemeral=True)
        return

    if server not in bot.servers:
        await interaction.response.send_message(
            f"❌ Serveur `{server}` non trouvé.", ephemeral=True
        )
        return

    await interaction.response.defer()

    minecraft_server = bot.servers[server]

    if not minecraft_server.is_running:
        await interaction.followup.send(
            f"⚠️ Le serveur `{server}` n'est pas en cours d'exécution."
        )
        return

    success = minecraft_server.stop()

    if success:
        bot.user_prefs.set_last_server(interaction.user.id, server)
        await interaction.followup.send(f"✅ Serveur `{server}` arrêté avec succès!")
        await bot._send_bot_log(
            f"🟡 Serveur `{server}` arrêté par {interaction.user.display_name}"
        )
        logger.info(f"Serveur `{server}` arrêté par {interaction.user}")
    else:
        await interaction.followup.send(f"❌ Échec de l'arrêt du serveur `{server}`.")
        await bot._send_bot_log(
            f"🔴 Échec de l'arrêt du serveur `{server}` par {interaction.user.display_name}"
        )


@bot.tree.command(name="cmd", description="Exécute une commande sur un serveur Minecraft")
@app_commands.describe(
    commande="Commande à exécuter",
    server="Serveur cible (optionnel, mémorise le dernier utilisé)"
)
@app_commands.autocomplete(server=server_autocomplete)
async def execute_command(
    interaction: discord.Interaction,
    commande: str,
    server: Optional[str] = None
):
    """Commande pour exécuter une commande Minecraft"""
    if not bot._has_permission(interaction.user, 'cmd'):
        await interaction.response.send_message(
            "❌ Vous n'avez pas la permission d'utiliser cette commande.", ephemeral=True
        )
        return

    server = bot._resolve_server(interaction.user.id, server)

    if server is None:
        await interaction.response.send_message("❌ Aucun serveur configuré.", ephemeral=True)
        return

    if server not in bot.servers:
        await interaction.response.send_message(
            f"❌ Serveur `{server}` non trouvé.", ephemeral=True
        )
        return

    minecraft_server = bot.servers[server]

    if not minecraft_server.is_running:
        await interaction.response.send_message(
            f"⚠️ Le serveur `{server}` n'est pas en cours d'exécution.", ephemeral=True
        )
        return

    success = minecraft_server.send_command(commande)

    if success:
        bot.user_prefs.set_last_server(interaction.user.id, server)
        await interaction.response.send_message(
            f"✅ Commande exécutée sur `{server}` : `{commande}`"
        )
        await bot._send_bot_log(
            f"⚡ Commande exécutée sur `{server}` par "
            f"{interaction.user.display_name} : {commande}"
        )
        logger.info(f"Commande exécutée sur `{server}` par {interaction.user} : {commande}")
    else:
        await interaction.response.send_message(
            f"❌ Échec de l'exécution de la commande sur `{server}`.", ephemeral=True
        )


@bot.tree.command(
    name="frigo",
    description="Permet d'envoyer MAMAAAAAAAAN LE FRIGOOOOOOO sur tous les serveurs en ligne"
)
async def frigo_command(interaction: discord.Interaction):
    """Commande pour envoyer MAMAAAAAAAAN LE FRIGOOOOOOO sur tous les serveurs en ligne"""
    if not bot._has_permission(interaction.user, 'frigo'):
        await interaction.response.send_message(
            "❌ Vous n'avez pas la permission d'utiliser cette commande.", ephemeral=True
        )
        return

    online_servers = [name for name, server in bot.servers.items() if server.is_running]
    if not online_servers:
        await interaction.response.send_message(
            "⚠️ Aucun serveur en ligne. MAMAAAAAAAAN LE FRIGOOOOOOO"
        )
        return

    for server_name in online_servers:
        bot.servers[server_name].send_message(
            pseudo="stephlafourchette",
            message="MAMAAAAAAAAAAAN LE FRIGOOOOOOOOOOOOOOO"
        )

    await interaction.response.send_message(
        f"✅ Message envoyé sur les serveurs en ligne : `{', '.join(online_servers)}`"
    )


@bot.tree.command(name="say", description="Permet d'envoyer un message sur le serveur sélectionné")
@app_commands.describe(
    message="Message à envoyer",
    server="Serveur cible (optionnel, mémorise le dernier utilisé)"
)
@app_commands.autocomplete(server=server_autocomplete)
async def say_command(
    interaction: discord.Interaction,
    message: str,
    server: Optional[str] = None
):
    """Commande pour envoyer un message sur le serveur sélectionné"""
    if not bot._has_permission(interaction.user, 'say'):
        await interaction.response.send_message(
            "❌ Vous n'avez pas la permission d'utiliser cette commande.", ephemeral=True
        )
        return

    server = bot._resolve_server(interaction.user.id, server)

    if server is None:
        await interaction.response.send_message("❌ Aucun serveur configuré.", ephemeral=True)
        return

    if server not in bot.servers:
        await interaction.response.send_message(
            f"❌ Serveur `{server}` non trouvé.", ephemeral=True
        )
        return

    minecraft_server = bot.servers[server]

    if not minecraft_server.is_running:
        await interaction.response.send_message(
            f"⚠️ Le serveur `{server}` n'est pas en cours d'exécution.", ephemeral=True
        )
        return

    success = minecraft_server.send_message(pseudo=interaction.user.name, message=message)

    if success:
        bot.user_prefs.set_last_server(interaction.user.id, server)
        await interaction.response.send_message(
            f"✅ Message envoyé sur `{server}` : {message}"
        )
        await bot._send_bot_log(
            f"📝 Message envoyé sur `{server}` par {interaction.user.display_name} : {message}"
        )
        logger.info(f"Message envoyé sur `{server}` par {interaction.user} : {message}")
    else:
        await interaction.response.send_message(
            f"❌ Échec de l'envoi du message sur `{server}`.", ephemeral=True
        )


@bot.tree.command(name="sync", description="Recharge la configuration du bot")
async def sync_config(interaction: discord.Interaction):
    """Commande pour recharger la configuration"""
    if not bot._has_permission(interaction.user, 'sync'):
        await interaction.response.send_message(
            "❌ Vous n'avez pas la permission d'utiliser cette commande.", ephemeral=True
        )
        return

    await interaction.response.defer()

    try:
        running_servers = [name for name, server in bot.servers.items() if server.is_running]

        bot.config = load_config()

        old_servers = bot.servers.copy()
        bot.servers.clear()
        bot.log_channels.clear()
        bot.chat_channels.clear()
        bot._initialize_servers()

        for name in running_servers:
            if name in old_servers and name in bot.servers:
                bot.servers[name].process = old_servers[name].process
                bot.servers[name].is_running = old_servers[name].is_running
                bot.servers[name].log_queue = old_servers[name].log_queue
                bot.servers[name].log_thread = old_servers[name].log_thread
                bot.servers[name].output_thread = old_servers[name].output_thread

        # Clean up preferences for servers that may have been removed
        cleared = bot.user_prefs.cleanup_unknown_servers(set(bot.servers.keys()))
        if cleared:
            logger.info(
                f"Post-sync preferences cleanup: {cleared} user(s) had an unknown server cleared."
            )

        await bot.tree.sync()

        await interaction.followup.send("✅ Configuration rechargée avec succès!")
        await bot._send_bot_log(
            f"🔄 Configuration rechargée par {interaction.user.display_name}"
        )
        logger.info(f"Configuration rechargée par {interaction.user}")

    except Exception as e:
        await interaction.followup.send(
            f"❌ Erreur lors du rechargement de la configuration : {str(e)}"
        )
        await bot._send_bot_log(
            f"🔴 Erreur lors du rechargement par {interaction.user.display_name} : {str(e)}"
        )
        logger.error(f"Erreur lors du rechargement de la configuration : {e}")


@bot.tree.command(name="status", description="Affiche le statut des serveurs")
async def server_status(interaction: discord.Interaction):
    """Commande pour afficher le statut des serveurs"""
    embed = discord.Embed(title="Statut des serveurs Minecraft", color=0x00ff00)

    if not bot.servers:
        embed.add_field(name="Aucun serveur", value="Aucun serveur configuré", inline=False)
    else:
        for name, server in bot.servers.items():
            status = "🟢 En ligne" if server.is_running else "🔴 Hors ligne"
            embed.add_field(name=name, value=status, inline=True)

    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not os.path.exists('config.json'):
        print("Erreur : fichier config.json non trouvé")
        print("Veuillez créer le fichier de configuration avant de lancer le bot")
        sys.exit(1)

    try:
        config = load_config()
        token = config.get('bot_token')

        if not token:
            print("Erreur : token du bot non spécifié dans la configuration")
            sys.exit(1)

        bot.run(token)

    except KeyboardInterrupt:
        print("\nArrêt du bot demandé par l'utilisateur")
        logger.info("Arrêt du bot demandé par l'utilisateur")
    except Exception as e:
        print(f"Erreur critique : {e}")
        logger.error(f"Erreur critique : {e}")
        sys.exit(1)