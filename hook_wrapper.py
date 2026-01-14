#!python3
"""
Hook wrapper pour Claude Code
- SessionStart: demarre session (gere orphelines via cleanup)
- SessionEnd: termine session
- UserPromptSubmit: met a jour last_activity
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Ajouter le dossier parent au path pour importer nectime
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from nectime import (
    SessionManager, LocalLogger,
    load_config, get_folder_mapping, set_folder_mapping, load_folder_mappings,
    get_git_commits
)
# KimaiClient importé localement (lazy) pour éviter de charger 'requests' inutilement


def format_duration(minutes: int) -> str:
    """Formate une duree en heures:minutes"""
    h, m = divmod(minutes, 60)
    return f"{h}h{m:02d}"


def output_message(message: str):
    """Affiche un message via JSON systemMessage ET stderr"""
    result = {"systemMessage": message}
    print(json.dumps(result))
    print(message, file=sys.stderr)


def start_session(cwd: str, session_id: str):
    """Demarre une session (ferme les anciennes d'abord)"""
    config = load_config()
    sm = SessionManager(folder=cwd, session_id=session_id)
    logger = LocalLogger()

    # Fermer les sessions anciennes (> 12h ou jour different)
    closed = sm.cleanup_old_sessions(logger=logger, config=config)
    cleanup_msg = ""
    if closed:
        total_mins = sum(c.get("billed_minutes", 0) for c in closed)
        h, m = divmod(total_mins, 60)
        cleanup_msg = f" [{len(closed)} ancienne(s) fermee(s): {h}h{m:02d}]"

    # Si session deja active pour ce session_id, juste informer
    if sm.is_active():
        status = sm.status()
        msg = f"NECTIME: Session active - {status['project_name']} ({format_duration(status['elapsed_minutes'])}){cleanup_msg}"
        output_message(msg)
        return

    # Chercher le mapping
    mapping = get_folder_mapping(cwd)

    if mapping:
        folder_type = mapping.get("folder_type", "pro")
        project_id = mapping.get("project_id")
        project_name = mapping.get("project_name", "Unknown")

        if folder_type == "off":
            msg = f"NECTIME: Dossier ignore{cleanup_msg}"
            output_message(msg)
            return

        # Demarrer la session
        sm.start(folder_type, project_id, project_name)

        type_label = {"pro": "Kimai", "perso": "local", "pending": "en attente"}
        msg = f"NECTIME: Session demarree - {project_name} ({type_label.get(folder_type, folder_type)}){cleanup_msg}"
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
            from nectime import KimaiClient  # Import lazy - charge 'requests' uniquement ici
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
        sm.start("pending", None, folder_name)

        if matches:
            match_str = ", ".join([f"{p['name']} (id={p['id']})" for p in matches[:2]])
            msg = f"NECTIME: Nouveau dossier '{folder_name}' - Projets similaires: {match_str} - /nectime set pro <id>{cleanup_msg}"
        else:
            msg = f"NECTIME: Nouveau dossier '{folder_name}' - Aucun projet similaire - /nectime projects{cleanup_msg}"

        output_message(msg)


def stop_session(cwd: str, session_id: str):
    """Arrete la session (log local uniquement, jamais de push auto)"""
    config = load_config()
    sm = SessionManager(folder=cwd, session_id=session_id)
    logger = LocalLogger()

    if not sm.is_active():
        return

    session_data = sm.stop()
    # Utiliser l'activite definie pendant la session, sinon la valeur par defaut
    activity = session_data.get("current_activity_estimate") or config.get("default_activity", "dev_applicatif")
    session_data["activity"] = activity

    # Recuperer les commits git de la session
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


def estimate_activity(prompt: str, cwd: str, config: dict) -> str:
    """Estime l'activite basee sur le prompt et les fichiers recents"""
    auto_config = config.get("auto_activity", {})
    rules = auto_config.get("rules", {})

    prompt_lower = prompt.lower()
    scores = {}

    for activity, rule in rules.items():
        score = 0

        # Verifier les mots-cles dans le prompt
        for keyword in rule.get("keywords", []):
            if keyword.lower() in prompt_lower:
                score += 2

        # Verifier les extensions mentionnees dans le prompt
        for ext in rule.get("extensions", []):
            if ext in prompt_lower:
                score += 3

        if score > 0:
            scores[activity] = score

    # Regarder les fichiers recemment modifies (< 5 min) dans cwd
    # Limite: seulement 1 niveau de profondeur pour eviter les scans massifs
    try:
        import time
        now = time.time()
        scan_count = 0
        max_scan = 200  # Limite pour eviter les gros projets
        for entry in os.scandir(cwd):
            if scan_count >= max_scan:
                break
            if entry.is_file():
                scan_count += 1
                try:
                    mtime = entry.stat().st_mtime
                    if now - mtime < 300:  # 5 minutes
                        ext = os.path.splitext(entry.name)[1].lower()
                        for activity, rule in rules.items():
                            if ext in rule.get("extensions", []):
                                scores[activity] = scores.get(activity, 0) + 1
                except:
                    pass
    except:
        pass

    if scores:
        return max(scores, key=scores.get)
    return None


def update_activity(cwd: str, session_id: str, prompt: str = ""):
    """Met a jour last_activity (appele a chaque message)"""
    config = load_config()
    sm = SessionManager(folder=cwd, session_id=session_id)

    if not sm.is_active():
        return

    session = sm._get_session()
    now = datetime.now()
    session["last_activity"] = now.isoformat()

    # Auto-estimation de l'activite
    auto_config = config.get("auto_activity", {})
    if auto_config.get("enabled", False):
        interval = auto_config.get("interval_minutes", 15)
        last_estimate = session.get("last_activity_estimate_time")

        should_estimate = False
        if not last_estimate:
            should_estimate = True
        else:
            last_dt = datetime.fromisoformat(last_estimate)
            if (now - last_dt).total_seconds() >= interval * 60:
                should_estimate = True

        if should_estimate and prompt:
            estimated = estimate_activity(prompt, cwd, config)
            if estimated:
                current = session.get("current_activity_estimate")

                if estimated != current:
                    ask = auto_config.get("ask_before_change", False)

                    if ask and current:
                        # Demander confirmation via systemMessage
                        msg = f"NECTIME: Activite detectee: {estimated} (actuelle: {current or 'aucune'}). Changer? /nectime activity {estimated}"
                        output_message(msg)
                    else:
                        # Changer directement
                        session["current_activity_estimate"] = estimated
                        if current:
                            msg = f"NECTIME: Activite changee: {current} -> {estimated}"
                            output_message(msg)

                session["last_activity_estimate_time"] = now.isoformat()

    sm._set_session(session)

    # Silencieux si pas de changement


def main():
    try:
        hook_input = json.load(sys.stdin)
    except json.JSONDecodeError:
        hook_input = {}

    cwd = hook_input.get('cwd', '.')
    cwd = os.path.normpath(cwd)
    session_id = hook_input.get('session_id', '')
    event = hook_input.get('hook_event_name', 'unknown')
    source = hook_input.get('source', '')

    if not session_id:
        # Pas de session_id = on ne peut rien faire
        return

    try:
        if event == "SessionStart":
            if source in ("resume", "clear", "compact"):
                return
            start_session(cwd, session_id)

        elif event == "SessionEnd":
            stop_session(cwd, session_id)

        elif event == "UserPromptSubmit":
            prompt = hook_input.get('prompt', '')
            update_activity(cwd, session_id, prompt)
    except Exception:
        # Silencieux - ne jamais bloquer Claude Code
        pass


if __name__ == "__main__":
    main()
