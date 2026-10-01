import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import os
import sys
import queue
import base64
import threading
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from flask import Flask

# ── Dossier de base (utile pour le mode local) ─────────────────────────────
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(BASE_DIR, "config.txt")
PSEUDOS_FILE = os.path.join(BASE_DIR, "pseudos.json")
TEAM_EVENTS_FILE = os.path.join(BASE_DIR, "team_events.json")

PARIS = ZoneInfo("Europe/Paris")

# ── Token Discord : variable d'environnement ou config.txt (local) ─────────
def load_token_from_config():
    if not os.path.exists(CONFIG_FILE):
        return None
    with open(CONFIG_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if line.startswith("TOKEN="):
                token = line.split("=", 1)[1].strip()
                if token:
                    return token
    return None

TOKEN = os.environ.get("DISCORD_TOKEN") or load_token_from_config()

if not TOKEN:
    print("=" * 50)
    print("ERREUR : aucun token Discord trouvé.")
    print("Serveur : ajoute DISCORD_TOKEN dans le fichier .env")
    print("En local : mets TOKEN=ton_token dans config.txt")
    print("=" * 50)
    sys.exit(1)

# Serveur Discord de la team : la commande /orga n'apparaît que là
TEAM_GUILD_ID = os.environ.get("TEAM_GUILD_ID", "").strip()
TEAM_GUILD = discord.Object(id=int(TEAM_GUILD_ID)) if TEAM_GUILD_ID else None

# ── Stockage : GitHub ou fichiers locaux ───────────────────────────────────
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")            # ex : "Gaurage/Bot-EVA-data"
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
USE_GITHUB = bool(GITHUB_TOKEN and GITHUB_REPO)

def _gh_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

def _gh_url(filename):
    return f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"

def github_load(filename):
    try:
        r = requests.get(_gh_url(filename), headers=_gh_headers(),
                         params={"ref": GITHUB_BRANCH}, timeout=15)
        if r.status_code == 200:
            decoded = base64.b64decode(r.json()["content"]).decode("utf-8")
            return json.loads(decoded) if decoded.strip() else {}
        if r.status_code == 404:
            return {}
        print(f"⚠️ GitHub load {filename} : {r.status_code} {r.text}")
    except Exception as e:
        print(f"⚠️ Erreur github_load {filename} : {e}")
    return {}

def _github_write(filename, json_str):
    try:
        sha = None
        r = requests.get(_gh_url(filename), headers=_gh_headers(),
                         params={"ref": GITHUB_BRANCH}, timeout=15)
        if r.status_code == 200:
            sha = r.json()["sha"]
        payload = {
            "message": f"update {filename}",
            "content": base64.b64encode(json_str.encode("utf-8")).decode("utf-8"),
            "branch": GITHUB_BRANCH,
        }
        if sha:
            payload["sha"] = sha
        r = requests.put(_gh_url(filename), headers=_gh_headers(),
                         json=payload, timeout=15)
        if r.status_code not in (200, 201):
            print(f"⚠️ GitHub save {filename} : {r.status_code} {r.text}")
    except Exception as e:
        print(f"⚠️ Erreur github_write {filename} : {e}")

# File d'attente : écrit sur GitHub dans l'ordre, sans bloquer le bot
_save_queue = queue.Queue()

def _save_worker():
    while True:
        filename, json_str = _save_queue.get()
        _github_write(filename, json_str)
        _save_queue.task_done()

if USE_GITHUB:
    threading.Thread(target=_save_worker, daemon=True).start()

def _save(filename, local_path, data):
    if USE_GITHUB:
        _save_queue.put((filename, json.dumps(data, ensure_ascii=False)))
    else:
        with open(local_path, "w") as f:
            json.dump(data, f)

def _load(filename, local_path):
    if USE_GITHUB:
        return github_load(filename)
    if os.path.exists(local_path):
        with open(local_path, "r") as f:
            return json.load(f)
    return {}

# ── Bot ────────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

def load_pseudos():
    return _load("pseudos.json", PSEUDOS_FILE)

def save_pseudos(pseudos):
    _save("pseudos.json", PSEUDOS_FILE, pseudos)

def load_team_events():
    return _load("team_events.json", TEAM_EVENTS_FILE)

def save_team_events():
    _save("team_events.json", TEAM_EVENTS_FILE, team_events)

def purge_old_team_events():
    limite = datetime.now(timezone.utc).timestamp() - 30 * 24 * 3600
    to_delete = [mid for mid, e in team_events.items() if e.get("start_ts", 0) < limite]
    for mid in to_delete:
        del team_events[mid]
    if to_delete:
        print(f"🗑️ {len(to_delete)} session(s) team purgée(s) (> 30 jours)")
        save_team_events()

pseudos_eva = load_pseudos()
team_events = load_team_events()

def pseudo_de(user):
    return pseudos_eva.get(str(user.id), user.display_name)

def joueur_lien(p):
    """Mention cliquable (ouvre le profil Discord) + pseudo EVA."""
    return f"<@{p['id']}> ({p['pseudo']})"

async def creer_fil(msg, nom):
    """Crée un fil de discussion sous l'annonce. Renvoie l'id du fil ou None."""
    try:
        fil = await msg.create_thread(name=nom[:100], auto_archive_duration=1440)
        return fil.id
    except discord.HTTPException as e:
        print(f"⚠️ Fil impossible (permission 'Créer des fils publics' ?) : {e}")
        return None

BOOKING_URL = "https://app.eva.gg/fr-FR/booking?locationId=52&gameIds=1&seatCount=1&isCompetitiveMode=true"

# ═══════════════════════════════════════════════════════════════════════════
#  /orga : session interne de la team (une seule liste "Présents")
# ═══════════════════════════════════════════════════════════════════════════
MOIS_FR = {
    "janvier": 1, "janv": 1, "jan": 1,
    "fevrier": 2, "février": 2, "fevr": 2, "févr": 2, "fev": 2, "fév": 2,
    "mars": 3, "mar": 3,
    "avril": 4, "avr": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7, "juil": 7,
    "aout": 8, "août": 8,
    "septembre": 9, "sept": 9, "sep": 9,
    "octobre": 10, "oct": 10,
    "novembre": 11, "nov": 11,
    "decembre": 12, "décembre": 12, "dec": 12, "déc": 12,
}
JOURS_FR = {"lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"}

def parse_heure(heure_str):
    """'22:00',
