import discord
from discord import app_commands
from discord.ext import tasks
import json
import os
import random
import sys
import queue
import base64
import threading
import time
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# ═══════════════════════════════════════════════════════════════════════════
#  Réglages
# ═══════════════════════════════════════════════════════════════════════════
PARIS = ZoneInfo("Europe/Paris")
DUREE_SESSION = 40                      # une session EVA dure toujours 40 min
PLACES_MAX = 10                         # capacité max de l'arène
TEL_SALLE = "04 85 96 05 10"            # EVA Lyon Sud
DESCRIPTIONS_PRESETS = ["Mix chill", "Train", "Split"]

def lien_reservation(ts):
    """Lien EVA Lyon Sud qui ouvre directement le calendrier au jour de la session."""
    jour = datetime.fromtimestamp(ts, PARIS).strftime("%Y-%m-%d")
    return (
        "https://app.eva.gg/fr-FR/booking/calendar?locationId=52&gameIds=1&seatCount=1"
        "&isCompetitiveMode=true&origin=%2Fbooking%3FlocationId%3D52%26gameIds%3D1"
        f"%26seatCount%3D1%26isCompetitiveMode%3Dtrue&currentDate={jour}"
    )

# ═══════════════════════════════════════════════════════════════════════════
#  Configuration (fichier .env sur le serveur)
#    DISCORD_TOKEN  : token du bot (obligatoire)
#    GITHUB_TOKEN + GITHUB_REPO : stockage sur GitHub (sinon fichier local)
# ═══════════════════════════════════════════════════════════════════════════
TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    print("ERREUR : DISCORD_TOKEN manquant dans le fichier .env")
    sys.exit(1)

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")            # ex : "Gaurage/Bot-EVA-data"
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
USE_GITHUB = bool(GITHUB_TOKEN and GITHUB_REPO)

FICHIER = "team_events.json"
FICHIER_LOCAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), FICHIER)

# ═══════════════════════════════════════════════════════════════════════════
#  Stockage des sessions : GitHub ou fichier local
# ═══════════════════════════════════════════════════════════════════════════
def _gh_headers():
    return {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}

def _gh_url():
    return f"https://api.github.com/repos/{GITHUB_REPO}/contents/{FICHIER}"

_sha = None  # version du fichier sur GitHub (évite une lecture avant chaque sauvegarde)

def github_load():
    """Réessaie 3 fois ; si GitHub reste injoignable, arrête le bot (il redémarre
    tout seul 30 s plus tard) plutôt que de démarrer à vide et d'écraser les données."""
    global _sha
    for _ in range(3):
        try:
            r = requests.get(_gh_url(), headers=_gh_headers(),
                             params={"ref": GITHUB_BRANCH}, timeout=15)
            if r.status_code == 200:
                data = r.json()
                _sha = data["sha"]
                decoded = base64.b64decode(data["content"]).decode("utf-8")
                return json.loads(decoded) if decoded.strip() else {}
            if r.status_code == 404:
                return {}
            print(f"⚠️ GitHub load : {r.status_code} {r.text}")
        except Exception as e:
            print(f"⚠️ Erreur GitHub load : {e}")
        time.sleep(5)
    print("❌ Impossible de lire les données sur GitHub : arrêt pour les protéger")
    sys.exit(1)

def _lire_sha():
    r = requests.get(_gh_url(), headers=_gh_headers(),
                     params={"ref": GITHUB_BRANCH}, timeout=15)
    return r.json()["sha"] if r.status_code == 200 else None

