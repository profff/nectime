#!python3
"""
Hook wrapper V2 pour Claude Code (modele par blocs).

Cable sur UserPromptSubmit : a chaque message, on estime l'activite et on
ouvre/prolonge le bloc (dossier, activite, jour) via nectime2.record_activity.
SessionEnd : no-op (les blocs sont deja persistes a chaque message).

Ne bloque jamais Claude Code : tout est sous try/except, sortie silencieuse.
"""

import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

import nectime as nt


def output_message(message: str):
    """Message visible : systemMessage (stdout JSON) + stderr."""
    print(json.dumps({"systemMessage": message}))
    print(message, file=sys.stderr)


def handle_event(hook_input: dict):
    event = hook_input.get("hook_event_name", "")
    if event != "UserPromptSubmit":
        return  # SessionEnd & co : rien a faire, les blocs sont deja ecrits

    cwd = os.path.normpath(hook_input.get("cwd", "."))
    prompt = hook_input.get("prompt", "")
    config = nt.load_config()

    mapping = nt.resolve_folder(cwd)
    ftype = mapping.get("folder_type", "unknown") if mapping else "unknown"

    if ftype == "off":
        return  # dossier ignore : silencieux, on ne spamme pas le prompt a chaque message

    # Activite : estimateur -> sinon activite courante du jour -> sinon defaut
    prev = nt.current_activity(cwd)  # None si premier bloc du jour pour ce dossier
    activity = (nt.estimate_activity(prompt, cwd, config)
                or prev
                or config.get("default_activity", "dev_applicatif"))

    nt.record_activity(cwd, activity, datetime.now())

    # Messages : ligne en debut de journee, puis seulement sur changement d'activite
    if prev is None:
        if ftype == "pro" and mapping and mapping.get("project_id"):
            output_message(f"NECTIME : {mapping.get('project_name', cwd)} - {activity} (Kimai)")
        else:
            name = os.path.basename(cwd.rstrip("/\\"))
            output_message(f"NECTIME : dossier non attribue '{name}' - {activity} - /nectime set pro <id>")
    elif activity != prev:
        output_message(f"NECTIME : activite {prev} -> {activity}")


def read_stdin_with_timeout(timeout_sec=3):
    """Lit stdin avec timeout (evite les blocages sur PowerShell/Windows)."""
    result = {"data": None}

    def _read():
        try:
            result["data"] = sys.stdin.read()
        except Exception:
            pass

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)
    return result["data"]


def main():
    raw = read_stdin_with_timeout()
    if not raw:
        return
    try:
        hook_input = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, ValueError):
        return
    try:
        handle_event(hook_input)
    except Exception:
        pass  # jamais bloquer Claude Code


if __name__ == "__main__":
    main()
