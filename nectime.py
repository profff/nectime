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
import subprocess

# 'requests' importé localement dans KimaiClient pour éviter les erreurs si non installé

# Chemins
SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
DATA_DIR = SCRIPT_DIR / "data"
SESSIONS_FILE = DATA_DIR / "sessions.json"
LOCAL_LOG_FILE = DATA_DIR / "local_log.json"
PENDING_FILE = DATA_DIR / "pending_push.json"
FOLDER_MAPPINGS_FILE = DATA_DIR / "folder_mappings.json"


# =============================================================================
# KIMAI CLIENT
# =============================================================================

class KimaiClient:
    """Wrapper pour l'API Kimai"""

    def __init__(self, url: str, auth_user: str, auth_token: str, dry_run: bool = False):
        import requests  # Import lazy
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
    """Gestion des sessions multiples (par session_id Claude Code)"""

    def __init__(self, folder: str = None, session_id: str = None):
        """
        Args:
            folder: Dossier de travail (défaut: cwd)
            session_id: ID de session Claude Code (passé par le hook)
        """
        DATA_DIR.mkdir(exist_ok=True)
        self.folder = os.path.normpath(folder) if folder else os.path.normpath(os.getcwd())
        self.session_id = session_id
        self.sessions = self._load_all()

    def _load_all(self) -> dict:
        """Charge toutes les sessions"""
        if SESSIONS_FILE.exists():
            with open(SESSIONS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {}

    def _save_all(self):
        """Sauvegarde toutes les sessions"""
        with open(SESSIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.sessions, f, indent=2, default=str)

    def _get_session(self) -> Optional[dict]:
        """Récupère la session courante (par session_id ou fallback folder)"""
        if self.session_id:
            return self.sessions.get(self.session_id)

        # Fallback CLI: si une seule session pour ce folder, l'utiliser
        folder_sessions = {sid: s for sid, s in self.sessions.items()
                          if s.get("folder") == self.folder}
        if len(folder_sessions) == 1:
            self._inferred_sid = list(folder_sessions.keys())[0]
            return list(folder_sessions.values())[0]
        return None

    def _get_effective_sid(self) -> Optional[str]:
        """Retourne le session_id effectif (explicite ou inféré)"""
        if self.session_id:
            return self.session_id
        return getattr(self, '_inferred_sid', None)

    def _set_session(self, session_data: Optional[dict]):
        """Définit la session courante"""
        sid = self._get_effective_sid()
        if not sid:
            return
        if session_data is None:
            self.sessions.pop(sid, None)
        else:
            self.sessions[sid] = session_data
        self._save_all()

    def is_active(self) -> bool:
        """Vérifie si une session est active pour ce session_id"""
        return self._get_session() is not None

    def has_any_session(self) -> bool:
        """Vérifie si au moins une session existe"""
        return len(self.sessions) > 0

    def get_folder_sessions(self) -> dict:
        """Récupère toutes les sessions du folder courant"""
        return {sid: s for sid, s in self.sessions.items() if s.get("folder") == self.folder}

    def get_all_sessions(self) -> dict:
        """Récupère toutes les sessions actives"""
        return self.sessions

    def start(self, folder_type: str, project_id: int = None,
              project_name: str = None) -> dict:
        """Démarre une nouvelle session"""
        if self.is_active():
            raise RuntimeError(f"Session déjà active pour ce folder+process.")

        now = datetime.now()
        session = {
            "begin": now.isoformat(),
            "folder": self.folder,
            "folder_type": folder_type,
            "project_id": project_id,
            "project_name": project_name,
            "last_activity": now.isoformat(),
            "activity_log": [],
            "activity_breakdown": {}
        }
        self._set_session(session)
        return session

    def update_activity(self, files: list = None, estimate: str = None):
        """Met à jour le timestamp et l'estimation d'activité"""
        session = self._get_session()
        if not session:
            return

        now = datetime.now()
        session["last_activity"] = now.isoformat()

        if estimate:
            session["activity_log"].append({
                "time": now.strftime("%H:%M"),
                "files": files or [],
                "estimate": estimate
            })
            if estimate not in session["activity_breakdown"]:
                session["activity_breakdown"][estimate] = 0
            session["activity_breakdown"][estimate] += 1
            session["current_activity_estimate"] = estimate

        self._set_session(session)

    def stop(self) -> dict:
        """Arrête la session et retourne les données"""
        session = self._get_session()
        if not session:
            raise RuntimeError("Aucune session active pour ce folder+process.")

        session_data = session.copy()
        session_data["end"] = datetime.now().isoformat()

        begin = datetime.fromisoformat(session_data["begin"])
        end = datetime.fromisoformat(session_data["end"])
        last_activity = datetime.fromisoformat(session_data["last_activity"])

        session_data["billed_minutes"] = int((end - begin).total_seconds() / 60)
        session_data["real_minutes"] = int((last_activity - begin).total_seconds() / 60)

        self._set_session(None)
        return session_data

    def cancel(self):
        """Annule la session sans logger"""
        self._set_session(None)

    def status(self) -> dict:
        """Retourne le statut de la session courante"""
        session = self._get_session()
        if not session:
            return {"active": False}

        begin = datetime.fromisoformat(session["begin"])
        now = datetime.now()
        elapsed = int((now - begin).total_seconds() / 60)

        return {
            "active": True,
            "folder": session.get("folder", self.folder),
            "session_id": self.session_id,
            "project_name": session.get("project_name", "Unknown"),
            "folder_type": session.get("folder_type"),
            "elapsed_minutes": elapsed,
            "current_activity": session.get("current_activity_estimate", "unknown"),
            "breakdown": session.get("activity_breakdown", {})
        }

    def status_all(self) -> list:
        """Retourne le statut de toutes les sessions actives"""
        result = []
        now = datetime.now()
        for session_id, session in self.sessions.items():
            begin = datetime.fromisoformat(session["begin"])
            elapsed = int((now - begin).total_seconds() / 60)
            result.append({
                "active": True,
                "folder": session.get("folder"),
                "session_id": session_id,
                "project_name": session.get("project_name", "Unknown"),
                "folder_type": session.get("folder_type"),
                "elapsed_minutes": elapsed,
                "current_activity": session.get("current_activity_estimate", "unknown"),
            })
        return result

    def cleanup_old_sessions(self, logger=None, config: dict = None, max_hours: int = 12) -> list:
        """
        Ferme proprement les sessions trop anciennes (orphelines probables).
        Critères: > max_hours depuis le début OU jour différent.
        Les sessions sont loggées avec last_activity comme heure de fin.
        """
        closed = []
        now = datetime.now()

        for session_id in list(self.sessions.keys()):
            session = self.sessions[session_id]
            begin = datetime.fromisoformat(session["begin"])

            # Orpheline si: autre jour OU > max_hours
            is_old = (begin.date() != now.date()) or ((now - begin).total_seconds() > max_hours * 3600)

            if is_old:
                self.sessions.pop(session_id)
                last_activity = datetime.fromisoformat(session["last_activity"])

                session_data = session.copy()
                session_data["end"] = last_activity.isoformat()
                session_data["billed_minutes"] = int((last_activity - begin).total_seconds() / 60)
                session_data["real_minutes"] = session_data["billed_minutes"]

                activity = session.get("current_activity_estimate")
                if not activity and config:
                    activity = config.get("default_activity", "dev_applicatif")
                session_data["activity"] = activity or "dev_applicatif"

                if logger:
                    logger.add_entry(session_data, pushed_to_kimai=False)

                closed.append({
                    "folder": session.get("folder"),
                    "session_id": session_id,
                    "project_name": session.get("project_name"),
                    "begin": session.get("begin"),
                    "billed_minutes": session_data["billed_minutes"]
                })

        if closed:
            self._save_all()
        return closed


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

    def calculate_adjustment_ratio(self, new_minutes: int, daily_limit: int = 480,
                                      date: str = None, expand: bool = True) -> float:
        """
        Calcule le ratio d'ajustement pour atteindre la limite journalière.

        - Si total > limit : shrink (ratio < 1)
        - Si total < limit et expand=True : expand (ratio > 1)
        - Sinon : ratio = 1.0

        Args:
            new_minutes: Minutes à ajouter
            daily_limit: Limite en minutes (défaut: 480 = 8h)
            date: Date concernée (défaut: aujourd'hui)
            expand: Autoriser l'expansion si < limit

        Returns:
            Ratio à appliquer
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        already_pushed = self.get_kimai_pushed_minutes(date)

        if new_minutes <= 0:
            return 1.0

        total_if_pushed = already_pushed + new_minutes
        remaining_capacity = max(0, daily_limit - already_pushed)

        if total_if_pushed > daily_limit:
            # Shrink: trop d'heures
            return remaining_capacity / new_minutes
        elif expand and total_if_pushed < daily_limit and remaining_capacity > 0:
            # Expand: pas assez d'heures, grossir pour atteindre la limite
            return remaining_capacity / new_minutes
        else:
            return 1.0

    # Alias pour rétrocompatibilité
    def calculate_shrink_ratio(self, new_minutes: int, daily_limit: int = 480) -> float:
        """Alias vers calculate_adjustment_ratio (shrink only)"""
        return self.calculate_adjustment_ratio(new_minutes, daily_limit, expand=False)

    def fill_empty_weekdays(self, start_date: str, end_date: str) -> list:
        """
        Remplit les jours de semaine vides en copiant les entrées du dernier jour non-vide.

        Args:
            start_date: Date de début (YYYY-MM-DD)
            end_date: Date de fin (YYYY-MM-DD)

        Returns:
            Liste des entrées créées
        """
        # Récupérer toutes les entrées non-pushées de type "pro"
        all_entries = [e for e in self.log["entries"]
                       if not e.get("pushed_to_kimai")
                       and e.get("folder_type") == "pro"
                       and e.get("project_id")]

        # Grouper par date
        entries_by_date = {}
        for e in all_entries:
            d = e.get("date", e.get("begin", "")[:10])
            if d not in entries_by_date:
                entries_by_date[d] = []
            entries_by_date[d].append(e)

        # Trouver les jours de semaine dans la plage
        weekdays = get_weekdays_in_range(start_date, end_date)

        # Identifier les jours vides
        empty_days = [d for d in weekdays if d not in entries_by_date]

        if not empty_days:
            return []

        # Trouver le dernier jour non-vide (avant ou dans la plage)
        all_dates_with_entries = sorted(entries_by_date.keys())
        if not all_dates_with_entries:
            return []

        # Chercher le jour source le plus récent avant le premier jour vide
        source_date = None
        for d in reversed(all_dates_with_entries):
            if d <= empty_days[0]:
                source_date = d
                break

        # Si pas trouvé avant, prendre le premier disponible
        if not source_date:
            source_date = all_dates_with_entries[0]

        source_entries = entries_by_date[source_date]
        created = []

        for empty_day in empty_days:
            # Copier les entrées du jour source
            for src in source_entries:
                # Calculer les nouvelles heures (garder la durée, changer la date)
                src_begin = datetime.fromisoformat(src["begin"])
                src_end = datetime.fromisoformat(src["end"])
                duration = src_end - src_begin

                # Nouvelles heures: même heure de début, même durée
                new_begin = datetime.strptime(empty_day, "%Y-%m-%d").replace(
                    hour=src_begin.hour, minute=src_begin.minute
                )
                new_end = new_begin + duration

                new_entry = {
                    "date": empty_day,
                    "folder": src.get("folder"),
                    "folder_type": src.get("folder_type"),
                    "project_id": src.get("project_id"),
                    "project_name": src.get("project_name"),
                    "activity": src.get("activity"),
                    "begin": new_begin.isoformat(),
                    "end": new_end.isoformat(),
                    "billed_minutes": src.get("billed_minutes", 0),
                    "real_minutes": src.get("real_minutes", 0),
                    "pushed_to_kimai": False,
                    "filled_from": source_date
                }

                self.log["entries"].append(new_entry)
                created.append(new_entry)

                # Mettre à jour les totaux journaliers
                if empty_day not in self.log["daily_totals"]:
                    self.log["daily_totals"][empty_day] = {"billed": 0, "real": 0}
                self.log["daily_totals"][empty_day]["billed"] += new_entry["billed_minutes"]
                self.log["daily_totals"][empty_day]["real"] += new_entry["real_minutes"]

        if created:
            self._save()

        return created


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
                       project_name: str = None, custom_activity: str = None):
    """Ajoute ou met à jour un mapping dossier → projet"""
    folder = os.path.normpath(folder)
    mappings = load_folder_mappings()

    # Récupérer l'ancien mapping pour conserver custom_activity si pas redéfinie
    old_mapping = mappings.get(folder, {})

    mappings[folder] = {
        "folder_type": folder_type,
        "project_id": project_id,
        "project_name": project_name,
        "custom_activity": custom_activity if custom_activity is not None else old_mapping.get("custom_activity")
    }

    save_folder_mappings(mappings)
    return mappings[folder]


# =============================================================================
# CLI COMMANDS
# =============================================================================

def cmd_status(args):
    """Affiche le statut des sessions"""
    folder = args.folder if hasattr(args, 'folder') and args.folder else None
    sm = SessionManager(folder=folder)

    # Labels pour les types de projet
    type_labels = {
        "pro": "PRO",
        "perso": "LOCAL",
        "pending": "PENDING",
        "off": "OFF",
        None: "N/A"
    }

    if args.all if hasattr(args, 'all') else False:
        # Afficher toutes les sessions
        all_sessions = sm.status_all()
        if not all_sessions:
            print("Aucune session active")
            return

        print(f"Sessions actives: {len(all_sessions)}")
        print("-" * 100)
        for s in all_sessions:
            hours, mins = divmod(s["elapsed_minutes"], 60)
            sid_short = s["session_id"][:8]
            activity = s.get("current_activity", "?")[:12]
            ftype = type_labels.get(s.get("folder_type"), "N/A")
            folder_path = s.get("folder", "?")
            print(f"  [{sid_short}] {s['project_name']:<22} | {ftype:<7} | {hours}h{mins:02d} | {activity:<12}")
            print(f"             {folder_path}")
        return

    # Session du folder courant (toutes les sessions de ce folder)
    folder_sessions = sm.get_folder_sessions()

    # Récupérer le mapping du dossier pour afficher le type même sans session
    mapping = get_folder_mapping(sm.folder)
    folder_type = mapping.get("folder_type") if mapping else None
    ftype_label = type_labels.get(folder_type, "N/A")

    if not folder_sessions:
        # Afficher quand même les infos du dossier
        print(f"Dossier: {Path(sm.folder).name}")
        print(f"Type: {ftype_label}")
        if mapping:
            print(f"Projet: {mapping.get('project_name', 'N/A')}")
            custom_act = mapping.get("custom_activity")
            if custom_act:
                print(f"Activité custom: {custom_act}")
        print(f"Session: inactive")

        # Peut-être des sessions ailleurs ?
        all_sessions = sm.status_all()
        if all_sessions:
            print(f"\n{len(all_sessions)} session(s) active(s) ailleurs. Utilisez --all")
        return

    print(f"Sessions dans {Path(sm.folder).name} [{ftype_label}]:")
    print("-" * 80)
    for session_id, session in folder_sessions.items():
        begin = datetime.fromisoformat(session["begin"])
        elapsed = int((datetime.now() - begin).total_seconds() / 60)
        hours, mins = divmod(elapsed, 60)
        activity = session.get("current_activity_estimate", "?")[:12]
        sid_short = session_id[:8]
        ftype = type_labels.get(session.get("folder_type"), "N/A")
        print(f"  [{sid_short}] {session.get('project_name', 'Unknown'):<22} | {ftype:<7} | {hours}h{mins:02d} | {activity}")


def cmd_start(args):
    """Démarre une session"""
    config = load_config()
    folder = args.folder or os.getcwd()
    folder = os.path.normpath(folder)
    sm = SessionManager(folder=folder)

    if sm.is_active():
        print("Session déjà active pour ce process. Utilisez 'stop' ou 'cancel' d'abord.")
        return

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

    session = sm.start(folder_type, project_id, project_name)
    print(f"Session démarrée: {project_name}")
    print(f"Début: {session['begin']}")


def cmd_stop(args):
    """Arrête la session"""
    config = load_config()
    folder = args.folder if hasattr(args, 'folder') and args.folder else None
    sm = SessionManager(folder=folder)
    logger = LocalLogger()

    if not sm.is_active():
        print("Aucune session active pour ce process")
        return

    session_data = sm.stop()

    # Déterminer l'activité
    activity = args.activity or session_data.get("current_activity_estimate") or config.get("default_activity", "dev_applicatif")
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
    folder = args.folder if hasattr(args, 'folder') and args.folder else None
    sm = SessionManager(folder=folder)
    if not sm.is_active():
        print("Aucune session active pour ce process")
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


def is_weekday(date_str: str) -> bool:
    """Vérifie si une date est un jour de semaine (lun-ven)"""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return dt.weekday() < 5  # 0=lundi, 4=vendredi


def get_weekdays_in_range(start_date: str, end_date: str) -> list:
    """Retourne tous les jours de semaine entre deux dates"""
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    weekdays = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            weekdays.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)
    return weekdays


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


def consolidate_entries(entries: list, adjustment_ratios: dict) -> list:
    """
    Consolide les entrées par jour + projet + activité.
    Applique les ratios d'ajustement (shrink ou expand).
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

    # Appliquer ajustement (shrink ou expand) et calculer les heures pour Kimai
    consolidated = []
    for key, group in groups.items():
        date = group["date"]
        ratio = adjustment_ratios.get(date, 1.0)
        adjusted_minutes = int(group["total_minutes"] * ratio)

        # Pour Kimai: utiliser first_begin et calculer end à partir de la durée ajustée
        begin = datetime.fromisoformat(group["first_begin"])
        end = begin + timedelta(minutes=adjusted_minutes)

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
            "adjusted_minutes": adjusted_minutes,
            "kimai_begin": begin,
            "kimai_end": end,
            "ratio": ratio,
            "kimai_description": kimai_description
        })

    # Trier par date puis activité
    consolidated.sort(key=lambda x: (x["date"], x["activity"]))
    return consolidated