def github_write(json_str):
    """Une seule requête par sauvegarde ; relit la version seulement en cas de conflit."""
    global _sha
    contenu = base64.b64encode(json_str.encode("utf-8")).decode("utf-8")
    for _ in range(2):
        try:
            payload = {"message": f"update {FICHIER}", "content": contenu, "branch": GITHUB_BRANCH}
            if _sha:
                payload["sha"] = _sha
            r = requests.put(_gh_url(), headers=_gh_headers(), json=payload, timeout=15)
            if r.status_code in (200, 201):
                _sha = r.json()["content"]["sha"]
                return
            if r.status_code in (409, 422):   # version périmée : on la relit et on réessaie
                _sha = _lire_sha()
                continue
            print(f"⚠️ GitHub save : {r.status_code} {r.text}")
            return
        except Exception as e:
            print(f"⚠️ Erreur GitHub save : {e}")
            return

# Sauvegarde en arrière-plan : si plusieurs sauvegardes arrivent d'un coup
# (plusieurs clics), seule la plus récente est envoyée à GitHub.
_save_queue = queue.Queue()

def _save_worker():
    while True:
        json_str = _save_queue.get()
        while not _save_queue.empty():   # on saute les versions déjà dépassées
            json_str = _save_queue.get_nowait()
        github_write(json_str)

if USE_GITHUB:
    threading.Thread(target=_save_worker, daemon=True).start()

def load_team_events():
    if USE_GITHUB:
        return github_load()
    if os.path.exists(FICHIER_LOCAL):
        with open(FICHIER_LOCAL, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_team_events():
    if USE_GITHUB:
        _save_queue.put(json.dumps(team_events, ensure_ascii=False))
    else:
        with open(FICHIER_LOCAL, "w", encoding="utf-8") as f:
            json.dump(team_events, f, ensure_ascii=False)

team_events = load_team_events()

# ═══════════════════════════════════════════════════════════════════════════
#  Bot
# ═══════════════════════════════════════════════════════════════════════════
bot = discord.Client(intents=discord.Intents.default())
tree = app_commands.CommandTree(bot)

def joueur_lien(p):
    """Mention cliquable : affiche le pseudo du serveur et ouvre le profil Discord."""
    return f"<@{p['id']}>"

def liste_champ(lignes, vide="_Personne pour l'instant_"):
    """Assemble des lignes sans dépasser la limite Discord (1024) ni couper un pseudo."""
    texte = ""
    for i, ligne in enumerate(lignes):
        suite = f"\n… et {len(lignes) - i} autre(s)"
        if len(texte) + len(ligne) + 1 + len(suite) > 1024:
            return texte + suite
        texte += ("\n" if texte else "") + ligne
    return texte or vide

def horaires_sessions(ev):
    ts, n = ev["start_ts"], ev.get("nb_sessions", 1)
    d = ev.get("duree", DUREE_SESSION)
    return " · ".join(f"<t:{ts + i * d * 60}:t>" for i in range(n))

async def envoyer_mp(user_id, embed):
    try:
        user = bot.get_user(int(user_id)) or await bot.fetch_user(int(user_id))
        await user.send(embed=embed)
    except discord.HTTPException as e:
        print(f"⚠️ MP impossible à {user_id} (MP fermés ?) : {e}")

async def creer_fil(msg, nom):
    """Crée un fil de discussion sous l'annonce. Renvoie l'id du fil ou None."""
    try:
        fil = await msg.create_thread(name=nom[:100], auto_archive_duration=1440)
        return fil.id
    except discord.HTTPException as e:
        print(f"⚠️ Fil impossible (permission 'Créer des fils publics' ?) : {e}")
        return None

# ── Lecture de la date et de l'heure tapées dans /orga ──────────────────────
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
JOURS_COURTS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]

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

