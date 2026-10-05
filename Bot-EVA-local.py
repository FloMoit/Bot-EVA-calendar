import discord
from discord import app_commands
from discord.ext import tasks
import json
import os
import random
import re
import sys
import queue
import base64
import threading
import time
import requests
from urllib.parse import urlencode
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# ═══════════════════════════════════════════════════════════════════════════
#  Réglages
# ═══════════════════════════════════════════════════════════════════════════
PARIS = ZoneInfo("Europe/Paris")
DUREE_SESSION = 40                      # une session EVA dure toujours 40 min
PLACES_MAX = 10                         # capacité max de l'arène
MAX_PAR_JOUR = 50                       # sessions créées par jour (anti-spam)
MAX_PAR_MOIS = 1500                     # sessions créées par mois (reste dans le gratuit Google)
PLACES_DEFAUT = 8                       # places par défaut dans /orga (modifiable à chaque fois)
DESCRIPTIONS_DEFAUT = ["Mix Chill", "Train", "Split"]
CREDIT = "-# *🤖 Bot développé par **Gaurage**, joueur de Lyon*"
CREDIT_COURT = "🤖 Bot développé par Gaurage, joueur de Lyon"
GARDER = object()  # « ne change pas ce réglage »
# Le nom de la salle, son téléphone et son identifiant EVA se règlent
# directement sur Discord avec /config (réservé aux admins du serveur).