def display_consolidated(consolidated: list, adjustment_ratios: dict, to_push: list, date_label: str, verbose: bool = False):
    """Affiche les entrées consolidées"""
    print(f"\nEntrées ({date_label}) - consolidées par jour:")
    print("-" * 75)

    total_original = 0
    total_adjusted = 0
    current_date = None

    for group in consolidated:
        date = group["date"]
        if date != current_date:
            ratio = adjustment_ratios.get(date, 1.0)
            if ratio < 1.0:
                print(f"\n  [{date}] [-] shrink {ratio:.0%}")
            elif ratio > 1.0:
                print(f"\n  [{date}] [+] expand {ratio:.0%}")
            else:
                print(f"\n  [{date}]")
            current_date = date

        original = group["total_minutes"]
        adjusted = group["adjusted_minutes"]
        n_entries = len(group["entries"])
        h, m = divmod(adjusted, 60)

        if group["ratio"] < 1.0:
            adjust_info = f" -> {h}h{m:02d}"
        elif group["ratio"] > 1.0:
            adjust_info = f" -> {h}h{m:02d}"
        else:
            adjust_info = ""
        entries_info = f"({n_entries} sessions)" if n_entries > 1 else ""

        h_orig, m_orig = divmod(original, 60)
        print(f"    {group['project_name']:<22} | {group['activity']:<12} | {h_orig}h{m_orig:02d}{adjust_info} {entries_info}")

        # Afficher description et commits si verbose ou si présents
        if verbose or group.get("kimai_description"):
            if group.get("descriptions"):
                for desc in group["descriptions"][:3]:
                    print(f"      → {desc[:60]}{'...' if len(desc) > 60 else ''}")
            if group.get("git_commits"):
                n_commits = len(group["git_commits"])
                print(f"      [git] {n_commits} commit{'s' if n_commits > 1 else ''}: {group['git_commits'][0][:50]}")
                if n_commits > 1:
                    print(f"         ... et {n_commits - 1} autres")

        total_original += original
        total_adjusted += adjusted

    h_orig, m_orig = divmod(total_original, 60)
    h_adj, m_adj = divmod(total_adjusted, 60)
    print("-" * 75)
    print(f"Sessions locales: {len(to_push)} -> Timesheets Kimai: {len(consolidated)}")
    if total_original != total_adjusted:
        direction = "shrink" if total_adjusted < total_original else "expand"
        print(f"Total: {h_orig}h{m_orig:02d} -> {h_adj}h{m_adj:02d} (après {direction})")
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
    sm = SessionManager()

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

    # Ajouter les sessions actives PRO
    active_pro_count = 0
    now = datetime.now()
    for sid, session in sm.get_all_sessions().items():
        if session.get("folder_type") == "pro" and session.get("project_id"):
            begin = datetime.fromisoformat(session["begin"])
            date = begin.strftime("%Y-%m-%d")

            # Filtrer par date si spécifiée
            if args.date and date != args.date:
                continue

            elapsed = int((now - begin).total_seconds() / 60)

            # Convertir en format entrée
            active_entry = {
                "date": date,
                "folder": session.get("folder"),
                "folder_type": "pro",
                "project_id": session.get("project_id"),
                "project_name": session.get("project_name"),
                "activity": session.get("current_activity_estimate", config.get("default_activity")),
                "begin": session.get("begin"),
                "end": now.isoformat(),
                "billed_minutes": elapsed,
                "real_minutes": elapsed,
                "pushed_to_kimai": False,
                "is_active": True  # Marqueur pour l'affichage
            }
            to_show.append(active_entry)
            active_pro_count += 1

    if not to_show:
        print(f"Aucune entrée à afficher ({date_label})")
        return

    if active_pro_count > 0:
        print(f"(inclut {active_pro_count} session(s) active(s) PRO)")

    # Calculer ratios d'ajustement (shrink ou expand)
    by_date = {}
    for e in to_show:
        d = e.get("date", e.get("begin", "")[:10])
        if d not in by_date:
            by_date[d] = []
        by_date[d].append(e)

    daily_limit = config.get("daily_limit_hours", 8) * 60
    adjustment_ratios = {}
    for date, day_entries in by_date.items():
        total_minutes = sum(e.get("billed_minutes", 0) for e in day_entries)
        if is_weekday(date):
            # Jour de semaine : expand/shrink pour atteindre 8h
            adjustment_ratios[date] = logger.calculate_adjustment_ratio(
                total_minutes, daily_limit, date=date, expand=True
            )
        else:
            # Week-end : heures réelles (pas d'ajustement)
            adjustment_ratios[date] = 1.0

    consolidated = consolidate_entries(to_show, adjustment_ratios)
    display_consolidated(consolidated, adjustment_ratios, to_show, date_label, verbose=args.verbose)


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
    sm = SessionManager()

    # Remplir les jours vides si demandé
    fill_empty = getattr(args, 'fill_empty', None)
    if fill_empty:
        try:
            start_date, end_date = fill_empty.split(":")
            created = logger.fill_empty_weekdays(start_date, end_date)
            if created:
                # Grouper par date pour affichage
                by_day = {}
                for e in created:
                    d = e["date"]
                    if d not in by_day:
                        by_day[d] = []
                    by_day[d].append(e)
                print(f"Jours remplis ({len(by_day)} jours, {len(created)} entrées):")
                for d in sorted(by_day.keys()):
                    total_mins = sum(e["billed_minutes"] for e in by_day[d])
                    h, m = divmod(total_mins, 60)
                    src = by_day[d][0].get("filled_from", "?")
                    print(f"  {d}: {h}h{m:02d} (copié de {src})")
            else:
                print("Aucun jour vide à remplir dans la plage")
        except ValueError:
            print(f"Format invalide pour --fill-empty. Utilisez START:END (ex: 2024-01-01:2024-01-05)")
            return

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

    # Ajouter les sessions actives PRO
    active_sessions_to_close = {}  # sid -> entry data
    now = datetime.now()
    for sid, session in sm.get_all_sessions().items():
        if session.get("folder_type") == "pro" and session.get("project_id"):
            begin = datetime.fromisoformat(session["begin"])
            date = begin.strftime("%Y-%m-%d")

            # Filtrer par date si spécifiée
            if args.date and date != args.date:
                continue

            elapsed = int((now - begin).total_seconds() / 60)

            # Convertir en format entrée
            active_entry = {
                "date": date,
                "folder": session.get("folder"),
                "folder_type": "pro",
                "project_id": session.get("project_id"),
                "project_name": session.get("project_name"),
                "activity": session.get("current_activity_estimate", config.get("default_activity")),
                "begin": session.get("begin"),
                "end": now.isoformat(),
                "billed_minutes": elapsed,
                "real_minutes": elapsed,
                "pushed_to_kimai": False,
                "is_active": True,
                "session_id": sid
            }
            to_push.append(active_entry)
            active_sessions_to_close[sid] = active_entry

    active_pro_count = len(active_sessions_to_close)

    if not to_push and not args.pad and not fill_empty:
        print(f"Aucune entrée à pusher ({date_label})")
        return

    if not to_push:
        print(f"Aucune entrée à pusher ({date_label}), padding uniquement...")

    if active_pro_count > 0:
        print(f"(inclut {active_pro_count} session(s) active(s) PRO qui seront fermées)")

    # Grouper par date pour le shrink
    by_date = {}
    for e in to_push:
        d = e.get("date", e.get("begin", "")[:10])
        if d not in by_date:
            by_date[d] = []
        by_date[d].append(e)

    daily_limit = config.get("daily_limit_hours", 8) * 60

    # Calculer les ratios d'ajustement par jour (shrink ou expand)
    adjustment_ratios = {}
    for date, day_entries in by_date.items():
        total_minutes = sum(e.get("billed_minutes", 0) for e in day_entries)
        if is_weekday(date):
            # Jour de semaine : expand/shrink pour atteindre 8h
            adjustment_ratios[date] = logger.calculate_adjustment_ratio(
                total_minutes, daily_limit, date=date, expand=True
            )
        else:
            # Week-end : heures réelles (pas d'ajustement)
            adjustment_ratios[date] = 1.0

    # Consolider les entrées
    consolidated = consolidate_entries(to_push, adjustment_ratios)

    # Arrondir à 30 min pour le push Kimai, en respectant le total de 8h/jour
    # Grouper par date
    by_date_consolidated = {}
    for group in consolidated:
        d = group["date"]
        if d not in by_date_consolidated:
            by_date_consolidated[d] = []
        by_date_consolidated[d].append(group)

    for date, groups in by_date_consolidated.items():
        # Arrondir chaque entrée à 30 min
        for group in groups:
            original = group["adjusted_minutes"]
            rounded = round(original / 30) * 30
            if rounded == 0 and original > 0:
                rounded = 30
            group["rounded_minutes"] = rounded

        # Calculer le total arrondi
        total_rounded = sum(g["rounded_minutes"] for g in groups)

        # Ajuster pour atteindre exactement 8h (ou la limite) - seulement en semaine
        if is_weekday(date):
            target = daily_limit
            diff = target - total_rounded

            if diff != 0 and groups:
                # Ajuster la plus grande entrée
                largest = max(groups, key=lambda g: g["rounded_minutes"])
                largest["rounded_minutes"] = max(30, largest["rounded_minutes"] + diff)

        # Appliquer les arrondis
        for group in groups:
            group["adjusted_minutes"] = group["rounded_minutes"]
            group["kimai_end"] = group["kimai_begin"] + timedelta(minutes=group["adjusted_minutes"])

    # Afficher le résumé consolidé
    display_consolidated(consolidated, adjustment_ratios, to_push, date_label, verbose=True)

    # Créer le client Kimai
    client = KimaiClient(
        config["kimai_url"],
        config["auth_user"],
        config["auth_token"],
        dry_run=False
    )

    is_dry_run = config.get("dry_run", True) and not getattr(args, 'force', False)

    if is_dry_run:
        print("\n[DRY-RUN] Aucun push effectué. Désactivez dry_run dans config.json")
        if active_sessions_to_close:
            print(f"[DRY-RUN] {len(active_sessions_to_close)} session(s) active(s) seraient fermées")
    elif consolidated and not args.yes:
        confirm = input("\nConfirmer le push? (y/N) ")
        if confirm.lower() != 'y':
            print("Push annulé")
            return

    pushed_count = 0
    entries_marked = 0
    sessions_closed = 0

    # Fermer les sessions actives AVANT le push (les ajouter au log)
    if not is_dry_run and active_sessions_to_close:
        for sid, entry_data in active_sessions_to_close.items():
            # Ajouter au log local (pas encore marqué comme pushé)
            logger.add_entry(entry_data, pushed_to_kimai=False)
            # Retirer de sessions actives
            sm.sessions.pop(sid, None)
            sessions_closed += 1
        sm._save_all()
        print(f"\n{sessions_closed} session(s) active(s) fermée(s)")

    if not is_dry_run:
        for group in consolidated:
            activity_key = group.get("activity", config.get("default_activity"))
            activity_id = config["activity_mappings"].get(activity_key, {}).get("id")

            if not activity_id:
                print(f"  [!] Activité inconnue: {activity_key}")
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
                    e["adjusted_minutes"] = group["adjusted_minutes"] if group["ratio"] != 1.0 else None
                    e["adjustment_ratio"] = group["ratio"] if group["ratio"] != 1.0 else None
                    e["consolidated_with"] = len(group["entries"])
                    entries_marked += 1

                pushed_count += 1
                h, m = divmod(group['adjusted_minutes'], 60)
                print(f"  [OK] [{group['date']}] {group['project_name']} / {activity_key} - {h}h{m:02d}")

            except Exception as ex:
                print(f"  [ERR] Erreur: {ex}")

    # Sauvegarder le log mis à jour
    logger._save()

    print(f"\n{pushed_count} timesheets créés ({entries_marked} entrées locales marquées)")

    # Padding si demandé
    if args.pad:
        # Dates à traiter : celles des entrées pushées ou la date spécifiée
        dates_to_pad = list(by_date.keys()) if by_date else []
        if args.date and args.date not in dates_to_pad:
            dates_to_pad.append(args.date)
        if not dates_to_pad:
            dates_to_pad = [datetime.now().strftime("%Y-%m-%d")]

        for date in dates_to_pad:
            total_pushed = logger.get_kimai_pushed_minutes(date)
            missing = daily_limit - total_pushed

            if missing > 0:
                pad_activity = config.get("pad_activity", "reunion")
                pad_activity_id = config["activity_mappings"].get(pad_activity, {}).get("id")

                if not pad_activity_id:
                    print(f"  [!] Activité de padding inconnue: {pad_activity}")
                    continue

                h, m = divmod(missing, 60)
                print(f"  → [{date}] Padding {pad_activity} +{h}h{m:02d} (projet {args.pad})")

                if config.get("dry_run", True):
                    continue

                # Créer un timesheet pour le padding
                pad_begin = datetime.strptime(date, "%Y-%m-%d").replace(hour=17, minute=0)
                pad_end = pad_begin + timedelta(minutes=missing)

                try:
                    client.create_timesheet(
                        project_id=args.pad,
                        activity_id=pad_activity_id,
                        begin=pad_begin,
                        end=pad_end,
                        description="Padding automatique"
                    )
                    print(f"  [OK] Padding créé")
                except Exception as ex:
                    print(f"  [ERR] Erreur padding: {ex}")


