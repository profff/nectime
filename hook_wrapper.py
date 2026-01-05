#!python3
"""
Hook wrapper pour Claude Code
- SessionStart: démarre session (gère orphelines)
- SessionEnd: termine session
- UserPromptSubmit: met à jour last_activity
"""

import json
import sys
from datetime import datetime
from pathlib import Path

# Ajouter le dossier parent au path pour importer nectime
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from nectime import (
    SessionManager, LocalLogger, KimaiClient,
    load_config, get_folder_mapping, set_folder_mapping, load_folder_mappings,
    get_git_commits
)


def format_duration(minutes: int) -> str:
    """Formate une durée en heures:minutes"""
    h, m = divmod(minutes, 60)
    return f"{h}h{m:02d}"


def output_message(message: str):
    """Affiche un message via JSON systemMessage ET stderr"""
    result = {"systemMessage": message}
    print(json.dumps(result))
    print(message, file=sys.stderr)


def close_orphan_session(sm: SessionManager, logger: LocalLogger, config: dict) -> str:
    """Ferme une session orpheline et retourne un message"""
    if not sm.is_active():
        return None

    # Vérifier si la session est orpheline (d'un autre jour ou > 12h)
    begin = datetime.fromisoformat(sm.session["begin"])
    last_activity = datetime.fromisoformat(sm.session["last_activity"])
    now = datetime.now()

    # Orpheline si: autre jour OU plus de 12h depuis le début
    is_orphan = (begin.date() != now.date()) or ((now - begin).total_seconds() > 12 * 3600)

    if not is_orphan:
        return None

    # Fermer avec last_activity comme heure de fin
    session_data = sm.session.copy()
    session_data["end"] = last_activity.isoformat()

    # Calculer les durées
    session_data["billed_minutes"] = int((last_activity - begin).total_seconds() / 60)
    session_data["real_minutes"] = session_data["billed_minutes"]

    # Activité par défaut
    activity = config.get("default_activity", "dev_applicatif")
    session_data["activity"] = activity

    # Logger localement (pas de push pour les orphelines)
    logger.add_entry(session_data, pushed_to_kimai=False)

    # Supprimer la session
    sm.session = None
    sm._save()

    duration = format_duration(session_data["billed_minutes"])
    return f"Session orpheline fermee: {session_data.get('project_name')} ({duration}) du {begin.strftime('%d/%m %H:%M')}"


def start_session(cwd: str):
    """Démarre une session (gère les orphelines)"""
    config = load_config()
    sm = SessionManager()
    logger = LocalLogger()

    # Gérer les sessions orphelines
    orphan_msg = close_orphan_session(sm, logger, config)

    # Si session encore active (pas orpheline), juste informer
    if sm.is_active():
        status = sm.status()
        msg = f"NECTIME: Session deja active - {status['project_name']} ({format_duration(status['elapsed_minutes'])})"
        if orphan_msg:
            msg = f"NECTIME: {orphan_msg} | Nouvelle session..."
        output_message(msg)
        return

    # Chercher le mapping
    mapping = get_folder_mapping(cwd)

    if mapping:
        folder_type = mapping.get("folder_type", "pro")
        project_id = mapping.get("project_id")
        project_name = mapping.get("project_name", "Unknown")

        if folder_type == "off":
            msg = f"NECTIME: Dossier ignore"
            if orphan_msg:
                msg = f"NECTIME: {orphan_msg}"
            output_message(msg)
            return

        # Démarrer la session
        sm.start(cwd, folder_type, project_id, project_name)

        type_label = {"pro": "Kimai", "perso": "local", "pending": "en attente"}
        msg = f"NECTIME: Session demarree - {project_name} ({type_label.get(folder_type, folder_type)})"
        if orphan_msg:
            msg = f"NECTIME: {orphan_msg} | {msg[7:]}"
        output_message(msg)

    else:
        # Nouveau dossier - fuzzy match
        path_parts = Path(cwd).parts
        search_terms = []
        for p in path_parts[-3:]:
            if len(p) <= 2:
                continue
            clean = p.replace("-main", "").replace("_main", "").replace("-dev", "")
            clean = clean.replace("_MAIN", "").replace("-MAIN", "")
            clean = clean.split("-")[0].split("_")[0]
            if len(clean) > 2:
                search_terms.append(clean)
            if clean != p and len(p) > 2:
                search_terms.append(p)

        matches = []
        try:
            client = KimaiClient(
                config["kimai_url"],
                config["auth_user"],
                config["auth_token"]
            )
            for term in search_terms:
                found = client.find_project_by_name(term)
                for p in found:
                    if p not in matches:
                        matches.append(p)
        except:
            matches = []

        folder_name = Path(cwd).name
        sm.start(cwd, "pending", None, folder_name)

        if matches:
            match_str = ", ".join([f"{p['name']} (id={p['id']})" for p in matches[:2]])
            msg = f"NECTIME: Nouveau dossier '{folder_name}' - Projets similaires: {match_str} - /nectime set pro <id>"
        else:
            msg = f"NECTIME: Nouveau dossier '{folder_name}' - Aucun projet similaire - /nectime projects"

        if orphan_msg:
            msg = f"NECTIME: {orphan_msg} | {msg[7:]}"
        output_message(msg)


def stop_session():
    """Arrête la session (log local uniquement, jamais de push auto)"""
    config = load_config()
    sm = SessionManager()
    logger = LocalLogger()

    if not sm.is_active():
        return

    session_data = sm.stop()
    # Utiliser l'activité définie pendant la session, sinon la valeur par défaut
    activity = session_data.get("current_activity_estimate") or config.get("default_activity", "dev_applicatif")
    session_data["activity"] = activity

    # Récupérer les commits git de la session
    folder = session_data.get("folder")
    if folder:
        commits = get_git_commits(
            folder,
            session_data.get("begin"),
            session_data.get("end")
        )
        if commits:
            session_data["git_commits"] = commits

    # Log local uniquement - push sera manuel via /nectime push
    logger.add_entry(session_data, pushed_to_kimai=False)

    duration = format_duration(session_data["billed_minutes"])
    folder_type = session_data.get("folder_type", "pro")
    type_label = {"pro": "-> /nectime push", "perso": "(local)", "pending": "(pending)"}

    # Info sur les commits
    commits_info = ""
    if session_data.get("git_commits"):
        n = len(session_data["git_commits"])
        commits_info = f" [{n} commit{'s' if n > 1 else ''}]"

    msg = f"NECTIME: Session terminee - {session_data.get('project_name')} - {duration}{commits_info} {type_label.get(folder_type, '')}"
    output_message(msg)


def update_activity():
    """Met à jour last_activity (appelé à chaque message)"""
    sm = SessionManager()

    if not sm.is_active():
        return

    # Mettre à jour le timestamp
    sm.session["last_activity"] = datetime.now().isoformat()
    sm._save()

    # Silencieux - pas de message


def main():
    try:
        hook_input = json.load(sys.stdin)
    except json.JSONDecodeError:
        hook_input = {}

    cwd = hook_input.get('cwd', '.')
    event = hook_input.get('hook_event_name', 'unknown')
    source = hook_input.get('source', '')

    if event == "SessionStart":
        if source in ("resume", "clear", "compact"):
            return
        start_session(cwd)

    elif event == "SessionEnd":
        stop_session()

    elif event == "UserPromptSubmit":
        update_activity()


if __name__ == "__main__":
    main()