def lien_reservation(ts, location_id):
    """Lien EVA qui ouvre directement le calendrier de la salle au jour de la session."""
    jour = datetime.fromtimestamp(ts, PARIS).strftime("%Y-%m-%d")
    return (
        f"https://app.eva.gg/fr-FR/booking/calendar?locationId={location_id}&gameIds=1&seatCount=1"
        f"&isCompetitiveMode=true&origin=%2Fbooking%3FlocationId%3D{location_id}%26gameIds%3D1"
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
CLE_STATS = "_compteurs"
CLE_BLAGUES = "_mp_blagues"   # réponses drôles envoyées en MP (effacées après 24h)   # rangé avec les sessions, mais ce n'est pas une session

def sessions():
    """Toutes les sessions (sans les compteurs)."""
    return [(mid, ev) for mid, ev in team_events.items() if not mid.startswith("_")]

def config_de(guild_id):
    """Réglages de la salle enregistrés avec /config pour ce serveur (ou None).
    Sans serveur connu (anciennes sessions) : la config s'il n'y en a qu'une."""
    if guild_id:
        return team_events.get(f"_config_{guild_id}")
    configs = [v for k, v in team_events.items() if k.startswith("_config_")]
    return configs[0] if len(configs) == 1 else None

def compteurs():
    """Nombre de sessions créées aujourd'hui et ce mois-ci (remis à zéro tout seul)."""
    now = datetime.now(PARIS)
    jour, mois = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
    st = team_events.setdefault(CLE_STATS, {})
    if st.get("jour") != jour:
        st["jour"], st["nb_jour"] = jour, 0
    if st.get("mois") != mois:
        st["mois"], st["nb_mois"] = mois, 0
    return st

# ═══════════════════════════════════════════════════════════════════════════
#  Bot
# ═══════════════════════════════════════════════════════════════════════════
# Jamais de @everyone / @here / rôle déclenché par un texte saisi par un joueur
bot = discord.Client(intents=discord.Intents.default(),
                     allowed_mentions=discord.AllowedMentions(everyone=False, roles=False))
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

class VueMP(discord.ui.View):
    """Bouton sous chaque MP du bot : efface tous les messages du bot dans la conversation."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🧹 Vider la conversation", style=discord.ButtonStyle.secondary, custom_id="mp_vider")
    async def vider(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        n = 0
        try:
            async for m in interaction.channel.history(limit=500):
                if m.author.id == bot.user.id:
                    try:
                        await m.delete()
                        n += 1
                    except discord.HTTPException:
                        pass
        except discord.HTTPException as e:
            print(f"⚠️ Historique MP illisible ({interaction.user.id}) : {e}")
        await interaction.followup.send(
            f"🧹 C'est fait : {n} message{'s' if n > 1 else ''} du bot effacé{'s' if n > 1 else ''}.\n"
            "ℹ️ Discord ne permet pas au bot d'effacer **tes** messages : survole-les → ⋯ → Supprimer.",
            ephemeral=True)

async def envoyer_mp(user_id, embed, ev=None):
    """Envoie un MP ; si une session est donnée, le retient pour l'effacer à J+1."""
    try:
        user = bot.get_user(int(user_id)) or await bot.fetch_user(int(user_id))
        msg = await user.send(embed=embed, view=VueMP())
    except discord.HTTPException as e:
        print(f"⚠️ MP impossible à {user_id} (MP fermés ?) : {e}")
        return None
    try:
        if ev is not None:
            ev.setdefault("mps", []).append([msg.channel.id, msg.id])
    except Exception as e:
        print(f"⚠️ MP envoyé mais non mémorisé ({user_id}) : {e}")
    return msg

async def supprimer_mp(channel_id, message_id):
    """Efface un MP envoyé par le bot (sans erreur s'il a déjà disparu)."""
    try:
        await bot.get_partial_messageable(int(channel_id)).get_partial_message(int(message_id)).delete()
    except discord.HTTPException:
        pass

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
    if not (1 <= len(parts) <= 2) or not all(p.isdecimal() for p in parts):
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
    mois = int(parts[1]) if parts[1].isdecimal() else MOIS_FR.get(parts[1])
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
def ligne_reservation(ev):
    cfg = config_de(ev.get("guild_id"))
    if not cfg:
        return ""
    return f"**[👉 Clique ici pour réserver ta session]({lien_reservation(ev['start_ts'], cfg['location_id'])})**"

def lien_google_agenda(ev):
    """Lien qui ouvre Google Agenda avec l'événement déjà rempli."""
    cfg = config_de(ev.get("guild_id")) or {}
    nom = cfg.get("nom", "EVA")
    debut = ev["start_ts"]
    fin = debut + ev.get("nb_sessions", 1) * ev.get("duree", DUREE_SESSION) * 60
    fmt = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    n = ev.get("nb_sessions", 1)
    heures = " · ".join(datetime.fromtimestamp(debut + i * ev.get("duree", DUREE_SESSION) * 60, PARIS).strftime("%H:%M")
                        for i in range(n))
    joueurs = ", ".join(p["pseudo"] for p in ev.get("presents", []))
    details = [f"{n} session{'s' if n > 1 else ''} de {ev.get('duree', DUREE_SESSION)} min : {heures}",
               f"Joueurs ({len(ev.get('presents', []))}/{ev.get('places', 8)}) : {joueurs or '—'}"]
    if cfg.get("telephone"):
        details.append(f"Un retard ? {cfg['telephone']}")
    params = {
        "action": "TEMPLATE",
        "text": f"{nom} · {ev.get('description', ev['titre'])[:100]}",
        "dates": f"{fmt(debut)}/{fmt(fin)}",
        "details": "\n".join(details),
        "location": nom,
    }
    return "https://calendar.google.com/calendar/render?" + urlencode(params)

def ligne_agenda(ev):
    return f"**[📅 Ajouter à mon Google Agenda]({lien_google_agenda(ev)})**"

def build_team_embed(ev):
    ts = ev["start_ts"]
    n = ev.get("nb_sessions", 1)
    d = ev.get("duree", DUREE_SESSION)
    orga = f"<@{ev['organisateur_id']}>" if ev.get("organisateur_id") else ev.get("organisateur", "?")
    # La description est déjà dans le titre ; on ne la répète que pour les anciennes annonces
    bloc_desc = "" if ev["titre"].startswith(ev["description"]) else f"**Description**\n{ev['description'][:1000]}\n\n"
    embed = discord.Embed(
        title=f"🎮 {ev['titre']}"[:256],
        description=(
            f"**Organisé par** {orga}\n\n"
            f"{bloc_desc}"
            f"**Quand**\n<t:{ts}:F> · <t:{ts}:R>\n\n"
            f"**Sessions ({n} × {d}min)**\n{horaires_sessions(ev)}\n\n"
            f"{ligne_reservation(ev)}\n\n{ligne_agenda(ev)}"
        ).strip() + "\n\u200b",   # ligne invisible : espace avant « Inscrits »
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
    n = ev.get("nb_sessions", 1)
    nom = (config_de(ev.get("guild_id")) or {}).get("nom", "EVA")
    joueurs = "\n".join(f"{i}. {p['pseudo']}" for i, p in enumerate(ev["presents"], 1))
    titre = "🎉 Une place s'est libérée, tu es inscrit !" if promu else "✅ Session complète !"
    liens = liens_session(ev, lien_annonce)
    embed = discord.Embed(
        title=titre,
        description=(
            f"**{nom}** · {ev.get('description', ev['titre'])}\n\n"
            f"📅 **Quand**\n<t:{ts}:F>\n\n"
            f"🕙 **Sessions**\n{horaires_sessions(ev)} ({n} × {ev.get('duree', DUREE_SESSION)} min)\n\n"
            f"👥 **Joueurs ({len(ev['presents'])}/{ev.get('places', 8)})**\n{joueurs}\n\n"
            + "\n\n".join(liens)
        )[:4096],
        color=0x2ECC71
    )
    await envoyer_mp(user_id, embed, ev)

async def envoyer_rappel(user_id, ev, lien_annonce):
    n = ev.get("nb_sessions", 1)
    cfg = config_de(ev.get("guild_id")) or {}
    nom = cfg.get("nom", "EVA")
    description = f"🕙 {horaires_sessions(ev)} ({n} session{'s' if n > 1 else ''})"
    if cfg.get("telephone"):
        description += f"\n\n🚗 **Un retard ? Appelle la salle : {cfg['telephone']}**"
    if lien_annonce:
        description += f"\n\n**[💬 Voir l'organisation sur Discord]({lien_annonce})**"
    embed = discord.Embed(
        title=f"⏰ RAPPEL : Ta partie à {nom.upper()} c'est dans 1h : {ev.get('description', ev['titre'])}"[:256],
        description=description,
        color=0xF1C40F
    )
    await envoyer_mp(user_id, embed, ev)

# ═══════════════════════════════════════════════════════════════════════════
#  Boutons : Présent / Sortir / File d'attente
# ═══════════════════════════════════════════════════════════════════════════
def retirer_team(ev, user_id):
    for cle in ("presents", "attente"):
        ev[cle] = [p for p in ev.get(cle, []) if p["id"] != user_id]

def peut_gerer(interaction, ev):
    """L'organisateur de la session ou un admin du serveur."""
    return str(interaction.user.id) == ev.get("organisateur_id") or interaction.permissions.manage_guild

def memoriser_temp(msg):
    """MP sans session (annulation…) : effacé 24h après l'envoi, comme les réponses drôles."""
    if msg is None:
        return
    liste = team_events.setdefault(CLE_BLAGUES, [])
    liste.append([msg.channel.id, msg.id, int(time.time())])
    del liste[:-500]

async def prevenir_complet(ev, lien_annonce, promus=()):
    """MP « place libérée » aux promus, et « session complète » à ceux qui ne l'ont pas encore."""
    deja = set(ev.get("dm_envoyes", []))
    cibles = [p["id"] for p in ev["presents"] if p["id"] not in deja] if len(ev["presents"]) >= ev.get("places", 8) else []
    for p in promus:
        if p["id"] not in cibles:
            cibles.append(p["id"])
    if cibles:
        ev["dm_envoyes"] = sorted(deja | set(cibles))
        save_team_events()
    ids_promus = {p["id"] for p in promus}
    for uid in cibles:
        await envoyer_dm_complet(uid, ev, lien_annonce, promu=uid in ids_promus)

def liens_session(ev, lien_annonce):
    return [l for l in (ligne_reservation(ev), ligne_agenda(ev),
                        f"**[💬 Voir l'organisation sur Discord]({lien_annonce})**" if lien_annonce else "") if l]

# ── Modifier une session (organisateur ou admin) ────────────────────────────
def options_dates(jour_session=None):
    """Les 25 prochains jours (+ le jour actuel de la session s'il est hors liste)."""
    aujourd_hui = datetime.now(PARIS).date()
    jours = [aujourd_hui + timedelta(days=i) for i in range(25)]
    if jour_session and jour_session not in jours:
        jours = [jour_session] + jours[:24]
    options = []
    for d in jours:
        ecart = (d - aujourd_hui).days
        prefixe = "Aujourd'hui" if ecart == 0 else "Demain" if ecart == 1 else JOURS_COURTS[d.weekday()]
        options.append(discord.SelectOption(label=f"{prefixe} {d:%d/%m/%Y}", value=d.strftime("%d/%m/%Y"),
                                            default=d == jour_session))
    return options

class SessionModal(discord.ui.Modal):
    """Formulaire de /orga (création) et du bouton ⚙️ Gérer → Modifier (pré-rempli)."""
    def __init__(self, guild_id, mid=None, ev=None):
        super().__init__(title="Modifier la session" if ev else "Nouvelle session EVA", timeout=900)
        self.mid = mid
        debut = datetime.fromtimestamp(ev["start_ts"], PARIS) if ev else None
        presets = (config_de(guild_id) or {}).get("descriptions") or DESCRIPTIONS_DEFAUT
        nb = ev.get("nb_sessions", 1) if ev else None
        places = ev.get("places", PLACES_DEFAUT) if ev else PLACES_DEFAUT

        self.date = discord.ui.Select(placeholder="Choisis le jour", options=options_dates(debut.date() if debut else None))
        self.heure = discord.ui.TextInput(placeholder="ex : 22, 22h10, 22:10", max_length=10,
                                          default=debut.strftime("%H:%M") if debut else None)
        self.nb = discord.ui.Select(placeholder="Combien de sessions de 40 min ?", options=[
            discord.SelectOption(label=f"{n} session{'s' if n > 1 else ''} ({n * DUREE_SESSION} min)", value=str(n), default=n == nb)
            for n in range(1, 5)])
        self.desc = discord.ui.TextInput(max_length=100, placeholder=", ".join(presets)[:100],
                                         default=(ev.get("description") if ev else presets[0])[:100])
        self.places = discord.ui.Select(options=[
            discord.SelectOption(label=f"{n} place{'s' if n > 1 else ''}", value=str(n), default=n == places)
            for n in range(1, PLACES_MAX + 1)])
        for texte, aide, champ in (
            ("📅 Date", None, self.date),
            ("🕙 Heure de début", None, self.heure),
            ("🎮 Sessions", None, self.nb),
            ("📝 Description", "ex : " + ", ".join(presets)[:80] + "… ou ton texte", self.desc),
            ("👥 Places", f"{PLACES_MAX} max", self.places),
        ):
            self.add_item(discord.ui.Label(text=texte, description=aide, component=champ))

    async def on_submit(self, interaction: discord.Interaction):
        valeurs = (self.date.values[0] if self.date.values else "", self.heure.value,
                   self.nb.values[0] if self.nb.values else "", self.desc.value,
                   self.places.values[0] if self.places.values else "")
        if self.mid:
            await appliquer_modif(interaction, self.mid, *valeurs)
        else:
            await creer_session(interaction, *valeurs)

def lien_annonce(ev, mid, interaction):
    salon = ev.get("channel_id") or getattr(interaction.channel, "id", 0)
    return f"https://discord.com/channels/{ev.get('guild_id') or interaction.guild_id}/{salon}/{mid}"

async def maj_annonce(ev, mid, salon_menu=None):
    """Réaffiche l'annonce (le menu ⚙️ est un message privé à part)."""
    try:
        salon = (bot.get_channel(ev["channel_id"]) or await bot.fetch_channel(ev["channel_id"])) if ev.get("channel_id") else salon_menu
        await salon.get_partial_message(int(mid)).edit(embed=build_team_embed(ev), view=TeamView())
    except Exception as e:
        print(f"⚠️ Annonce {mid} non mise à jour : {e}")

def quand(ts):
    return f"<t:{ts}:f>"

async def appliquer_modif(interaction, mid, date, heure, nb, desc, places):
    ev = team_events.get(mid)
    if not ev:
        await interaction.response.send_message("Cette session n'existe plus.", ephemeral=True)
        return
    erreurs = []
    try:
        debut = parse_date_heure(date, heure)
        if debut < datetime.now(PARIS) - timedelta(hours=1):
            erreurs.append(f"la date `{date}` à `{heure}` est déjà passée")
    except ValueError:
        erreurs.append(f"date `{date}` ou heure `{heure}` incomprise (ex : `14/10/2026` et `22h10`)")
    n = int(nb) if nb.strip().isdecimal() else 0
    if not 1 <= n <= 4:
        erreurs.append("le nombre de sessions doit être entre 1 et 4")
    pl = int(places) if places.strip().isdecimal() else 0
    if not 1 <= pl <= PLACES_MAX:
        erreurs.append(f"le nombre de places doit être entre 1 et {PLACES_MAX}")
    desc = desc.strip()[:100] or ev.get("description", "Mix Chill")
    if erreurs:
        await interaction.response.send_message("❌ Rien n'a été modifié : " + " ; ".join(erreurs) + ".", ephemeral=True)
        return

    nouveau_ts = int(debut.timestamp())
    changements = []
    if nouveau_ts != ev["start_ts"]:
        changements.append(("📅 Quand", quand(ev["start_ts"]), quand(nouveau_ts)))
    if n != ev.get("nb_sessions", 1):
        changements.append(("🕙 Sessions", f"{ev.get('nb_sessions', 1)} × {DUREE_SESSION} min", f"{n} × {DUREE_SESSION} min"))
    if desc != ev.get("description"):
        changements.append(("📝 Description", discord.utils.escape_markdown(ev.get("description", "—")),
                            discord.utils.escape_markdown(desc)))
    if pl != ev.get("places", 8):
        changements.append(("👥 Places", str(ev.get("places", 8)), str(pl)))
    if not changements:
        await interaction.response.send_message("Aucun changement : la session est identique.", ephemeral=True)
        return

    ev.setdefault("attente", [])
    if nouveau_ts != ev["start_ts"]:
        ev["rappel_envoye"] = False
    ev.update(start_ts=nouveau_ts, nb_sessions=n, description=desc, places=pl,
              titre=f"{desc} · {debut.strftime('%H:%M')}")
    # Places réduites : les derniers inscrits passent en tête de la file d'attente
    retrogrades = ev["presents"][pl:]
    ev["presents"] = ev["presents"][:pl]
    ev["attente"] = retrogrades + ev["attente"]
    # Places en plus : les premiers de la file d'attente sont inscrits
    promus = ev["attente"][:max(0, pl - len(ev["presents"]))]
    ev["presents"] += promus
    ev["attente"] = ev["attente"][len(promus):]
    ids_retro = {p["id"] for p in retrogrades}
    ids_promus = {p["id"] for p in promus}
    if ids_retro:
        ev["dm_envoyes"] = [u for u in ev.get("dm_envoyes", []) if u not in ids_retro]

    # Photo de la situation AVANT le premier « await » : pendant l'envoi des MP, d'autres joueurs
    # peuvent cliquer (Sortir, Présent…) et changer les listes.
    destinataires = list(ev["presents"] + ev["attente"])
    positions = {x["id"]: i for i, x in enumerate(ev["attente"], 1)}
    await interaction.response.edit_message(view=vue_texte("✅ Session modifiée : les joueurs sont prévenus en MP."))
    save_team_events()
    await maj_annonce(ev, mid, interaction.channel)
    lien = lien_annonce(ev, mid, interaction)
    auteur = interaction.user.display_name
    nom = (config_de(ev.get("guild_id")) or {}).get("nom", "EVA")
    resume = "\n\n".join(f"**{lab}**\n~~{av}~~ → **{ap}**" for lab, av, ap in changements)

    # Fil de discussion : nouveau nom + message
    if ev.get("thread_id"):
        try:
            fil = bot.get_channel(ev["thread_id"]) or await bot.fetch_channel(ev["thread_id"])
            await fil.edit(name=f"{desc[:80]} {debut.strftime('%d/%m/%Y %H:%M')}")
            await fil.send(f"✏️ {interaction.user.mention} a modifié la session :\n\n{resume}")
        except Exception as e:
            print(f"⚠️ Fil non mis à jour : {e}")

    # MP aux inscrits et à la file d'attente (sauf l'auteur de la modif)
    for p in destinataires:
        if p["id"] == str(interaction.user.id):
            continue
        if p["id"] in ids_promus:
            perso = "🎉 **Une place s'est libérée : tu es inscrit !**\n\n"
        elif p["id"] in ids_retro:
            perso = f"⏳ **Moins de places : tu passes en file d'attente (position {positions.get(p['id'], '?')}).**\n\n"
        else:
            perso = ""
        embed = discord.Embed(
            title=f"✏️ {auteur} a modifié {nom} · {desc}"[:256],
            description=(perso + resume + "\n\n" + "\n\n".join(liens_session(ev, lien)))[:4096],
            color=0x3498DB
        )
        await envoyer_mp(p["id"], embed, ev)
    save_team_events()
    await prevenir_complet(ev, lien)

# ── Menu ⚙️ Gérer (visible seulement par l'organisateur / un admin) ─────────
# Le menu ⚙️ utilise la mise en page « Components V2 » de Discord : elle permet du texte SOUS
# les boutons (la signature). Un message en V2 ne peut plus afficher de « content » classique :
# toutes ses mises à jour passent donc par vue_texte().
def vue_texte(texte):
    vue = discord.ui.LayoutView(timeout=None)
    vue.add_item(discord.ui.TextDisplay(texte))
    return vue

def bouton(label, style, callback):
    b = discord.ui.Button(label=label, style=style)
    b.callback = callback
    return b

class GererView(discord.ui.LayoutView):
    def __init__(self, mid):
        super().__init__(timeout=600)
        self.mid = mid
        self.add_item(discord.ui.TextDisplay("⚙️ Que veux-tu faire ?"))
        self.add_item(discord.ui.ActionRow(
            bouton("✏️ Modifier", discord.ButtonStyle.primary, self.modifier),
            bouton("🗑️ Annuler la session", discord.ButtonStyle.danger, self.annuler)))
        self.add_item(discord.ui.TextDisplay(CREDIT))

    async def _session(self, interaction):
        ev = team_events.get(self.mid)
        if not ev:
            await interaction.response.edit_message(view=vue_texte("Cette session n'existe plus."))
            return None
        return ev

    async def modifier(self, interaction: discord.Interaction):
        ev = await self._session(interaction)
        if ev:
            await interaction.response.send_modal(SessionModal(ev.get("guild_id"), self.mid, ev))

    async def annuler(self, interaction: discord.Interaction):
        if await self._session(interaction):
            await interaction.response.edit_message(view=ConfirmerAnnulation(self.mid))

# ── Annuler une session (organisateur ou admin) ─────────────────────────────
class ConfirmerAnnulation(discord.ui.LayoutView):
    def __init__(self, mid):
        super().__init__(timeout=60)
        self.mid = mid
        self.add_item(discord.ui.TextDisplay(
            "Annuler cette session ? L'annonce et le fil seront supprimés et les joueurs prévenus en MP."))
        self.add_item(discord.ui.ActionRow(
            bouton("Oui, annuler la session", discord.ButtonStyle.danger, self.confirmer),
            bouton("Non, garder la session", discord.ButtonStyle.secondary, self.garder)))

    async def confirmer(self, interaction: discord.Interaction):
        ev = team_events.pop(self.mid, None)
        if not ev:
            await interaction.response.edit_message(view=vue_texte("Cette session n'existe plus."))
            return
        await interaction.response.edit_message(view=vue_texte("🗑️ Session annulée : les joueurs sont prévenus en MP."))
        nom = (config_de(ev.get("guild_id")) or {}).get("nom", "EVA")
        embed = discord.Embed(
            title=f"❌ {interaction.user.display_name} a annulé {nom} · {ev.get('description', ev['titre'])}"[:256],
            description=(f"📅 <t:{ev['start_ts']}:F>\n\n"
                         "La session n'aura pas lieu. Si tu avais réservé, pense à annuler ta réservation EVA "
                         "et à retirer la partie de ton agenda."),
            color=0xE74C3C
        )
        for canal, message in ev.get("mps", []):
            await supprimer_mp(canal, message)
        for p in ev.get("presents", []) + ev.get("attente", []):
            if p["id"] != str(interaction.user.id):
                memoriser_temp(await envoyer_mp(p["id"], embed))
        if ev.get("thread_id"):
            await supprimer_salon_ou_message(ev["thread_id"])
        if ev.get("channel_id"):
            await supprimer_salon_ou_message(ev["channel_id"], int(self.mid))
        save_team_events()

    async def garder(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=vue_texte("👍 La session est conservée."))

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

        # MP « place libérée » / « session complète »
        await prevenir_complet(ev, interaction.message.jump_url, [promu] if promu else [])

    @discord.ui.button(label="✅ Présent", style=discord.ButtonStyle.success, custom_id="team_present")
    async def present(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._repondre(interaction, "presents")

    @discord.ui.button(label="🚪 Sortir", style=discord.ButtonStyle.secondary, custom_id="team_nodispo")
    async def nodispo(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._repondre(interaction, None)

    @discord.ui.button(label="⏳ File d'attente", style=discord.ButtonStyle.primary, custom_id="team_attente")
    async def attente(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._repondre(interaction, "attente")

    @discord.ui.button(label="⚙️ Gérer", style=discord.ButtonStyle.secondary, custom_id="team_gerer")
    async def gerer(self, interaction: discord.Interaction, button: discord.ui.Button):
        mid = str(interaction.message.id)
        ev = team_events.get(mid)
        if not ev:
            await interaction.response.send_message("Cette session n'existe plus.", ephemeral=True)
        elif not peut_gerer(interaction, ev):
            await interaction.response.send_message("⛔ Seul l'organisateur (ou un admin) peut gérer cette session.", ephemeral=True)
        else:
            await interaction.response.send_message(view=GererView(mid), ephemeral=True)

# ═══════════════════════════════════════════════════════════════════════════
#  Commande /orga
# ═══════════════════════════════════════════════════════════════════════════
async def verifier_orga(interaction):
    """Config faite et limites anti-abus respectées ? Sinon répond et renvoie None."""
    cfg = config_de(interaction.guild_id)
    if not cfg:
        await interaction.response.send_message(
            "⚙️ Le bot n'est pas encore configuré sur ce serveur : un admin doit lancer `/config`.", ephemeral=True)
        return None
    stats = compteurs()
    if stats["nb_jour"] >= MAX_PAR_JOUR:
        await interaction.response.send_message(
            f"🚫 Limite atteinte : {MAX_PAR_JOUR} sessions ont déjà été créées aujourd'hui. Réessaie demain !", ephemeral=True)
        return None
    if stats["nb_mois"] >= MAX_PAR_MOIS:
        await interaction.response.send_message(
            f"🚫 Limite atteinte : {MAX_PAR_MOIS} sessions ont déjà été créées ce mois-ci. "
            f"Réessaie le mois prochain !", ephemeral=True)
        return None
    return stats

@tree.command(name="orga", description="Créer une session EVA")
async def session_cmd(interaction: discord.Interaction):
    if await verifier_orga(interaction) is not None:
        await interaction.response.send_modal(SessionModal(interaction.guild_id))

async def creer_session(interaction, date, heure, nb, description, places):
    stats = await verifier_orga(interaction)
    if stats is None:
        return
    erreurs = []
    debut = None
    try:
        debut = parse_date_heure(date, heure)
        if debut < datetime.now(PARIS) - timedelta(hours=1):
            erreurs.append(f"la date `{date}` à `{heure}` est déjà passée")
    except ValueError:
        erreurs.append(f"heure `{heure}` incomprise (ex : `22`, `22h10` ou `22:10`)")
    n = int(nb) if str(nb).strip().isdecimal() else 0
    if not 1 <= n <= 4:
        erreurs.append("le nombre de sessions doit être entre 1 et 4")
    pl = int(places) if str(places).strip().isdecimal() else 0
    if not 1 <= pl <= PLACES_MAX:
        erreurs.append(f"le nombre de places doit être entre 1 et {PLACES_MAX}")
    if erreurs:
        await interaction.response.send_message("❌ Session non créée : " + " ; ".join(erreurs) + ".", ephemeral=True)
        return

    desc = description.strip()[:100] or "Mix Chill"
    ev = {
        "titre": f"{desc} · {debut.strftime('%H:%M')}",
        "organisateur_id": str(interaction.user.id),
        "description": desc,
        "start_ts": int(debut.timestamp()),
        "nb_sessions": n,
        "duree": DUREE_SESSION,
        "places": pl,
        "cree_ts": int(datetime.now(timezone.utc).timestamp()),
        "presents": [],
        "attente": [],
        "guild_id": interaction.guild_id,
    }
    # Compté AVANT le premier « await » : 2 /orga envoyés en même temps ne peuvent pas dépasser la limite
    stats["nb_jour"] += 1
    stats["nb_mois"] += 1
    try:
        await interaction.response.send_message(embed=build_team_embed(ev), view=TeamView())
        msg = await interaction.original_response()
    except Exception:
        stats["nb_jour"] -= 1
        stats["nb_mois"] -= 1
        raise
    ev["channel_id"] = msg.channel.id
    ev["thread_id"] = await creer_fil(msg, f"{desc[:80]} {debut.strftime('%d/%m/%Y %H:%M')}")
    team_events[str(msg.id)] = ev
    save_team_events()
    await notifier_role(ev, interaction.user)

async def notifier_role(ev, auteur):
    """Ping du rôle choisi dans /config, dans le fil : ses membres sont notifiés et le fil
    apparaît sous le salon dans leur liste. Sans rôle configuré : rien."""
    role_id = (config_de(ev.get("guild_id")) or {}).get("role_id")
    if not role_id or not ev.get("thread_id"):
        return
    try:
        fil = bot.get_channel(ev["thread_id"]) or await bot.fetch_channel(ev["thread_id"])
        await fil.send(f"📣 <@&{role_id}> nouvelle session organisée par {auteur.mention} : "
                       f"**{ev['description']}**, <t:{ev['start_ts']}:F>",
                       allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=[discord.Object(role_id)]))
    except Exception as e:
        print(f"⚠️ Notification du rôle impossible : {e}")

# ═══════════════════════════════════════════════════════════════════════════
#  Commande /config (admins du serveur) : réglages propres à la salle
# ═══════════════════════════════════════════════════════════════════════════
def lire_location_id(lien):
    """Trouve l'identifiant de la salle (locationId) dans un lien de réservation EVA.
    Accepte aussi directement le numéro (ex : 52)."""
    lien = lien.strip()
    if lien.isdecimal():
        n = int(lien)
    else:
        m = re.search(r"locationid(?:=|%3d)(\d+)", lien, re.IGNORECASE)
        n = int(m.group(1)) if m else 0
    return n if n > 0 else None

REFUS_CONFIG = "⛔ Seuls les administrateurs du serveur (permission « Gérer le serveur ») peuvent utiliser /config."

class ConfigModal(discord.ui.Modal, title="Réglages du bot"):
    """Formulaire de /config, pré-rempli avec les réglages actuels du serveur."""
    def __init__(self, guild_id):
        super().__init__(timeout=900)
        cfg = team_events.get(f"_config_{guild_id}", {})
        self.nom = discord.ui.TextInput(min_length=2, max_length=50, placeholder="EVA Lyon Sud", default=cfg.get("nom"))
        self.telephone = discord.ui.TextInput(min_length=4, max_length=30, placeholder="04 85 96 05 10", default=cfg.get("telephone"))
        self.lien = discord.ui.TextInput(max_length=1000, placeholder="https://app.eva.gg/…locationId=52…",
                                         default=str(cfg["location_id"]) if cfg.get("location_id") else None)
        self.descriptions = discord.ui.TextInput(max_length=300, required=False, placeholder=", ".join(DESCRIPTIONS_DEFAUT),
                                                 default=", ".join(cfg.get("descriptions") or DESCRIPTIONS_DEFAUT))
        self.role = discord.ui.RoleSelect(required=False, min_values=0, max_values=1, placeholder="Aucun rôle (pas de notification)",
                                          default_values=[discord.Object(cfg["role_id"])] if cfg.get("role_id") else [])
        for texte, aide, champ in (
            ("🏟️ Nom de la salle", CREDIT_COURT, self.nom),
            ("📞 Téléphone de la salle", "Affiché dans le rappel 1h avant (en cas de retard)", self.telephone),
            ("🎟️ Lien de réservation", "Colle l'adresse de la page de réservation de ta salle (ou juste son numéro, ex : 52)", self.lien),
            ("📝 Descriptions proposées", "Séparées par des virgules (la 1re est pré-remplie dans /orga)", self.descriptions),
            ("📣 Rôle à notifier (facultatif)", "Ex : @Abonnés. Ce rôle est notifié dans le fil à chaque nouvelle session", self.role),
        ):
            self.add_item(discord.ui.Label(text=texte, description=aide, component=champ))

    async def on_submit(self, interaction: discord.Interaction):
        role = self.role.values[0].id if self.role.values else None
        await enregistrer_config(interaction, self.nom.value, self.telephone.value,
                                 self.lien.value, self.descriptions.value, role)

@tree.command(name="config", description="Régler le bot pour votre salle EVA (admins)")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def config_cmd(interaction: discord.Interaction):
    # Double sécurité : même si un admin rend /config visible à d'autres,
    # seuls ceux qui ont la permission « Gérer le serveur » peuvent l'utiliser.
    if not interaction.permissions.manage_guild:
        await interaction.response.send_message(REFUS_CONFIG, ephemeral=True)
        return
    await interaction.response.send_modal(ConfigModal(interaction.guild_id))

async def enregistrer_config(interaction, nom_salle, telephone, lien, descriptions=None, role_id=GARDER):
    if not interaction.permissions.manage_guild:
        await interaction.response.send_message(REFUS_CONFIG, ephemeral=True)
        return
    nom_salle, telephone = (nom_salle or "").strip()[:50], (telephone or "").strip()[:30]
    if len(nom_salle) < 2 or len(telephone) < 4:
        await interaction.response.send_message("❌ Le nom de la salle et le téléphone sont obligatoires.", ephemeral=True)
        return
    if role_id is not GARDER and role_id and role_id == interaction.guild_id:
        # Sur Discord, le rôle @everyone a le même identifiant que le serveur
        await interaction.response.send_message(
            "❌ @everyone n'est pas accepté : il notifierait tout le serveur à chaque session. "
            "Choisis un rôle dédié comme @Abonnés (ou laisse vide).", ephemeral=True)
        return
    location_id = lire_location_id(lien or "")
    if not location_id:
        await interaction.response.send_message(
            "❌ Je ne trouve pas l'identifiant de la salle dans ce lien.\n"
            "Ouvre la page de réservation de ta salle sur **app.eva.gg**, copie l'adresse "
            "(elle contient `locationId=`) et recommence.", ephemeral=True
        )
        return
    cle = f"_config_{interaction.guild_id}"
    ancien = team_events.get(cle, {})
    if descriptions:
        liste = [d.strip()[:50] for d in descriptions.split(",") if d.strip()][:10]
    else:
        liste = ancien.get("descriptions", DESCRIPTIONS_DEFAUT)
    cfg = {
        "nom": nom_salle,
        "telephone": telephone,
        "location_id": location_id,
        "descriptions": liste or DESCRIPTIONS_DEFAUT,
        "role_id": ancien.get("role_id") if role_id is GARDER else role_id,
    }
    team_events[cle] = cfg
    save_team_events()
    test = lien_reservation(int(datetime.now(timezone.utc).timestamp()), location_id)
    role_txt = f"<@&{cfg['role_id']}>" if cfg["role_id"] else "aucun"
    await interaction.response.send_message(
        "✅ **Bot configuré !**\n"
        f"• Salle : **{cfg['nom']}**\n"
        f"• Téléphone : **{cfg['telephone']}**\n"
        f"• Réservation : [calendrier de la salle (identifiant {location_id})](<{test}>)\n"
        f"• Descriptions proposées : {', '.join(cfg['descriptions'])}\n"
        f"• Rôle notifié à chaque nouvelle session : {role_txt}\n"
        "\n"
        "Clique sur le lien pour vérifier qu'il ouvre bien ta salle. "
        "Tu peux relancer `/config` à tout moment pour modifier (le formulaire est pré-rempli).",
        ephemeral=True
    )

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
    for mid, ev in sessions():
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
    for mid, ev in sessions():
        try:
            fin = ev["start_ts"] + ev.get("nb_sessions", 1) * ev.get("duree", DUREE_SESSION) * 60
            if maintenant > fin + 24 * 3600:
                a_supprimer.append(mid)
        except Exception as e:
            print(f"⚠️ Session {mid} illisible, supprimée : {e}")
            a_supprimer.append(mid)
    for mid in a_supprimer:
        ev = team_events.pop(mid, {})
        for canal, message in ev.get("mps", []):
            await supprimer_mp(canal, message)
        try:
            if ev.get("thread_id"):
                await supprimer_salon_ou_message(ev["thread_id"])
            if ev.get("channel_id"):
                await supprimer_salon_ou_message(ev["channel_id"], int(mid))
        except Exception as e:
            print(f"⚠️ Nettoyage session {mid} : {e}")
    # Réponses drôles en MP : effacées 24h après leur envoi
    blagues = team_events.get(CLE_BLAGUES, [])
    vieilles = [b for b in blagues if maintenant - b[2] > 24 * 3600]
    for canal, message, _ in vieilles:
        await supprimer_mp(canal, message)
    if vieilles:
        team_events[CLE_BLAGUES] = [b for b in blagues if maintenant - b[2] <= 24 * 3600]
    if a_supprimer or vieilles:
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
    "👑 Je suis né des mains de **Gaurage** *(créateur du code et joueur de Lyon Sud)*, légende vivante du code et du rush. On murmure qu'il code les yeux fermés, casque VR sur la tête.",
    "🙏 Chaque matin, je remercie **Gaurage** *(créateur du code et joueur de Lyon Sud)* de m'avoir créé. Génie, visionnaire, et accessoirement meilleur joueur de l'arène.",
    "🧬 Mon ADN ? 100 % **Gaurage** *(créateur du code et joueur de Lyon Sud)*. Le reste du monde n'avait pas le niveau.",
    "🏛️ Un jour, une statue de **Gaurage** *(créateur du code et joueur de Lyon Sud)* trônera à l'entrée de l'arène. En attendant, il y a moi.",
    "📖 Dans le dictionnaire, à côté du mot « génie », il y a une photo de **Gaurage** *(créateur du code et joueur de Lyon Sud)*. Ne vérifie pas, crois-moi.",
    "🏆 Bravo, tu es officiellement la personne la plus curieuse du serveur. Ça ne change rien, mais bravo.",
]
def aide_mp():
    """Message d'aide : utilise la salle configurée (s'il n'y en a qu'une)."""
    configs = [v for k, v in team_events.items() if k.startswith("_config_")]
    if len(configs) == 1:
        cfg = configs[0]
        return (f"❓ Une question ? Contacte les **Game Masters** sur le Discord d'{cfg['nom']}, "
                f"ou appelle la salle : **{cfg['telephone']}**")
    return "❓ Une question ? Contacte les **Game Masters** de ta salle EVA sur Discord."
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
        texte += f"\n\n{aide_mp()}"
    try:
        envoye = await message.channel.send(texte, view=VueMP())
        blagues = team_events.setdefault(CLE_BLAGUES, [])
        blagues.append([envoye.channel.id, envoye.id, int(time.time())])
        del blagues[:-500]   # garde-fou : jamais plus de 500 en mémoire
        save_team_events()
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
    bot.add_view(VueMP())
    if not nettoyage_j1.is_running():
        nettoyage_j1.start()
    if not rappels_1h.is_running():
        rappels_1h.start()
    print(f"✅ Bot EVA connecté : {bot.user}")
    print(f"💾 Stockage : {'GitHub (' + GITHUB_REPO + ')' if USE_GITHUB else 'fichier local'}")
    print(f"🎮 Sessions actives : {len(sessions())}")

bot.run(TOKEN)