def cmd_describe(args):
    """Ajoute une description à la session en cours ou à une entrée"""
    folder = args.folder if hasattr(args, 'folder') and args.folder else None
    sm = SessionManager(folder=folder)
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
            print("Usage: nectime describe <index> \"texte de description\"")
            return

        logger.log["entries"][real_idx]["description"] = args.text
        logger._save()
        print(f"Description ajoutée à l'entrée [{args.index}]")

    elif sm.is_active():
        # Ajouter à la session en cours
        session = sm._get_session()
        if not args.text:
            current = session.get("description", "(aucune)")
            print(f"Description actuelle: {current}")
            print("Usage: nectime describe \"texte de description\"")
            return

        session["description"] = args.text
        sm._set_session(session)
        print(f"Description ajoutée à la session en cours")

    else:
        print("Aucune session active. Utilisez: nectime describe <index> \"texte\"")


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
    folder = args.folder if hasattr(args, 'folder') and args.folder else None
    sm = SessionManager(folder=folder)

    if not sm.is_active():
        print("Aucune session active pour ce process")
        return

    session = sm._get_session()
    folder_type = session.get("folder_type", "pro")
    activity_mappings = config.get("activity_mappings", {})

    if args.activity_key:
        # Vérifier selon le type de projet
        if folder_type == "pro":
            # Projet pro: doit être une activité Kimai connue
            if args.activity_key not in activity_mappings:
                print(f"Erreur: Projet PRO - l'activité doit être connue de Kimai.")
                print("Activités disponibles:")
                for key, val in activity_mappings.items():
                    print(f"  {key}: {val.get('name', 'N/A')}")
                return

            sm.update_activity(estimate=args.activity_key)
            print(f"Activité changée: {args.activity_key}")
            print(f"  -> {activity_mappings[args.activity_key].get('name')}")

        else:
            # Projet local (perso/pending): activité custom acceptée
            if args.activity_key in activity_mappings:
                # C'est une clé Kimai connue
                sm.update_activity(estimate=args.activity_key)
                print(f"Activité changée: {args.activity_key}")
                print(f"  -> {activity_mappings[args.activity_key].get('name')}")
            else:
                # Activité custom (texte libre)
                sm.update_activity(estimate=args.activity_key)
                print(f"Activité custom définie: {args.activity_key}")
                print("  (Projet LOCAL - activité libre)")

    else:
        # Afficher l'activité actuelle
        current = session.get("current_activity_estimate", "non définie")
        breakdown = session.get("activity_breakdown", {})

        # Vérifier si c'est une activité Kimai ou custom
        is_kimai = current in activity_mappings
        type_label = "PRO" if folder_type == "pro" else "LOCAL"

        print(f"Projet: {type_label}")
        print(f"Activité actuelle: {current}", end="")
        if is_kimai:
            print(f" ({activity_mappings[current].get('name')})")
        else:
            print(" (custom)")

        if breakdown:
            print("Répartition:")
            for act, mins in sorted(breakdown.items(), key=lambda x: -x[1]):
                print(f"  {act}: ~{mins}min")

        # Aide contextuelle
        if folder_type == "pro":
            print("\nActivités Kimai disponibles:")
            for key in activity_mappings.keys():
                print(f"  {key}")
        else:
            print("\n(Projet local: activité libre acceptée)")


