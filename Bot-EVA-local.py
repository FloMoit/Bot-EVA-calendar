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
    """Mention cliquable : affiche le pseudo du serveur et ouvre le profil Discord."""
    return f"<@{p['id']}>"

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
    """'22:00', '22h00', '22h', '22', '22.30', '22 h 30' -> (h, m)"""
    s = heure_str.strip().lower().replace(" ", "")
    for sep in ("h", ".", ":"):
        s = s.replace(sep, ":")
    if s.endswith(":"):
        s = s[:-1]
    parts = s.split(":")
    if not (1 <= len(parts) <= 2) or not all(p.isdigit() for p in parts):
        raise ValueError
    h = int(parts[0])
    m = int(parts[1]) if len(parts) == 2 else 0
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError
    return h, m

def parse_date(date_str):
    """'14/10/2026', '14/10/26', '14/10', '14-10', '14 octobre', 'mercredi 14 oct'
    -> (jour, mois, annee ou None)"""
    s = date_str.strip().lower().replace(",", " ")
    for sep in ("-", ".", " "):
        s = s.replace(sep, "/")
    parts = [p for p in s.split("/") if p and p not in JOURS_FR]
    if len(parts) not in (2, 3):
        raise ValueError
    jour = int(parts[0])
    mois = int(parts[1]) if parts[1].isdigit() else MOIS_FR.get(parts[1])
    if mois is None:
        raise ValueError
    annee = None
    if len(parts) == 3:
        annee = int(parts[2])
        if annee < 100:
            annee += 2000
    return jour, mois, annee

def parse_date_heure(date_str, heure_str):
    h, m = parse_heure(heure_str)
    jour, mois, annee = parse_date(date_str)
    if annee is not None:
        return datetime(annee, mois, jour, h, m, tzinfo=PARIS)
    now = datetime.now(PARIS)
    debut = datetime(now.year, mois, jour, h, m, tzinfo=PARIS)
    if debut < now - timedelta(days=1):
        debut = debut.replace(year=now.year + 1)
    return debut

def build_team_embed(ev):
    ts = ev["start_ts"]
    n = ev["nb_sessions"]
    d = ev["duree"]
    horaires = " · ".join(f"<t:{ts + i * d * 60}:t>" for i in range(n))
    orga = f"<@{ev['organisateur_id']}>" if ev.get("organisateur_id") else ev["organisateur"]

    embed = discord.Embed(
        title=f"🎮 {ev['titre']}",
        description=(
            f"**Organisé par** {orga}\n\n"
            f"**Description**\n{ev['description']}\n\n"
            f"**Quand**\n<t:{ts}:F> · <t:{ts}:R>\n\n"
            f"**Sessions ({n} × {d}min)**\n{horaires}\n\n"
            f"**[👉 Clique ici pour réserver ta session]({BOOKING_URL})**"
        ),
        color=0x2ECC71
    )

    presents = ev["presents"]
    places = ev.get("places", 8)
    liste = "\n".join(f"{i}. {joueur_lien(p)}" for i, p in enumerate(presents, 1))
    embed.add_field(
        name=f"✅ Inscrits ({len(presents)}/{places})",
        value=(liste or "_Personne pour l'instant_")[:1024],
        inline=False
    )
    if ev["absents"]:
        embed.add_field(
            name="😴 Pas dispo",
            value="\n".join(f"• {joueur_lien(p)}" for p in ev["absents"])[:1024],
            inline=False
        )
    if ev["annules"]:
        embed.add_field(
            name="❌ Ne vient plus",
            value="\n".join(f"• {joueur_lien(p)}" for p in ev["annules"])[:1024],
            inline=False
        )
    embed.set_footer(text="Clique sur un bouton pour répondre")
    return embed

def retirer_team(ev, user_id):
    for cle in ("presents", "absents", "annules"):
        ev[cle] = [p for p in ev[cle] if p["id"] != user_id]

async def envoyer_dm_complet(user_id, ev, lien_annonce):
    ts = ev["start_ts"]
    joueurs = "\n".join(f"{i}. {p['pseudo']}" for i, p in enumerate(ev["presents"], 1))
    embed = discord.Embed(
        title=f"✅ Session complète pour EVA : {ev['titre']}",
        description=(
            f"📅 <t:{ts}:F>\n\n"
            f"**Joueurs ({len(ev['presents'])}/{ev.get('places', 8)})**\n{joueurs}\n\n"
            f"**[👉 Clique ici pour réserver ta session]({BOOKING_URL})**\n\n"
            f"**[💬 Voir l'organisation de la partie sur Discord]({lien_annonce})**"
        ),
        color=0x2ECC71
    )
    try:
        user = bot.get_user(int(user_id)) or await bot.fetch_user(int(user_id))
        await user.send(embed=embed)
    except discord.HTTPException as e:
        print(f"⚠️ DM impossible à {user_id} (MP fermés ?) : {e}")

