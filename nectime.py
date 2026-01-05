#!python3
"""
Kimay Bridge - Auto-logging Kimai pour Claude Code
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import requests
import subprocess

# Chemins
SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
DATA_DIR = SCRIPT_DIR / "data"
SESSION_FILE = DATA_DIR / "session.json"
LOCAL_LOG_FILE = DATA_DIR / "local_log.json"
PENDING_FILE = DATA_DIR / "pending_push.json"
FOLDER_MAPPINGS_FILE = DATA_DIR / "folder_mappings.json"


# =============================================================================
# KIMAI CLIENT
# =============================================================================

class KimaiClient:
    """Wrapper pour l'API Kimai"""

    def __init__(self, url: str, auth_user: str, auth_token: str, dry_run: bool = False):
        self.url = url.rstrip('/')
        self.auth_user = auth_user
        self.auth_token = auth_token
        self.dry_run = dry_run
        self.session = requests.Session()
        self.session.headers.update({
            'X-AUTH-USER': auth_user,
            'X-AUTH-TOKEN': auth_token,
            'Content-Type': 'application/json'
        })

    def _get(self, endpoint: str, params: dict = None) -> dict:
        """GET request"""
        resp = self.session.get(f"{self.url}/api/{endpoint}", params=params)
        resp.raise_for_status()
        return resp.json()

    def _post(self, endpoint: str, data: dict) -> dict:
        """POST request (respects dry_run)"""
        if self.dry_run:
            print(f"[DRY-RUN] POST /api/{endpoint}")
            print(json.dumps(data, indent=2, default=str))
            return {"dry_run": True, "data": data}

        resp = self.session.post(f"{self.url}/api/{endpoint}", json=data)
        resp.raise_for_status()
        return resp.json()

    def get_version(self) -> dict:
        """Récupère la version de Kimai"""
        return self._get("version")

    def get_projects(self, visible: bool = True) -> list:
        """Liste les projets"""
        params = {"visible": 1 if visible else 0}
        return self._get("projects", params)

    def get_activities(self, visible: bool = True) -> list:
        """Liste les activités"""
        params = {"visible": 1 if visible else 0}
        return self._get("activities", params)

    def get_active_timesheets(self) -> list:
        """Récupère les timesheets actifs (en cours)"""
        return self._get("timesheets/active")

    def create_timesheet(self, project_id: int, activity_id: int,
                         begin: datetime, end: datetime,
                         description: str = None) -> dict:
        """Crée un timesheet"""
        data = {
            "project": project_id,
            "activity": activity_id,
            "begin": begin.strftime("%Y-%m-%dT%H:%M:%S"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        if description:
            data["description"] = description

        return self._post("timesheets", data)

    def find_project_by_name(self, search: str) -> list:
        """Cherche un projet par nom (fuzzy)"""
        projects = self.get_projects()
        search_lower = search.lower()
        matches = []
        for p in projects:
            name = p.get('name', '').lower()
            if search_lower in name or any(part in name for part in search_lower.split('_')):
                matches.append(p)
        return matches


# =============================================================================
# SESSION MANAGER
# =============================================================================

class SessionManager:
    """Gestion de la session en cours"""

    def __init__(self):
        DATA_DIR.mkdir(exist_ok=True)
        self.session = self._load()

    def _load(self) -> Optional[dict]:
        """Charge la session en cours"""
        if SESSION_FILE.exists():
            with open(SESSION_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        return None

    def _save(self):
        """Sauvegarde la session"""
        if self.session:
            with open(SESSION_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.session, f, indent=2, default=str)
        elif SESSION_FILE.exists():
            SESSION_FILE.unlink()

    def is_active(self) -> bool:
        """Vérifie si une session est active"""
        return self.session is not None

    def start(self, folder: str, folder_type: str, project_id: int = None,
              project_name: str = None) -> dict:
        """Démarre une nouvelle session"""
        if self.is_active():
            raise RuntimeError("Une session est déjà active. Utilisez 'stop' ou 'cancel'.")

        now = datetime.now()
        self.session = {
            "begin": now.isoformat(),
            "folder": folder,
            "folder_type": folder_type,
            "project_id": project_id,
            "project_name": project_name,
            "last_activity": now.isoformat(),
            "activity_log": [],
            "activity_breakdown": {}
        }
        self._save()
        return self.session

    def update_activity(self, files: list = None, estimate: str = None):
        """Met à jour le timestamp et l'estimation d'activité"""
        if not self.is_active():
            return

        now = datetime.now()
        self.session["last_activity"] = now.isoformat()

        if estimate:
            # Ajouter au log
            self.session["activity_log"].append({
                "time": now.strftime("%H:%M"),
                "files": files or [],
                "estimate": estimate
            })
            # Mettre à jour le breakdown (en minutes depuis le dernier update)
            if estimate not in self.session["activity_breakdown"]:
                self.session["activity_breakdown"][estimate] = 0
            # On ajoute 1 minute à chaque update (approximation)
            self.session["activity_breakdown"][estimate] += 1
            self.session["current_activity_estimate"] = estimate

        self._save()

    def stop(self) -> dict:
        """Arrête la session et retourne les données"""
        if not self.is_active():
            raise RuntimeError("Aucune session active.")

        session_data = self.session.copy()
        session_data["end"] = datetime.now().isoformat()

        # Calculer les durées
        begin = datetime.fromisoformat(session_data["begin"])
        end = datetime.fromisoformat(session_data["end"])
        last_activity = datetime.fromisoformat(session_data["last_activity"])

        session_data["billed_minutes"] = int((end - begin).total_seconds() / 60)
        session_data["real_minutes"] = int((last_activity - begin).total_seconds() / 60)

        self.session = None
        self._save()
        return session_data

    def cancel(self):
        """Annule la session sans logger"""
        self.session = None
        self._save()

    def status(self) -> dict:
        """Retourne le statut de la session"""
        if not self.is_active():
            return {"active": False}

        begin = datetime.fromisoformat(self.session["begin"])
        now = datetime.now()
        elapsed = int((now - begin).total_seconds() / 60)

        return {
            "active": True,
            "project_name": self.session.get("project_name", "Unknown"),
            "folder_type": self.session.get("folder_type"),
            "elapsed_minutes": elapsed,
            "current_activity": self.session.get("current_activity_estimate", "unknown"),
            "breakdown": self.session.get("activity_breakdown", {})
        }


# =============================================================================
# LOCAL LOGGER
# =============================================================================

class LocalLogger:
    """Gestion du log local"""

    def __init__(self):
        DATA_DIR.mkdir(exist_ok=True)
        self.log = self._load()

    def _load(self) -> dict:
        """Charge le log"""
        if LOCAL_LOG_FILE.exists():
            with open(LOCAL_LOG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {"entries": [], "daily_totals": {}}

    def _save(self):
        """Sauvegarde le log"""
        with open(LOCAL_LOG_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.log, f, indent=2, default=str)

    def add_entry(self, session_data: dict, pushed_to_kimai: bool = False):
        """Ajoute une entrée au log"""
        date = session_data["begin"][:10]  # YYYY-MM-DD

        entry = {
            "date": date,
            "folder": session_data.get("folder"),
            "folder_type": session_data.get("folder_type"),
            "project_id": session_data.get("project_id"),
            "project_name": session_data.get("project_name"),
            "activity": session_data.get("activity", "unknown"),
            "begin": session_data.get("begin"),
            "end": session_data.get("end"),
            "billed_minutes": session_data.get("billed_minutes", 0),
            "real_minutes": session_data.get("real_minutes", 0),
            "pushed_to_kimai": pushed_to_kimai,
            "description": session_data.get("description"),
            "git_commits": session_data.get("git_commits", [])
        }

        self.log["entries"].append(entry)

        # Mettre à jour les totaux journaliers
        if date not in self.log["daily_totals"]:
            self.log["daily_totals"][date] = {"billed": 0, "real": 0}
        self.log["daily_totals"][date]["billed"] += entry["billed_minutes"]
        self.log["daily_totals"][date]["real"] += entry["real_minutes"]

        self._save()
        return entry

    def get_daily_total(self, date: str = None) -> dict:
        """Récupère le total pour une date"""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        return self.log["daily_totals"].get(date, {"billed": 0, "real": 0})

    def get_entries(self, date: str = None) -> list:
        """Récupère les entrées pour une date"""
        if date is None:
            return self.log["entries"]
        return [e for e in self.log["entries"] if e["date"] == date]

    def get_kimai_pushed_minutes(self, date: str = None) -> int:
        """Récupère le total des minutes déjà pushées vers Kimai pour une date"""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        entries = self.get_entries(date)
        return sum(e.get("billed_minutes", 0) for e in entries if e.get("pushed_to_kimai"))

    def calculate_shrink_ratio(self, new_minutes: int, daily_limit: int = 480) -> float:
        """
        Calcule le ratio de shrink pour respecter la limite journalière.

        Args:
            new_minutes: Minutes à ajouter
            daily_limit: Limite en minutes (défaut: 480 = 8h)

        Returns:
            Ratio à appliquer (1.0 si pas de shrink nécessaire)
        """
        date = datetime.now().strftime("%Y-%m-%d")
        already_pushed = self.get_kimai_pushed_minutes(date)

        total_if_pushed = already_pushed + new_minutes

        if total_if_pushed <= daily_limit:
            return 1.0

        # Calculer le ratio pour que le total = limit
        # On ne peut shrink que ce qu'on ajoute, pas ce qui est déjà pushé
        remaining_capacity = max(0, daily_limit - already_pushed)

        if new_minutes <= 0:
            return 1.0

        return remaining_capacity / new_minutes


# =============================================================================
# CONFIG
# =============================================================================

def load_config() -> dict:
    """Charge la configuration"""
    if not CONFIG_FILE.exists():
        print(f"Config non trouvée. Copiez config.example.json vers config.json")
        sys.exit(1)

    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_config(config: dict):
    """Sauvegarde la configuration"""
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)


# =============================================================================
# FOLDER MAPPINGS
# =============================================================================

def load_folder_mappings() -> dict:
    """Charge les mappings dossier → projet"""
    DATA_DIR.mkdir(exist_ok=True)
    if FOLDER_MAPPINGS_FILE.exists():
        with open(FOLDER_MAPPINGS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_folder_mappings(mappings: dict):
    """Sauvegarde les mappings dossier → projet"""
    DATA_DIR.mkdir(exist_ok=True)
    with open(FOLDER_MAPPINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(mappings, f, indent=2, ensure_ascii=False)


def get_folder_mapping(folder: str) -> Optional[dict]:
    """Trouve le mapping pour un dossier (ou un parent)"""
    folder = os.path.normpath(folder)
    mappings = load_folder_mappings()

    # Chercher exact match ou parent
    current = folder
    while current:
        if current in mappings:
            return mappings[current]
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    return None


def set_folder_mapping(folder: str, folder_type: str, project_id: int = None,
                       project_name: str = None):
    """Ajoute ou met à jour un mapping dossier → projet"""
    folder = os.path.normpath(folder)
    mappings = load_folder_mappings()

    mappings[folder] = {
        "folder_type": folder_type,
        "project_id": project_id,
        "project_name": project_name
    }

    save_folder_mappings(mappings)
    return mappings[folder]


# =============================================================================
# CLI COMMANDS
# =============================================================================

def cmd_status(args):
    """Affiche le statut de la session"""
    sm = SessionManager()
    status = sm.status()

    if not status["active"]:
        print("Aucune session active")
        return

    elapsed = status["elapsed_minutes"]
    hours, mins = divmod(elapsed, 60)

    print(f"Session en cours: {status['project_name']}")
    print(f"Type: {status['folder_type']}")
    print(f"Durée: {hours}h{mins:02d}")
    print(f"Activité estimée: {status['current_activity']}")

    if status["breakdown"]:
        print("Breakdown:")
        for act, mins in status["breakdown"].items():
            print(f"  - {act}: {mins}min")


def cmd_start(args):
    """Démarre une session"""
    config = load_config()
    sm = SessionManager()

    if sm.is_active():
        print("Session déjà active. Utilisez 'stop' ou 'cancel' d'abord.")
        return

    folder = args.folder or os.getcwd()
    folder = os.path.normpath(folder)

    # Chercher le mapping
    mapping = get_folder_mapping(folder)

    if mapping:
        folder_type = mapping.get("folder_type", "pro")
        project_id = mapping.get("project_id")
        project_name = mapping.get("project_name", "Unknown")

        if folder_type == "off":
            print(f"Dossier marqué 'off', pas de tracking.")
            return

        print(f"Projet détecté: {project_name} ({folder_type})")
    else:
        # Nouveau dossier - fuzzy match
        print(f"Nouveau dossier: {folder}")

        client = KimaiClient(
            config["kimai_url"],
            config["auth_user"],
            config["auth_token"]
        )

        # Extraire le nom du dossier pour fuzzy search
        folder_name = os.path.basename(folder)
        matches = client.find_project_by_name(folder_name)

        if matches:
            print(f"Projets similaires trouvés:")
            for i, p in enumerate(matches[:5]):
                print(f"  {i+1}. {p['name']} (id={p['id']})")

        print("\nType de projet?")
        print("  1. pro (push Kimai)")
        print("  2. perso (local only)")
        print("  3. pending (push quand projet existe)")
        print("  4. off (ignorer)")

        # Pour l'instant, on utilise les args ou valeurs par défaut
        folder_type = args.type or "pro"
        project_id = args.project
        project_name = folder_name

    session = sm.start(folder, folder_type, project_id, project_name)
    print(f"Session démarrée: {project_name}")
    print(f"Début: {session['begin']}")


def cmd_stop(args):
    """Arrête la session"""
    config = load_config()
    sm = SessionManager()
    logger = LocalLogger()

    if not sm.is_active():
        print("Aucune session active")
        return

    session_data = sm.stop()

    # Déterminer l'activité
    activity = args.activity or config.get("default_activity", "dev_applicatif")
    session_data["activity"] = activity

    folder_type = session_data.get("folder_type", "pro")
    pushed = False

    if folder_type == "pro" and session_data.get("project_id"):
        # Push vers Kimai
        client = KimaiClient(
            config["kimai_url"],
            config["auth_user"],
            config["auth_token"],
            dry_run=config.get("dry_run", True)
        )

        activity_id = config["activity_mappings"].get(activity, {}).get("id")
        if activity_id:
            begin = datetime.fromisoformat(session_data["begin"])
            end = datetime.fromisoformat(session_data["end"])

            result = client.create_timesheet(
                project_id=session_data["project_id"],
                activity_id=activity_id,
                begin=begin,
                end=end
            )

            if not config.get("dry_run"):
                pushed = True
                print(f"Timesheet créé dans Kimai (id={result.get('id')})")

    # Log local
    entry = logger.add_entry(session_data, pushed_to_kimai=pushed)

    hours, mins = divmod(session_data["billed_minutes"], 60)
    print(f"Session terminée: {session_data.get('project_name')}")
    print(f"Durée facturée: {hours}h{mins:02d}")
    print(f"Temps réel: {session_data['real_minutes']}min")
    print(f"Activité: {activity}")

    if folder_type == "perso":
        print("(Log local uniquement)")
    elif folder_type == "pending":
        print("(En attente de projet Kimai)")


def cmd_cancel(args):
    """Annule la session"""
    sm = SessionManager()
    if not sm.is_active():
        print("Aucune session active")
        return

    sm.cancel()
    print("Session annulée")


def cmd_projects(args):
    """Liste les projets Kimai"""
    config = load_config()
    client = KimaiClient(
        config["kimai_url"],
        config["auth_user"],
        config["auth_token"]
    )

    projects = client.get_projects()
    print(f"{'ID':>4} | {'Nom':<50}")
    print("-" * 60)
    for p in sorted(projects, key=lambda x: x['name']):
        print(f"{p['id']:>4} | {p['name']:<50}")


def cmd_activities(args):
    """Liste les activités Kimai"""
    config = load_config()
    client = KimaiClient(
        config["kimai_url"],
        config["auth_user"],
        config["auth_token"]
    )

    activities = client.get_activities()
    print(f"{'ID':>4} | {'Nom':<40}")
    print("-" * 50)
    for a in sorted(activities, key=lambda x: x['name']):
        print(f"{a['id']:>4} | {a['name']:<40}")


def cmd_log(args):
    """Affiche le log local"""
    logger = LocalLogger()

    date = args.date or datetime.now().strftime("%Y-%m-%d")
    entries = logger.get_entries(date)
    totals = logger.get_daily_total(date)

    print(f"Log du {date}")
    print("-" * 60)

    for e in entries:
        begin = e["begin"][11:16]  # HH:MM
        end = e["end"][11:16] if e.get("end") else "??:??"
        status = "K" if e.get("pushed_to_kimai") else "L"
        print(f"[{status}] {begin}-{end} | {e['project_name']:<30} | {e['activity']}")

    print("-" * 60)
    bh, bm = divmod(totals["billed"], 60)
    rh, rm = divmod(totals["real"], 60)
    print(f"Total facturé: {bh}h{bm:02d} | Réel: {rh}h{rm:02d}")


def get_git_commits(folder: str, since: str, until: str) -> list:
    """Récupère les commits git entre deux dates"""
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "--since", since, "--until", until, "--format=%h %s"],
            cwd=folder,
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().split('\n')
        return []
    except:
        return []


def consolidate_entries(entries: list, shrink_ratios: dict) -> list:
    """
    Consolide les entrées par jour + projet + activité.
    Retourne une liste de groupes consolidés pour Kimai.
    """
    # Grouper par: date + project_id + activity
    groups = {}

    for e in entries:
        date = e.get("date", e.get("begin", "")[:10])
        key = (date, e.get("project_id"), e.get("activity"))

        if key not in groups:
            groups[key] = {
                "date": date,
                "project_id": e.get("project_id"),
                "project_name": e.get("project_name"),
                "folder": e.get("folder"),
                "activity": e.get("activity"),
                "entries": [],
                "total_minutes": 0,
                "first_begin": e.get("begin"),
                "last_end": e.get("end"),
                "descriptions": [],
                "git_commits": []
            }

        groups[key]["entries"].append(e)
        groups[key]["total_minutes"] += e.get("billed_minutes", 0)

        # Collecter descriptions et commits
        if e.get("description"):
            groups[key]["descriptions"].append(e["description"])
        if e.get("git_commits"):
            groups[key]["git_commits"].extend(e["git_commits"])

        # Garder le premier begin et le dernier end
        if e.get("begin") < groups[key]["first_begin"]:
            groups[key]["first_begin"] = e.get("begin")
        if e.get("end") > groups[key]["last_end"]:
            groups[key]["last_end"] = e.get("end")

    # Appliquer shrink et calculer les heures pour Kimai
    consolidated = []
    for key, group in groups.items():
        date = group["date"]
        ratio = shrink_ratios.get(date, 1.0)
        shrunk_minutes = int(group["total_minutes"] * ratio)

        # Pour Kimai: utiliser first_begin et calculer end à partir de la durée shrinkée
        begin = datetime.fromisoformat(group["first_begin"])
        end = begin + timedelta(minutes=shrunk_minutes)

        # Construire la description pour Kimai
        desc_parts = []

        # Ajouter les descriptions manuelles (dédupliquées)
        unique_descs = list(dict.fromkeys(group["descriptions"]))
        if unique_descs:
            desc_parts.extend(unique_descs)

        # Ajouter les commits git (dédupliqués)
        unique_commits = list(dict.fromkeys(group["git_commits"]))
        if unique_commits:
            if desc_parts:
                desc_parts.append("")  # ligne vide
            desc_parts.append("Commits:")
            desc_parts.extend([f"  {c}" for c in unique_commits[:10]])  # max 10 commits
            if len(unique_commits) > 10:
                desc_parts.append(f"  ... et {len(unique_commits) - 10} autres")

        kimai_description = "\n".join(desc_parts) if desc_parts else None

        consolidated.append({
            **group,
            "shrunk_minutes": shrunk_minutes,
            "kimai_begin": begin,
            "kimai_end": end,
            "ratio": ratio,
            "kimai_description": kimai_description
        })

    # Trier par date puis activité
    consolidated.sort(key=lambda x: (x["date"], x["activity"]))
    return consolidated


def display_consolidated(consolidated: list, shrink_ratios: dict, to_push: list, date_label: str, verbose: bool = False):
    """Affiche les entrées consolidées"""
    print(f"\nEntrées ({date_label}) - consolidées par jour:")
    print("-" * 75)

    total_original = 0
    total_shrunk = 0
    current_date = None

    for group in consolidated:
        date = group["date"]
        if date != current_date:
            ratio = shrink_ratios.get(date, 1.0)
            if ratio < 1.0:
                print(f"\n  [{date}] ⚠ shrink {ratio:.0%}")
            else:
                print(f"\n  [{date}]")
            current_date = date

        original = group["total_minutes"]
        shrunk = group["shrunk_minutes"]
        n_entries = len(group["entries"])
        h, m = divmod(shrunk, 60)

        shrink_info = f" -> {h}h{m:02d}" if group["ratio"] < 1.0 else ""
        entries_info = f"({n_entries} sessions)" if n_entries > 1 else ""

        h_orig, m_orig = divmod(original, 60)
        print(f"    {group['project_name']:<22} | {group['activity']:<12} | {h_orig}h{m_orig:02d}{shrink_info} {entries_info}")

        # Afficher description et commits si verbose ou si présents
        if verbose or group.get("kimai_description"):
            if group.get("descriptions"):
                for desc in group["descriptions"][:3]:
                    print(f"      → {desc[:60]}{'...' if len(desc) > 60 else ''}")
            if group.get("git_commits"):
                n_commits = len(group["git_commits"])
                print(f"      📝 {n_commits} commit{'s' if n_commits > 1 else ''}: {group['git_commits'][0][:50]}")
                if n_commits > 1:
                    print(f"         ... et {n_commits - 1} autres")

        total_original += original
        total_shrunk += shrunk

    h_orig, m_orig = divmod(total_original, 60)
    h_shrunk, m_shrunk = divmod(total_shrunk, 60)
    print("-" * 75)
    print(f"Sessions locales: {len(to_push)} -> Timesheets Kimai: {len(consolidated)}")
    if total_original != total_shrunk:
        print(f"Total: {h_orig}h{m_orig:02d} -> {h_shrunk}h{m_shrunk:02d} (après shrink)")
    else:
        print(f"Total: {h_orig}h{m_orig:02d}")


def cmd_summary(args):
    """Affiche un résumé consolidé sans pusher"""
    # Vérifier le bridge
    supported_bridges = ["kimai"]
    if args.bridge not in supported_bridges:
        print(f"Bridge inconnu: {args.bridge}")
        print(f"Bridges supportés: {', '.join(supported_bridges)}")
        return

    config = load_config()
    logger = LocalLogger()

    # Toutes les entrées ou filtrées par date
    if args.date:
        entries = logger.get_entries(args.date)
        date_label = args.date
    else:
        entries = logger.get_entries()
        date_label = "toutes dates"

    # Filtrer les entrées non-pushées de type "pro"
    to_show = [e for e in entries
               if not e.get("pushed_to_kimai")
               and e.get("folder_type") == "pro"
               and e.get("project_id")]

    if not to_show:
        print(f"Aucune entrée à afficher ({date_label})")
        return

    # Calculer shrink ratios
    by_date = {}
    for e in to_show:
        d = e.get("date", e.get("begin", "")[:10])
        if d not in by_date:
            by_date[d] = []
        by_date[d].append(e)

    daily_limit = config.get("daily_limit_hours", 8) * 60
    shrink_ratios = {}
    for date, day_entries in by_date.items():
        total_minutes = sum(e.get("billed_minutes", 0) for e in day_entries)
        already_pushed = logger.get_kimai_pushed_minutes(date)
        available = max(0, daily_limit - already_pushed)
        if total_minutes > available:
            shrink_ratios[date] = available / total_minutes if total_minutes > 0 else 1.0
        else:
            shrink_ratios[date] = 1.0

    consolidated = consolidate_entries(to_show, shrink_ratios)
    display_consolidated(consolidated, shrink_ratios, to_show, date_label, verbose=args.verbose)


def cmd_push(args):
    """Push les entrées non-pushées vers un bridge (consolidées par jour)"""
    # Vérifier le bridge
    supported_bridges = ["kimai"]
    if args.bridge not in supported_bridges:
        print(f"Bridge inconnu: {args.bridge}")
        print(f"Bridges supportés: {', '.join(supported_bridges)}")
        return

    config = load_config()
    logger = LocalLogger()

    # Toutes les entrées ou filtrées par date
    if args.date:
        entries = logger.get_entries(args.date)
        date_label = args.date
    else:
        entries = logger.get_entries()  # Toutes
        date_label = "toutes dates"

    # Filtrer les entrées non-pushées de type "pro"
    to_push = [e for e in entries
               if not e.get("pushed_to_kimai")
               and e.get("folder_type") == "pro"
               and e.get("project_id")]

    if not to_push:
        print(f"Aucune entrée à pusher ({date_label})")
        return

    # Grouper par date pour le shrink
    by_date = {}
    for e in to_push:
        d = e.get("date", e.get("begin", "")[:10])
        if d not in by_date:
            by_date[d] = []
        by_date[d].append(e)

    daily_limit = config.get("daily_limit_hours", 8) * 60

    # Calculer les shrink ratios par jour
    shrink_ratios = {}
    for date, day_entries in by_date.items():
        total_minutes = sum(e.get("billed_minutes", 0) for e in day_entries)
        already_pushed = logger.get_kimai_pushed_minutes(date)
        available = max(0, daily_limit - already_pushed)

        if total_minutes > available:
            shrink_ratios[date] = available / total_minutes if total_minutes > 0 else 1.0
        else:
            shrink_ratios[date] = 1.0

    # Consolider les entrées
    consolidated = consolidate_entries(to_push, shrink_ratios)

    # Afficher le résumé consolidé
    display_consolidated(consolidated, shrink_ratios, to_push, date_label, verbose=True)

    if config.get("dry_run", True):
        print("\n[DRY-RUN] Aucun push effectué. Désactivez dry_run dans config.json")
        return

    if not args.yes:
        confirm = input("\nConfirmer le push? (y/N) ")
        if confirm.lower() != 'y':
            print("Push annulé")
            return

    # Push vers Kimai (entrées consolidées)
    client = KimaiClient(
        config["kimai_url"],
        config["auth_user"],
        config["auth_token"],
        dry_run=False
    )

    pushed_count = 0
    entries_marked = 0

    for group in consolidated:
        activity_key = group.get("activity", config.get("default_activity"))
        activity_id = config["activity_mappings"].get(activity_key, {}).get("id")

        if not activity_id:
            print(f"  ✗ Activité inconnue: {activity_key}")
            continue

        try:
            result = client.create_timesheet(
                project_id=group["project_id"],
                activity_id=activity_id,
                begin=group["kimai_begin"],
                end=group["kimai_end"],
                description=group.get("kimai_description")
            )

            # Marquer toutes les entrées du groupe comme pushées
            for e in group["entries"]:
                e["pushed_to_kimai"] = True
                e["shrunk_minutes"] = group["shrunk_minutes"] if group["ratio"] < 1.0 else None
                e["consolidated_with"] = len(group["entries"])
                entries_marked += 1

            pushed_count += 1
            h, m = divmod(group['shrunk_minutes'], 60)
            print(f"  ✓ [{group['date']}] {group['project_name']} / {activity_key} - {h}h{m:02d}")

        except Exception as ex:
            print(f"  ✗ Erreur: {ex}")

    # Sauvegarder le log mis à jour
    logger._save()

    print(f"\n{pushed_count} timesheets créés ({entries_marked} entrées locales marquées)")


def cmd_describe(args):
    """Ajoute une description à la session en cours ou à une entrée"""
    sm = SessionManager()
    logger = LocalLogger()

    if args.index is not None:
        # Modifier une entrée existante
        entries = logger.get_entries()
        unpushed = [(i, e) for i, e in enumerate(entries) if not e.get("pushed_to_kimai")]

        if args.index < 0 or args.index >= len(unpushed):
            print(f"Index invalide. Utilisez 0-{len(unpushed)-1}")
            return

        real_idx, entry = unpushed[args.index]

        if not args.text:
            current = entry.get("description", "(aucune)")
            print(f"Description actuelle: {current}")
            print("Usage: kimay describe <index> \"texte de description\"")
            return

        logger.log["entries"][real_idx]["description"] = args.text
        logger._save()
        print(f"Description ajoutée à l'entrée [{args.index}]")

    elif sm.is_active():
        # Ajouter à la session en cours
        if not args.text:
            current = sm.session.get("description", "(aucune)")
            print(f"Description actuelle: {current}")
            print("Usage: kimay describe \"texte de description\"")
            return

        sm.session["description"] = args.text
        sm._save()
        print(f"Description ajoutée à la session en cours")

    else:
        print("Aucune session active. Utilisez: kimay describe <index> \"texte\"")


def cmd_edit(args):
    """Edite une entrée non-pushée (activité)"""
    config = load_config()
    logger = LocalLogger()

    # Récupérer les entrées non-pushées
    entries = logger.get_entries()
    unpushed = [(i, e) for i, e in enumerate(entries) if not e.get("pushed_to_kimai")]

    if not unpushed:
        print("Aucune entrée modifiable (toutes pushées)")
        return

    if args.index is None:
        # Lister les entrées modifiables
        print("Entrées modifiables (non pushées):")
        print("-" * 70)
        for idx, (real_idx, e) in enumerate(unpushed):
            date = e.get("date", "?")
            begin = e.get("begin", "")[11:16]
            end = e.get("end", "")[11:16]
            mins = e.get("billed_minutes", 0)
            desc = " [desc]" if e.get("description") else ""
            commits = f" [{len(e.get('git_commits', []))} commits]" if e.get("git_commits") else ""
            print(f"  [{idx}] {date} {begin}-{end} | {e.get('project_name', 'N/A'):<20} | {e.get('activity', 'N/A'):<12} | {mins}min{desc}{commits}")
        print("-" * 70)
        print("Usage: kimay edit <index> --activity <key>")
        return

    # Trouver l'entrée
    if args.index < 0 or args.index >= len(unpushed):
        print(f"Index invalide. Utilisez 0-{len(unpushed)-1}")
        return

    real_idx, entry = unpushed[args.index]

    if not args.activity:
        print(f"Entrée [{args.index}]:")
        print(f"  Projet: {entry.get('project_name')}")
        print(f"  Date: {entry.get('date')} {entry.get('begin', '')[11:16]}-{entry.get('end', '')[11:16]}")
        print(f"  Activité actuelle: {entry.get('activity')}")
        print("\nActivités disponibles:")
        for key, val in config.get("activity_mappings", {}).items():
            print(f"  {key}: {val.get('name', 'N/A')}")
        print("\nUsage: kimay edit", args.index, "--activity <key>")
        return

    # Vérifier que l'activité existe
    if args.activity not in config.get("activity_mappings", {}):
        print(f"Activité inconnue: {args.activity}")
        return

    # Modifier
    old_activity = entry.get("activity")
    logger.log["entries"][real_idx]["activity"] = args.activity
    logger._save()

    print(f"Entrée [{args.index}] modifiée: {old_activity} -> {args.activity}")


def cmd_activity(args):
    """Change ou affiche l'activité en cours"""
    config = load_config()
    sm = SessionManager()

    if not sm.is_active():
        print("Aucune session active")
        return

    if args.activity_key:
        # Vérifier que l'activité existe
        activity_mappings = config.get("activity_mappings", {})
        if args.activity_key not in activity_mappings:
            print(f"Activité inconnue: {args.activity_key}")
            print("Activités disponibles:")
            for key, val in activity_mappings.items():
                print(f"  {key}: {val.get('name', 'N/A')}")
            return

        # Mettre à jour l'activité
        sm.update_activity(estimate=args.activity_key)
        print(f"Activité changée: {args.activity_key}")
        print(f"  -> {activity_mappings[args.activity_key].get('name')}")
    else:
        # Afficher l'activité actuelle
        current = sm.session.get("current_activity_estimate", "non définie")
        breakdown = sm.session.get("activity_breakdown", {})

        print(f"Activité actuelle: {current}")
        if breakdown:
            print("Répartition:")
            for act, mins in sorted(breakdown.items(), key=lambda x: -x[1]):
                print(f"  {act}: ~{mins}min")


def cmd_set(args):
    """Configure le type de projet pour un dossier"""
    sm = SessionManager()
    config = load_config()

    folder_type = args.type
    project_id = args.project_id
    folder = args.folder or os.getcwd()
    folder = os.path.normpath(folder)

    # Récupérer le nom du projet si pro
    project_name = None
    if folder_type == "pro" and project_id:
        try:
            client = KimaiClient(
                config["kimai_url"],
                config["auth_user"],
                config["auth_token"]
            )
            projects = client.get_projects()
            for p in projects:
                if p['id'] == project_id:
                    project_name = p['name']
                    break
        except:
            pass

        if not project_name:
            project_name = f"Project {project_id}"

    elif folder_type in ("perso", "pending"):
        project_name = Path(folder).name

    # Sauvegarder le mapping
    set_folder_mapping(folder, folder_type, project_id, project_name)

    # Mettre à jour la session en cours si elle existe et correspond au dossier
    if sm.is_active() and sm.session.get("folder") == folder:
        sm.session["folder_type"] = folder_type
        sm.session["project_id"] = project_id
        sm.session["project_name"] = project_name
        sm._save()
        print(f"Session mise à jour: {project_name} ({folder_type})")
    else:
        print(f"Mapping enregistré: {folder}")
        print(f"  Type: {folder_type}")
        if project_name:
            print(f"  Projet: {project_name}")

    if folder_type == "pro" and project_id:
        print(f"  -> Les heures seront pushées vers Kimai")
    elif folder_type == "perso":
        print(f"  -> Les heures resteront en local")
    elif folder_type == "pending":
        print(f"  -> En attente d'un projet Kimai")
    elif folder_type == "off":
        print(f"  -> Ce dossier sera ignoré")


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Kimay Bridge - Auto-logging Kimai")
    subparsers = parser.add_subparsers(dest="command", help="Commandes")

    # status
    sp = subparsers.add_parser("status", help="Statut de la session")
    sp.set_defaults(func=cmd_status)

    # start
    sp = subparsers.add_parser("start", help="Démarrer une session")
    sp.add_argument("--folder", "-f", help="Dossier (défaut: cwd)")
    sp.add_argument("--project", "-p", type=int, help="Project ID Kimai")
    sp.add_argument("--type", "-t", choices=["pro", "perso", "pending", "off"])
    sp.set_defaults(func=cmd_start)

    # stop
    sp = subparsers.add_parser("stop", help="Arrêter la session")
    sp.add_argument("--activity", "-a", help="Clé d'activité (ex: dev_embarque)")
    sp.add_argument("--dry-run", action="store_true", help="Ne pas poster sur Kimai")
    sp.set_defaults(func=cmd_stop)

    # cancel
    sp = subparsers.add_parser("cancel", help="Annuler la session")
    sp.set_defaults(func=cmd_cancel)

    # projects
    sp = subparsers.add_parser("projects", help="Lister les projets Kimai")
    sp.set_defaults(func=cmd_projects)

    # activities
    sp = subparsers.add_parser("activities", help="Lister les activités Kimai")
    sp.set_defaults(func=cmd_activities)

    # log
    sp = subparsers.add_parser("log", help="Voir le log local")
    sp.add_argument("--date", "-d", help="Date (YYYY-MM-DD)")
    sp.set_defaults(func=cmd_log)

    # set
    sp = subparsers.add_parser("set", help="Configurer le type de projet")
    sp.add_argument("type", choices=["pro", "perso", "pending", "off"], help="Type de projet")
    sp.add_argument("project_id", nargs="?", type=int, help="ID du projet Kimai (pour 'pro')")
    sp.add_argument("--folder", "-f", help="Dossier (défaut: cwd)")
    sp.set_defaults(func=cmd_set)

    # activity
    sp = subparsers.add_parser("activity", help="Changer ou afficher l'activité en cours")
    sp.add_argument("activity_key", nargs="?", help="Clé d'activité (ex: dev_embarque)")
    sp.set_defaults(func=cmd_activity)

    # push
    sp = subparsers.add_parser("push", help="Push les entrées vers un bridge")
    sp.add_argument("bridge", nargs="?", default="kimai", help="Bridge cible (défaut: kimai)")
    sp.add_argument("--date", "-d", help="Date (YYYY-MM-DD, défaut: toutes)")
    sp.add_argument("--yes", "-y", action="store_true", help="Confirmer automatiquement")
    sp.set_defaults(func=cmd_push)

    # edit
    sp = subparsers.add_parser("edit", help="Editer une entrée non-pushée")
    sp.add_argument("index", nargs="?", type=int, help="Index de l'entrée")
    sp.add_argument("--activity", "-a", help="Nouvelle activité")
    sp.set_defaults(func=cmd_edit)

    # describe
    sp = subparsers.add_parser("describe", help="Ajouter une description")
    sp.add_argument("text", nargs="?", help="Texte de description")
    sp.add_argument("--index", "-i", type=int, help="Index de l'entrée (sinon session en cours)")
    sp.set_defaults(func=cmd_describe)

    # summary
    sp = subparsers.add_parser("summary", help="Résumé consolidé (sans push)")
    sp.add_argument("bridge", nargs="?", default="kimai", help="Bridge cible (défaut: kimai)")
    sp.add_argument("--date", "-d", help="Date (YYYY-MM-DD)")
    sp.add_argument("--verbose", "-v", action="store_true", help="Afficher détails")
    sp.set_defaults(func=cmd_summary)

    args = parser.parse_args()

    if args.command is None:
        # Défaut: status
        cmd_status(args)
    else:
        args.func(args)


if __name__ == "__main__":
    main()