def cmd_set(args):
    """Configure le type de projet pour un dossier"""
    config = load_config()

    folder_type = args.type
    project_id = args.project_id
    folder = args.folder or os.getcwd()
    folder = os.path.normpath(folder)
    custom_activity = getattr(args, 'activity', None)
    sm = SessionManager(folder=folder)

    # Validation: activité custom seulement pour projets locaux
    if custom_activity and folder_type == "pro":
        # Vérifier que l'activité est connue de Kimai
        if custom_activity not in config.get("activity_mappings", {}):
            print(f"Erreur: Pour un projet 'pro', l'activité doit être connue de Kimai.")
            print(f"Activités valides: {', '.join(config.get('activity_mappings', {}).keys())}")
            return

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

    # Sauvegarder le mapping (custom_activity uniquement pour perso)
    final_custom = custom_activity if folder_type == "perso" else None
    set_folder_mapping(folder, folder_type, project_id, project_name, final_custom)

    # Mettre à jour TOUTES les sessions de ce folder
    folder_sessions = sm.get_folder_sessions()
    if folder_sessions:
        for sid, session in folder_sessions.items():
            session["folder_type"] = folder_type
            session["project_id"] = project_id
            session["project_name"] = project_name
            if final_custom:
                session["current_activity_estimate"] = final_custom
            sm.sessions[sid] = session
        sm._save_all()
        print(f"{len(folder_sessions)} session(s) mise(s) à jour: {project_name} ({folder_type})")
    else:
        print(f"Mapping enregistré: {folder}")
        print(f"  Type: {folder_type}")
        if project_name:
            print(f"  Projet: {project_name}")

    if folder_type == "pro" and project_id:
        print(f"  -> Les heures seront pushées vers Kimai")
        if custom_activity:
            print(f"  -> Activité: {custom_activity}")
    elif folder_type == "perso":
        print(f"  -> Les heures resteront en local")
        if final_custom:
            print(f"  -> Activité custom: {final_custom}")
    elif folder_type == "pending":
        print(f"  -> En attente d'un projet Kimai")
    elif folder_type == "off":
        print(f"  -> Ce dossier sera ignoré")