class TeamView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _repondre(self, interaction, liste):
        ev = team_events.get(str(interaction.message.id))
        if not ev:
            await interaction.response.send_message("Cette session n'existe plus.", ephemeral=True)
            return
        user_id = str(interaction.user.id)
        if liste == "presents":
            deja = any(p["id"] == user_id for p in ev["presents"])
            if not deja and len(ev["presents"]) >= ev.get("places", 8):
                await interaction.response.send_message("La session est complète !", ephemeral=True)
                return
        retirer_team(ev, user_id)
        if liste:
            ev[liste].append({"id": user_id, "pseudo": pseudo_de(interaction.user)})
        await interaction.response.edit_message(embed=build_team_embed(ev), view=TeamView())
        save_team_events()

        # Session pleine : DM à tous la 1re fois, puis à chaque nouvel arrivant
        if liste == "presents" and not deja and len(ev["presents"]) >= ev.get("places", 8):
            if not ev.get("dm_complet"):
                ev["dm_complet"] = True
                save_team_events()
                cibles = [p["id"] for p in ev["presents"]]
            else:
                cibles = [user_id]
            for uid in cibles:
                await envoyer_dm_complet(uid, ev, interaction.message.jump_url)

    @discord.ui.button(label="✅ Présent", style=discord.ButtonStyle.success, custom_id="team_present")
    async def present(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._repondre(interaction, "presents")

    @discord.ui.button(label="🚪 Sortir", style=discord.ButtonStyle.secondary, custom_id="team_nodispo")
    async def nodispo(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._repondre(interaction, None)

    @discord.ui.button(label="❌ Je ne viens plus", style=discord.ButtonStyle.danger, custom_id="team_leave")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._repondre(interaction, "annules")

DESCRIPTIONS_PRESETS = ["Mix chill", "Train", "Split"]

@app_commands.command(name="orga", description="Créer une session EVA pour la team")
@app_commands.describe(
    date="Choisis dans la liste ou tape JJ/MM (année en cours ajoutée)",
    heure="Heure de début (ex : 22, 22h, 22h10, 22:10, 22.10)",
    sessions="Combien de sessions à partir de l'heure de début ?",
    description="Mix chill, Train, Split… ou tape ton propre texte",
    duree="Durée d'une session en minutes (défaut : 40)",
    titre="Titre de l'annonce (défaut : Session EVA)",
    places="Nombre de places (défaut : 8)",
)
@app_commands.choices(sessions=[
    app_commands.Choice(name="1 session", value=1),
    app_commands.Choice(name="2 sessions", value=2),
    app_commands.Choice(name="3 sessions", value=3),
    app_commands.Choice(name="4 sessions", value=4),
])
async def session_cmd(
    interaction: discord.Interaction,
    date: str,
    heure: str,
    sessions: app_commands.Choice[int],
    description: str,
    duree: app_commands.Range[int, 10, 180] = 40,
    titre: str = "Session EVA",
    places: app_commands.Range[int, 1, 50] = 8,
):
    try:
        debut = parse_date_heure(date, heure)
    except ValueError:
        await interaction.response.send_message(
            f"Je n'ai pas compris la date `{date}` ou l'heure `{heure}`.\n"
            f"Exemples : date `14/10/2026` ou `14/10`, heure `22`, `22h10` ou `22:10`",
            ephemeral=True
        )
        return

    ev = {
        "titre": titre,
        "organisateur": pseudo_de(interaction.user),
        "organisateur_id": str(interaction.user.id),
        "description": description.strip() or "Mix chill",
        "start_ts": int(debut.timestamp()),
        "nb_sessions": sessions.value,
        "duree": duree,
        "places": places,
        "presents": [],
        "absents": [],
        "annules": [],
    }
    await interaction.response.send_message(embed=build_team_embed(ev), view=TeamView())
    msg = await interaction.original_response()
    ev["channel_id"] = msg.channel.id
    ev["thread_id"] = await creer_fil(msg, f"{titre} {debut.strftime('%d/%m/%Y %H:%M')}")
    team_events[str(msg.id)] = ev
    save_team_events()

JOURS_COURTS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]

@session_cmd.autocomplete("date")
async def date_autocomplete(interaction: discord.Interaction, current: str):
    """Pré-remplit la date : les 14 prochains jours, ou la date tapée complétée avec l'année."""
    tape = current.strip()
    options = []

    if tape:
        try:
            jour, mois, annee = parse_date(tape)
            if annee is None:
                debut = parse_date_heure(tape, "0")
                annee = debut.year
            d = datetime(annee, mois, jour)
            valeur = d.strftime("%d/%m/%Y")
            options.append(app_commands.Choice(name=f"{JOURS_COURTS[d.weekday()]} {valeur}", value=valeur))
        except (ValueError, TypeError):
            pass

    aujourd_hui = datetime.now(PARIS).date()
    for i in range(14):
        d = aujourd_hui + timedelta(days=i)
        valeur = d.strftime("%d/%m/%Y")
        if tape and not valeur.startswith(tape) and tape not in valeur:
            continue
        if any(o.value == valeur for o in options):
            continue
        prefixe = "Aujourd'hui" if i == 0 else "Demain" if i == 1 else JOURS_COURTS[d.weekday()]
        options.append(app_commands.Choice(name=f"{prefixe} {valeur}", value=valeur))

    if tape and not options:
        options.append(app_commands.Choice(name=f"✏️ {tape}"[:100], value=tape[:100]))
    return options[:25]

@session_cmd.autocomplete("description")
async def description_autocomplete(interaction: discord.Interaction, current: str):
    """Propose les présets, et garde ce que l'utilisateur tape comme choix libre."""
    tape = current.strip()
    choix = [p for p in DESCRIPTIONS_PRESETS if tape.lower() in p.lower()]
    options = [app_commands.Choice(name=p, value=p) for p in choix]
    if tape and tape.lower() not in [p.lower() for p in DESCRIPTIONS_PRESETS]:
        options.append(app_commands.Choice(name=f"✏️ {tape}"[:100], value=tape[:100]))
    return options[:25]

if TEAM_GUILD:
    bot.tree.add_command(session_cmd, guild=TEAM_GUILD)
else:
    bot.tree.add_command(session_cmd)

# ── Nettoyage J+1 : annonce + fil supprimés 24h après la fin de la session ──
async def supprimer_salon_ou_message(channel_id, message_id=None):
    try:
        salon = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        if message_id is None:
            await salon.delete()
        else:
            msg = await salon.fetch_message(message_id)
            await msg.delete()
    except discord.NotFound:
        pass
    except discord.HTTPException as e:
        print(f"⚠️ Suppression impossible ({channel_id}/{message_id}) : {e}")

@tasks.loop(minutes=30)
async def nettoyage_j1():
    maintenant = datetime.now(timezone.utc).timestamp()
    a_supprimer = []
    for mid, ev in team_events.items():
        fin = ev["start_ts"] + ev["nb_sessions"] * ev["duree"] * 60
        if maintenant > fin + 24 * 3600:
            a_supprimer.append(mid)
    for mid in a_supprimer:
        ev = team_events.pop(mid)
        if ev.get("thread_id"):
            await supprimer_salon_ou_message(ev["thread_id"])
        if ev.get("channel_id"):
            await supprimer_salon_ou_message(ev["channel_id"], int(mid))
    if a_supprimer:
        print(f"🧹 {len(a_supprimer)} session(s) supprimée(s) (J+1)")
        save_team_events()

# ── Démarrage ──────────────────────────────────────────────────────────────
_deja_pret = False

@bot.event
async def on_ready():
    global _deja_pret
    if _deja_pret:
        return
    _deja_pret = True

    await bot.tree.sync()
    if TEAM_GUILD:
        try:
            await bot.tree.sync(guild=TEAM_GUILD)
        except discord.HTTPException as e:
            print(f"⚠️ Sync serveur team impossible (bot pas invité ?) : {e}")
    else:
        # Supprime les anciennes /orga réservées à un serveur (évite les doublons)
        for g in bot.guilds:
            try:
                await bot.tree.sync(guild=g)
            except discord.HTTPException:
                pass

    purge_old_team_events()
    bot.add_view(TeamView())
    if not nettoyage_j1.is_running():
        nettoyage_j1.start()
    print(f"✅ Bot EVA connecté : {bot.user}")
    print(f"💾 Stockage : {'GitHub (' + GITHUB_REPO + ')' if USE_GITHUB else 'fichiers locaux'}")
    print(f"📋 Pseudos chargés : {len(pseudos_eva)} joueur(s)")
    print(f"🎮 Sessions team actives : {len(team_events)}")
    print(f"🏠 Serveur team : {TEAM_GUILD_ID or 'non défini (/orga partout)'}")

# ── Mini serveur web (inutile sur Google Cloud, sans effet) ────────────────
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot EVA en ligne ✅"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

def keep_alive():
    threading.Thread(target=run_web, daemon=True).start()

keep_alive()
bot.run(TOKEN)