# ═══════════════════════════════════════════════════════════════════════════
#  Annonce et MP
# ═══════════════════════════════════════════════════════════════════════════
def build_team_embed(ev):
    ts = ev["start_ts"]
    n = ev.get("nb_sessions", 1)
    d = ev.get("duree", DUREE_SESSION)
    orga = f"<@{ev['organisateur_id']}>" if ev.get("organisateur_id") else ev.get("organisateur", "?")
    embed = discord.Embed(
        title=f"🎮 {ev['titre']}"[:256],
        description=(
            f"**Organisé par** {orga}\n\n"
            f"**Description**\n{ev['description'][:1000]}\n\n"
            f"**Quand**\n<t:{ts}:F> · <t:{ts}:R>\n\n"
            f"**Sessions ({n} × {d}min)**\n{horaires_sessions(ev)}\n\n"
            f"**[👉 Clique ici pour réserver ta session]({lien_reservation(ts)})**"
        ),
        color=0x2ECC71
    )
    presents = ev["presents"]
    embed.add_field(
        name=f"✅ Inscrits ({len(presents)}/{ev.get('places', 8)})",
        value=liste_champ([f"{i}. {joueur_lien(p)}" for i, p in enumerate(presents, 1)]),
        inline=False
    )
    if ev.get("attente"):
        embed.add_field(
            name=f"⏳ File d'attente ({len(ev['attente'])})",
            value=liste_champ([f"{i}. {joueur_lien(p)}" for i, p in enumerate(ev["attente"], 1)]),
            inline=False
        )
    embed.set_footer(text="Clique sur un bouton pour répondre")
    return embed

async def envoyer_dm_complet(user_id, ev, lien_annonce, promu=False):
    ts = ev["start_ts"]
    joueurs = "\n".join(f"{i}. {p['pseudo']}" for i, p in enumerate(ev["presents"], 1))
    titre = (f"🎉 Une place s'est libérée, tu es inscrit : {ev['titre']}" if promu
             else f"✅ Session complète pour EVA : {ev['titre']}")
    embed = discord.Embed(
        title=titre[:256],
        description=(
            f"📅 <t:{ts}:F>\n\n"
            f"**Joueurs ({len(ev['presents'])}/{ev.get('places', 8)})**\n{joueurs}\n\n"
            f"**[👉 Clique ici pour réserver ta session]({lien_reservation(ts)})**\n\n"
            f"**[💬 Voir l'organisation de la partie sur Discord]({lien_annonce})**"
        )[:4096],
        color=0x2ECC71
    )
    await envoyer_mp(user_id, embed)

async def envoyer_rappel(user_id, ev, lien_annonce):
    n = ev.get("nb_sessions", 1)
    description = (
        f"🕙 {horaires_sessions(ev)} ({n} session{'s' if n > 1 else ''})\n\n"
        f"🚗 **Un retard ? Appelle la salle : {TEL_SALLE}**"
    )
    if lien_annonce:
        description += f"\n\n**[💬 Voir l'organisation de la partie sur Discord]({lien_annonce})**"
    embed = discord.Embed(
        title=f"⏰ RAPPEL : Ta partie à EVA LYON SUD c'est dans 1h : {ev['titre']}"[:256],
        description=description,
        color=0xF1C40F
    )
    await envoyer_mp(user_id, embed)

# ═══════════════════════════════════════════════════════════════════════════
#  Boutons : Présent / Sortir / File d'attente
# ═══════════════════════════════════════════════════════════════════════════
def retirer_team(ev, user_id):
    for cle in ("presents", "attente"):
        ev[cle] = [p for p in ev.get(cle, []) if p["id"] != user_id]