def cmd_cleanup(args):
    """Ferme les sessions anciennes (> 12h ou jour different)"""
    config = load_config()
    sm = SessionManager()
    logger = LocalLogger()

    closed = sm.cleanup_old_sessions(logger=logger, config=config)

    if not closed:
        print("Aucune session ancienne a fermer")
        return

    print(f"Sessions fermees et loggees: {len(closed)}")
    for s in closed:
        h, m = divmod(s.get('billed_minutes', 0), 60)
        sid_short = s.get('session_id', '?')[:8]
        print(f"  - {s['project_name']} ({h}h{m:02d}) [{sid_short}]")


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Kimay Bridge - Auto-logging Kimai")
    subparsers = parser.add_subparsers(dest="command", help="Commandes")

    # status
    sp = subparsers.add_parser("status", help="Statut des sessions")
    sp.add_argument("--all", "-a", action="store_true", help="Afficher toutes les sessions")
    sp.add_argument("--folder", "-f", help="Dossier (défaut: cwd)")
    sp.set_defaults(func=cmd_status)

    # start
    sp = subparsers.add_parser("start", help="Démarrer une session")
    sp.add_argument("--folder", "-f", help="Dossier (défaut: cwd)")
    sp.add_argument("--project", "-p", type=int, help="Project ID Kimai")
    sp.add_argument("--type", "-t", choices=["pro", "perso", "pending", "off"])
    sp.set_defaults(func=cmd_start)

    # stop
    sp = subparsers.add_parser("stop", help="Arrêter la session")
    sp.add_argument("--folder", "-f", help="Dossier (défaut: cwd)")
    sp.add_argument("--activity", "-a", help="Clé d'activité (ex: dev_embarque)")
    sp.add_argument("--dry-run", action="store_true", help="Ne pas poster sur Kimai")
    sp.set_defaults(func=cmd_stop)

    # cancel
    sp = subparsers.add_parser("cancel", help="Annuler la session")
    sp.add_argument("--folder", "-f", help="Dossier (défaut: cwd)")
    sp.set_defaults(func=cmd_cancel)

    # cleanup
    sp = subparsers.add_parser("cleanup", help="Nettoyer les sessions orphelines")
    sp.set_defaults(func=cmd_cleanup)

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
    sp.add_argument("--activity", "-a", help="Activité custom (perso) ou clé Kimai (pro)")
    sp.set_defaults(func=cmd_set)

    # activity
    sp = subparsers.add_parser("activity", help="Changer ou afficher l'activité en cours")
    sp.add_argument("activity_key", nargs="?", help="Clé d'activité (ex: dev_embarque)")
    sp.add_argument("--folder", "-f", help="Dossier (défaut: cwd)")
    sp.set_defaults(func=cmd_activity)

    # push
    sp = subparsers.add_parser("push", help="Push les entrées vers un bridge")
    sp.add_argument("bridge", nargs="?", default="kimai", help="Bridge cible (défaut: kimai)")
    sp.add_argument("--date", "-d", help="Date (YYYY-MM-DD, défaut: toutes)")
    sp.add_argument("--yes", "-y", action="store_true", help="Confirmer automatiquement")
    sp.add_argument("--force", "-f", action="store_true", help="Ignorer dry_run et pusher réellement")
    sp.add_argument("--pad", type=int, metavar="PROJECT_ID", help="Compléter à 8h avec réunion sur ce projet")
    sp.add_argument("--fill-empty", metavar="START:END", help="Remplir jours vides (ex: 2024-01-01:2024-01-05)")
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