class TeamView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _repondre(self, interaction, action):
        ev = team_events.get(str(interaction.message.id))
        if not ev:
            await interaction.response.send_message("Cette session n'existe plus.", ephemeral=True)
            return
        ev.setdefault("attente", [])
        user_id = str(interaction.user.id)
        joueur = {"id": user_id, "pseudo": interaction.user.display_name}
        places = ev.get("places", 8)
        inscrit = any(p["id"] == user_id for p in ev["presents"])
        en_attente = any(p["id"] == user_id for p in ev["attente"])
        promu = None
        info = None

        if action in ("presents", "attente"):
            if inscrit or (en_attente and len(ev["presents"]) >= places):
                # Déjà à sa place : on ne touche à rien
                await interaction.response.defer()
                return
            retirer_team(ev, user_id)
            if len(ev["presents"]) < places:
                ev["presents"].append(joueur)
            else:
                ev["attente"].append(joueur)
                info = (f"⏳ Session complète : tu es en file d'attente (position {len(ev['attente'])}). "
                        f"Tu recevras un MP si une place se libère.")
        else:  # Sortir
            retirer_team(ev, user_id)
            # Une place se libère : le 1er de la file d'attente la prend
            if inscrit and ev["attente"] and len(ev["presents"]) < places:
                promu = ev["attente"].pop(0)
                ev["presents"].append(promu)

        await interaction.response.edit_message(embed=build_team_embed(ev), view=TeamView())
        save_team_events()
        if info:
            await interaction.followup.send(info, ephemeral=True)

        # Session pleine : MP à chaque joueur qui ne l'a pas encore reçu
        if len(ev["presents"]) >= places:
            deja_prevenus = set(ev.get("dm_envoyes", []))
            cibles = [p["id"] for p in ev["presents"] if p["id"] not in deja_prevenus]
            if promu and promu["id"] not in cibles:
                cibles.append(promu["id"])
            if cibles:
                ev["dm_envoyes"] = sorted(deja_prevenus | set(cibles))
                save_team_events()
            for uid in cibles:
                await envoyer_dm_complet(uid, ev, interaction.message.jump_url,
                                         promu=bool(promu and uid == promu["id"]))

    @discord.ui.button(label="✅ Présent", style=discord.ButtonStyle.success, custom_id="team_present")
    async def present(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._repondre(interaction, "presents")

    @discord.ui.button(label="🚪 Sortir", style=discord.ButtonStyle.secondary, custom_id="team_nodispo")
    async def nodispo(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._repondre(interaction, None)

    @discord.ui.button(label="⏳ File d'attente", style=discord.ButtonStyle.primary, custom_id="team_attente")
    async def attente(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._repondre(interaction, "attente")

# ═══════════════════════════════════════════════════════════════════════════
#  Commande /orga
# ═══════════════════════════════════════════════════════════════════════════
@tree.command(name="orga", description="Créer une session EVA")
@app_commands.describe(
    date="Choisis dans la liste ou tape JJ/MM (année en cours ajoutée)",
    heure="Heure de début (ex : 22, 22h, 22h10, 22:10, 22.10)",
    sessions="Combien de sessions de 40 min à la suite ?",
    description="Mix chill, Train, Split… ou tape ton propre texte",
    titre="Titre de l'annonce (défaut : Session EVA)",
    places=f"Nombre de places, {PLACES_MAX} max (défaut : 8)",
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
    titre: app_commands.Range[str, 1, 100] = "Session EVA",
    places: app_commands.Range[int, 1, PLACES_MAX] = 8,
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
    if debut < datetime.now(PARIS) - timedelta(hours=1):
        await interaction.response.send_message(
            f"La date `{date}` à `{heure}` est déjà passée.", ephemeral=True
        )
        return

    ev = {
        "titre": titre,
        "organisateur_id": str(interaction.user.id),
        "description": description.strip()[:300] or "Mix chill",
        "start_ts": int(debut.timestamp()),
        "nb_sessions": sessions.value,
        "duree": DUREE_SESSION,
        "places": places,
        "cree_ts": int(datetime.now(timezone.utc).timestamp()),
        "presents": [],
        "attente": [],
    }
    await interaction.response.send_message(embed=build_team_embed(ev), view=TeamView())
    msg = await interaction.original_response()
    ev["channel_id"] = msg.channel.id
    ev["guild_id"] = msg.guild.id if msg.guild else None
    ev["thread_id"] = await creer_fil(msg, f"{titre} {debut.strftime('%d/%m/%Y %H:%M')}")
    team_events[str(msg.id)] = ev
    save_team_events()

@session_cmd.autocomplete("date")
async def date_autocomplete(interaction: discord.Interaction, current: str):
    """Propose les 14 prochains jours, ou la date tapée complétée avec l'année."""
    tape = current.strip()
    options = []
    if tape:
        try:
            jour, mois, annee = parse_date(tape)
            if annee is None:
                annee = parse_date_heure(tape, "0").year
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
    options = [app_commands.Choice(name=p, value=p)
               for p in DESCRIPTIONS_PRESETS if tape.lower() in p.lower()]
    if tape and tape.lower() not in [p.lower() for p in DESCRIPTIONS_PRESETS]:
        options.append(app_commands.Choice(name=f"✏️ {tape}"[:100], value=tape[:100]))
    return options[:25]

# ═══════════════════════════════════════════════════════════════════════════
#  Tâches automatiques : rappel 1h avant + nettoyage J+1
# ═══════════════════════════════════════════════════════════════════════════
async def lien_de_annonce(mid, ev):
    guild_id = ev.get("guild_id")
    if not guild_id and ev.get("channel_id"):
        try:
            salon = bot.get_channel(ev["channel_id"]) or await bot.fetch_channel(ev["channel_id"])
            guild_id = salon.guild.id
        except Exception:
            return None
    if guild_id and ev.get("channel_id"):
        return f"https://discord.com/channels/{guild_id}/{ev['channel_id']}/{mid}"
    return None

@tasks.loop(minutes=1)
async def rappels_1h():
    """MP de rappel aux inscrits 1h avant le début (une seule fois)."""
    maintenant = datetime.now(timezone.utc).timestamp()
    for mid, ev in list(team_events.items()):
        try:
            debut = ev["start_ts"]
            if ev.get("rappel_envoye") or not (debut - 3600 <= maintenant < debut):
                continue
            ev["rappel_envoye"] = True
            save_team_events()
            if ev.get("cree_ts", 0) > debut - 3600:
                continue  # session créée moins d'1h avant : pas de rappel
            lien = await lien_de_annonce(mid, ev)
            for p in ev.get("presents", []):
                await envoyer_rappel(p["id"], ev, lien)
            print(f"⏰ Rappel envoyé pour la session {mid} ({len(ev.get('presents', []))} joueur(s))")
        except Exception as e:
            print(f"⚠️ Rappel session {mid} : {e}")

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
    """Supprime l'annonce et son fil 24h après la fin de la session."""
    maintenant = datetime.now(timezone.utc).timestamp()
    a_supprimer = []
    for mid, ev in list(team_events.items()):
        try:
            fin = ev["start_ts"] + ev.get("nb_sessions", 1) * ev.get("duree", DUREE_SESSION) * 60
            if maintenant > fin + 24 * 3600:
                a_supprimer.append(mid)
        except Exception as e:
            print(f"⚠️ Session {mid} illisible, supprimée : {e}")
            a_supprimer.append(mid)
    for mid in a_supprimer:
        ev = team_events.pop(mid, {})
        try:
            if ev.get("thread_id"):
                await supprimer_salon_ou_message(ev["thread_id"])
            if ev.get("channel_id"):
                await supprimer_salon_ou_message(ev["channel_id"], int(mid))
        except Exception as e:
            print(f"⚠️ Nettoyage session {mid} : {e}")
    if a_supprimer:
        print(f"🧹 {len(a_supprimer)} session(s) supprimée(s) (J+1)")
        save_team_events()

# ═══════════════════════════════════════════════════════════════════════════
#  Réponse automatique quand quelqu'un écrit au bot en MP
# ═══════════════════════════════════════════════════════════════════════════
REPONSES_MP = [
    "🤖 Bip boup… Je suis un bot, je ne sais que compter jusqu'à 10 joueurs. Personne ne lit ce message !",
    "📭 Ton message vient de partir dans le vide intersidéral. Personne ne lit les MP du bot 👀",
    "🥽 Désolé, je suis en pleine partie dans l'arène, je ne lis pas mes messages.",
    "🎯 Joli tir, mais tu as visé le bot ! Aucun point marqué.",
    "🛡️ Message bloqué derrière un mur. Comme toi au dernier round.",
    "⚡ Tu rushes le bot ? Mauvaise idée, je ne respawn jamais.",
    "📡 Connexion établie… avec personne. Ce message finira dans le néant.",
    "🔋 Ma batterie de lecture est à 0 %. Depuis toujours.",
    "🎮 Tu viens de débloquer le succès : « Parler à un robot ». Récompense : rien.",
    "🧱 Tu parles à un mur. Un mur très bien codé, mais un mur.",
    "🕶️ Je lirais bien ton message, mais j'ai encore mon casque VR sur la tête.",
    "💥 Headshot ! Ah non, c'était juste un MP.",
    "🏃 Je cours trop vite pour lire les messages. C'est ça, être un bot de rush.",
    "📜 Ton message a été transmis au Game Master imaginaire. Il ne répond jamais.",
    "🔁 Tu peux réessayer autant que tu veux, je suis programmé pour ne rien comprendre.",
    "🤫 Chut… le bot fait la sieste entre deux sessions.",
    "🎲 J'ai lancé un dé pour savoir si je lisais ton message. Résultat : non.",
    "🧠 Mon cerveau fait 600 lignes de code. Aucune ne sert à lire tes messages.",
    "🚀 Message envoyé en orbite. On le retrouvera peut-être dans 10 000 ans.",
    "🏆 Bravo, tu es officiellement la personne la plus curieuse du serveur. Ça ne change rien, mais bravo.",
]
AIDE_MP = ("❓ Une question ? Contacte les **Game Masters** sur le Discord d'EVA Lyon Sud, "
           f"ou appelle la salle : **{TEL_SALLE}**")
DEJA_AIDE = {}         # dernier jour où chaque personne a reçu le message d'aide
PAQUETS = {}           # phrases restantes à envoyer, par personne

def prochaine_phrase(uid):
    """Les 19 premières phrases dans un ordre aléatoire, puis la dernière (🏆) en 20e.
    Une fois les 20 envoyées, on remélange et on recommence."""
    if not PAQUETS.get(uid):
        paquet = REPONSES_MP[:-1]
        random.shuffle(paquet)
        PAQUETS[uid] = paquet + [REPONSES_MP[-1]]
    return PAQUETS[uid].pop(0)

@bot.event
async def on_message(message):
    if message.author.bot or message.guild is not None:
        return  # on ne répond qu'aux MP envoyés par des humains
    uid = message.author.id
    texte = prochaine_phrase(uid)
    aujourd_hui = datetime.now(PARIS).date()
    if DEJA_AIDE.get(uid) != aujourd_hui:
        DEJA_AIDE[uid] = aujourd_hui  # l'aide revient au 1er MP de chaque journée
        texte += f"\n\n{AIDE_MP}"
    try:
        await message.channel.send(texte)
    except discord.HTTPException:
        pass

# ═══════════════════════════════════════════════════════════════════════════
#  Démarrage
# ═══════════════════════════════════════════════════════════════════════════
_deja_pret = False

@bot.event
async def on_ready():
    global _deja_pret
    if _deja_pret:
        return
    _deja_pret = True

    await tree.sync()
    bot.add_view(TeamView())
    if not nettoyage_j1.is_running():
        nettoyage_j1.start()
    if not rappels_1h.is_running():
        rappels_1h.start()
    print(f"✅ Bot EVA connecté : {bot.user}")
    print(f"💾 Stockage : {'GitHub (' + GITHUB_REPO + ')' if USE_GITHUB else 'fichier local'}")
    print(f"🎮 Sessions actives : {len(team_events)}")

bot.run(TOKEN)
