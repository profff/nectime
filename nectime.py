#!python3
"""
nectime V2 - tracking temps par blocs (jour, activite) pour Claude Code.

Modele :
  - Un bloc = {folder, activity, start, stop}. Pas de type ni project_id stockes :
    ils sont resolus a la volee depuis folder_mappings.json (conversion
    unknown->pro gratuite, retroactive).
  - Un message ouvre un nouveau bloc si (jour different OU activite differente
    OU dossier different), sinon il prolonge le stop du bloc courant.
  - Pas de dedup parallele : deux projets en parallele = les deux temps comptes.

Consolidation / cap (au push, affichable en list) :
  - Ceiling 12h par (jour, projet). Floor 8h par jour-total (proportionnel).
    Zone morte [8h,12h]. Week-end : heures reelles, aucun ajustement.

CLI (dry-run par defaut, -y/--yes pour executer) :
  - set <pro|unknown|off> [project_id]   type du dossier courant
  - list                                 temps consolides par jour/projet/activite
  - push                                 pousse les jours clos vers Kimai
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
DATA_DIR = SCRIPT_DIR / "data"
BLOCKS_FILE = DATA_DIR / "blocks.json"
FOLDER_MAPPINGS_FILE = DATA_DIR / "folder_mappings.json"

DEFAULT_EXPAND_HOURS = 8   # floor (jour-total)
DEFAULT_SHRINK_HOURS = 12  # ceiling (jour, projet)


# =============================================================================
# KIMAI CLIENT (minimal)
# =============================================================================

class KimaiClient:
    def __init__(self, url: str, auth_user: str, auth_token: str):
        import requests  # lazy
        self.url = url.rstrip('/')
        self.session = requests.Session()
        self.session.headers.update({
            'X-AUTH-USER': auth_user,
            'X-AUTH-TOKEN': auth_token,
            'Content-Type': 'application/json',
        })

    def _get(self, endpoint, params=None):
        resp = self.session.get(f"{self.url}/api/{endpoint}", params=params)
        resp.raise_for_status()
        return resp.json()

    def get_project(self, project_id: int) -> dict:
        return self._get(f"projects/{project_id}")

    def create_timesheet(self, project_id, activity_id, begin, end, description=None):
        data = {
            "project": project_id,
            "activity": activity_id,
            "begin": begin.strftime("%Y-%m-%dT%H:%M:%S"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        if description:
            data["description"] = description
        resp = self.session.post(f"{self.url}/api/timesheets", json=data)
        resp.raise_for_status()
        return resp.json()


# =============================================================================
# CONFIG / MAPPINGS / BLOCKS
# =============================================================================

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        print("Config non trouvee (config.json).")
        sys.exit(1)
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def load_folder_mappings() -> dict:
    if FOLDER_MAPPINGS_FILE.exists():
        with open(FOLDER_MAPPINGS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_folder_mappings(mappings: dict):
    DATA_DIR.mkdir(exist_ok=True)
    with open(FOLDER_MAPPINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(mappings, f, indent=2, ensure_ascii=False)


def resolve_folder(folder: str, mappings: dict = None) -> Optional[dict]:
    """Mapping du dossier (ou d'un parent). None si non mappe."""
    if mappings is None:
        mappings = load_folder_mappings()
    current = os.path.normpath(folder)
    while current:
        if current in mappings:
            return mappings[current]
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def load_blocks() -> list:
    if BLOCKS_FILE.exists():
        with open(BLOCKS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []


def save_blocks(blocks: list):
    DATA_DIR.mkdir(exist_ok=True)
    tmp = BLOCKS_FILE.with_suffix(".json.tmp")
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(blocks, f, indent=2, ensure_ascii=False)
    os.replace(tmp, BLOCKS_FILE)  # ecriture atomique -> plus de queue JSON orpheline


# =============================================================================
# ENREGISTREMENT D'UN BLOC (appele par le hook ; ici pour test/reuse)
# =============================================================================

def record_activity(folder: str, activity: str, ts: datetime = None) -> dict:
    """Ouvre ou prolonge le bloc (folder, activite, jour). Off -> ignore."""
    ts = ts or datetime.now()
    folder = os.path.normpath(folder)
    mapping = resolve_folder(folder)
    if mapping and mapping.get("folder_type") == "off":
        return {"ignored": True}

    day = ts.date().isoformat()
    blocks = load_blocks()
    for b in blocks:
        if (b["folder"] == folder and b["activity"] == activity
                and b["start"][:10] == day):
            b["stop"] = ts.isoformat()
            save_blocks(blocks)
            return b
    block = {"folder": folder, "activity": activity,
             "start": ts.isoformat(), "stop": ts.isoformat()}
    blocks.append(block)
    save_blocks(blocks)
    return block


def current_activity(folder: str, ts: datetime = None) -> Optional[str]:
    """Activite du bloc le plus recent de ce dossier aujourd'hui (fallback hook)."""
    ts = ts or datetime.now()
    folder = os.path.normpath(folder)
    day = ts.date().isoformat()
    best_act, best_stop = None, None
    for b in load_blocks():
        if b["folder"] == folder and b["start"][:10] == day:
            if best_stop is None or b["stop"] > best_stop:
                best_stop, best_act = b["stop"], b["activity"]
    return best_act


def estimate_activity(prompt: str, cwd: str, config: dict) -> Optional[str]:
    """Estime l'activite depuis le prompt + les fichiers recemment modifies.
    Scoring par mots-cles/extensions (config['auto_activity']['rules']). Pas de LLM."""
    rules = config.get("auto_activity", {}).get("rules", {})
    prompt_lower = (prompt or "").lower()
    scores = {}

    for activity, rule in rules.items():
        score = 0
        for keyword in rule.get("keywords", []):
            if keyword.lower() in prompt_lower:
                score += 2
        for ext in rule.get("extensions", []):
            if ext in prompt_lower:
                score += 3
        if score > 0:
            scores[activity] = score

    # Fichiers modifies < 5 min dans cwd (1 niveau, plafonne pour les gros projets)
    try:
        import time
        now = time.time()
        scanned = 0
        for entry in os.scandir(cwd):
            if scanned >= 200:
                break
            if entry.is_file():
                scanned += 1
                try:
                    if now - entry.stat().st_mtime < 300:
                        ext = os.path.splitext(entry.name)[1].lower()
                        for activity, rule in rules.items():
                            if ext in rule.get("extensions", []):
                                scores[activity] = scores.get(activity, 0) + 1
                except OSError:
                    pass
    except OSError:
        pass

    return max(scores, key=scores.get) if scores else None


# =============================================================================
# CONSOLIDATION + CAP
# =============================================================================

def is_weekday(day: str) -> bool:
    return datetime.strptime(day, "%Y-%m-%d").weekday() < 5


def _block_minutes(b: dict) -> float:
    start = datetime.fromisoformat(b["start"])
    stop = datetime.fromisoformat(b["stop"])
    return max(0.0, (stop - start).total_seconds() / 60.0)


def consolidate(blocks: list, config: dict, mappings: dict = None) -> list:
    """Retourne des groupes {date, folder_type, project_id, project_name,
    activity, begin, raw_minutes, adj_minutes}.
    Cap par jour-total avec zone morte [expand, shrink] : <8h on etire a 8h,
    >12h on ramene a 12h, entre les deux on garde le reel. Week-end : reel."""
    if mappings is None:
        mappings = load_folder_mappings()

    expand_min = config.get("expand_limit_hours", DEFAULT_EXPAND_HOURS) * 60
    shrink_min = config.get("shrink_limit_hours", DEFAULT_SHRINK_HOURS) * 60

    # 1) agreger par (jour, projet, activite) ; begin = plus tot des blocs du groupe
    groups = {}
    for b in blocks:
        day = b["start"][:10]
        mapping = resolve_folder(b["folder"], mappings)
        ftype = mapping.get("folder_type", "unknown") if mapping else "unknown"
        project_id = mapping.get("project_id") if mapping else None
        project_name = (mapping.get("project_name") if mapping
                        else os.path.basename(b["folder"].rstrip("/\\")))
        key = (day, ftype, project_id, project_name, b["activity"])
        if key not in groups:
            groups[key] = {
                "date": day, "folder_type": ftype, "project_id": project_id,
                "project_name": project_name, "activity": b["activity"],
                "begin": b["start"], "raw_minutes": 0.0, "adj_minutes": 0.0,
            }
        groups[key]["raw_minutes"] += _block_minutes(b)
        if b["start"] < groups[key]["begin"]:
            groups[key]["begin"] = b["start"]

    result = list(groups.values())

    # 2) cap par jour-total, zone morte [expand, shrink]
    by_day = {}
    for g in result:
        by_day.setdefault(g["date"], []).append(g)

    for day, gs in by_day.items():
        day_total = sum(g["raw_minutes"] for g in gs)
        if not is_weekday(day) or day_total == 0:
            ratio = 1.0
        elif day_total < expand_min:
            ratio = expand_min / day_total       # floor -> 8h
        elif day_total > shrink_min:
            ratio = shrink_min / day_total        # shrink -> 12h
        else:
            ratio = 1.0                            # [8h, 12h] : tel quel
        for g in gs:
            g["adj_minutes"] = g["raw_minutes"] * ratio

    result.sort(key=lambda g: (g["date"], str(g["project_name"]), g["activity"]))
    return result


# =============================================================================
# AFFICHAGE
# =============================================================================

def fmt(minutes: float) -> str:
    m = int(round(minutes))
    return f"{m // 60}h{m % 60:02d}"


def cmd_list(args):
    config = load_config()
    blocks = load_blocks()
    if args.date:
        blocks = [b for b in blocks if b["start"][:10] == args.date]
    if args.today:
        today = datetime.now().date().isoformat()
        blocks = [b for b in blocks if b["start"][:10] == today]
    groups = consolidate(blocks, config)
    if not groups:
        print("Aucun bloc.")
        return

    today = datetime.now().date().isoformat()
    by_day = {}
    for g in groups:
        by_day.setdefault(g["date"], []).append(g)

    for day in sorted(by_day.keys()):
        gs = by_day[day]
        raw_total = sum(g["raw_minutes"] for g in gs)
        adj_total = sum(g["adj_minutes"] for g in gs)
        open_flag = "  (jour en cours, non poussable)" if day == today else ""
        print(f"\n{day}  brut {fmt(raw_total)} / ajuste {fmt(adj_total)}{open_flag}")
        # regrouper par projet
        by_proj = {}
        for g in gs:
            by_proj.setdefault((g["project_name"], g["folder_type"], g["project_id"]), []).append(g)
        for (pname, ftype, pid), pgs in sorted(by_proj.items(), key=lambda x: str(x[0][0])):
            pushable = ftype == "pro" and pid is not None and day != today
            tag = "" if pushable else f"  [{ftype}{'' if pid else '/sans-id'} - non poussable]"
            praw = sum(g["raw_minutes"] for g in pgs)
            print(f"  {pname} ({fmt(praw)}){tag}")
            for g in sorted(pgs, key=lambda g: g["activity"]):
                print(f"      {g['activity']:<18} brut {fmt(g['raw_minutes'])}  ->  {fmt(g['adj_minutes'])}")


# =============================================================================
# PUSH
# =============================================================================

def _round30(minutes: float) -> int:
    r = int(round(minutes / 30.0) * 30)
    return r if r > 0 else (30 if minutes > 0 else 0)


def cmd_push(args):
    config = load_config()
    blocks = load_blocks()
    today = datetime.now().date().isoformat()

    groups = consolidate(blocks, config)
    # jours clos uniquement + pro + project_id
    pushable = [g for g in groups
                if g["date"] < today
                and g["folder_type"] == "pro"
                and g["project_id"] is not None]
    if args.date:
        pushable = [g for g in pushable if g["date"] == args.date]

    if not pushable:
        print("Rien a pousser (jours clos pro avec project_id).")
        return

    # arrondi 30min + synthese begin/end (empilage des 9h00 par jour)
    by_day = {}
    for g in pushable:
        by_day.setdefault(g["date"], []).append(g)

    plan = []
    for day in sorted(by_day.keys()):
        for g in by_day[day]:
            minutes = _round30(g["adj_minutes"])
            if minutes == 0:
                continue
            activity_id = config.get("activity_mappings", {}).get(g["activity"], {}).get("id")
            # Depart a l'heure de debut reelle, etire/reduit par la duree ajustee.
            # Chevauchements autorises sur Kimai ; debordement minuit assume.
            begin = datetime.fromisoformat(g["begin"])
            plan.append({**g, "minutes": minutes, "activity_id": activity_id,
                         "begin": begin, "end": begin + timedelta(minutes=minutes)})

    print("Plan de push :")
    for p in plan:
        warn = "" if p["activity_id"] else "  [!] activite sans id Kimai - SKIP"
        span = f"{p['begin'].strftime('%H:%M')}-{p['end'].strftime('%d/%H:%M')}"
        print(f"  [{p['date']}] {p['project_name']} / {p['activity']} "
              f"-> {fmt(p['minutes'])}  {span} (proj {p['project_id']}, act {p['activity_id']}){warn}")

    if not args.yes:
        print("\n[DRY-RUN] Rien pousse. Relancer avec -y pour executer.")
        return

    client = KimaiClient(config["kimai_url"], config["auth_user"], config["auth_token"])
    pushed_keys = set()
    ok = 0
    for p in plan:
        if not p["activity_id"]:
            continue
        try:
            client.create_timesheet(p["project_id"], p["activity_id"],
                                    p["begin"], p["end"])
            pushed_keys.add((p["date"], p["project_id"], p["activity"]))
            ok += 1
            print(f"  [OK] [{p['date']}] {p['project_name']} / {p['activity']} {fmt(p['minutes'])}")
        except Exception as ex:
            print(f"  [ERR] {p['date']} {p['project_name']}: {ex}")

    # nettoyage des blocs pousses
    remaining = []
    for b in blocks:
        mapping = resolve_folder(b["folder"])
        pid = mapping.get("project_id") if mapping else None
        key = (b["start"][:10], pid, b["activity"])
        if key in pushed_keys:
            continue
        remaining.append(b)
    save_blocks(remaining)
    print(f"\n{ok} timesheet(s) cree(s). {len(blocks) - len(remaining)} bloc(s) nettoye(s).")


# =============================================================================
# SET
# =============================================================================

def cmd_set(args):
    config = load_config()
    folder = os.path.normpath(args.folder or os.getcwd())
    mappings = load_folder_mappings()
    ftype = args.type

    if ftype == "off":
        blocks = load_blocks()
        n = sum(1 for b in blocks if os.path.normpath(b["folder"]) == folder)
        print(f"set off : {folder}")
        print(f"  -> dossier ignore + {n} bloc(s) supprime(s)")
        if not args.yes:
            print("\n[DRY-RUN] Relancer avec -y pour executer.")
            return
        mappings[folder] = {"folder_type": "off", "project_id": None,
                            "project_name": os.path.basename(folder), "custom_activity": None}
        save_folder_mappings(mappings)
        save_blocks([b for b in blocks if os.path.normpath(b["folder"]) != folder])
        print("OK.")
        return

    project_id = args.project_id
    project_name = os.path.basename(folder)
    if ftype == "pro" and project_id is not None:
        try:
            client = KimaiClient(config["kimai_url"], config["auth_user"], config["auth_token"])
            project_name = client.get_project(project_id).get("name", project_name)
        except Exception:
            pass  # best effort, nom = dossier sinon

    print(f"set {ftype} : {folder}")
    print(f"  -> project_id={project_id}  project_name={project_name}")
    if ftype == "pro" and project_id is None:
        print("  [i] pro sans project_id : logge mais non poussable tant qu'aucun id.")
    if not args.yes:
        print("\n[DRY-RUN] Relancer avec -y pour executer.")
        return

    old = mappings.get(folder, {})
    mappings[folder] = {
        "folder_type": ftype,
        "project_id": project_id,
        "project_name": project_name,
        "custom_activity": old.get("custom_activity"),
    }
    save_folder_mappings(mappings)
    print("OK.")


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(prog="nectime", description="Tracking temps par blocs (V2)")
    sub = parser.add_subparsers(dest="command")

    sp = sub.add_parser("set", help="Type du dossier courant")
    sp.add_argument("type", choices=["pro", "unknown", "off"])
    sp.add_argument("project_id", nargs="?", type=int, help="ID projet Kimai (si pro)")
    sp.add_argument("--folder", "-f", help="Dossier (defaut: cwd)")
    sp.add_argument("--yes", "-y", action="store_true", help="Executer (sinon dry-run)")
    sp.set_defaults(func=cmd_set)

    sp = sub.add_parser("list", help="Temps consolides par jour/projet/activite")
    sp.add_argument("--date", "-d", help="Filtrer un jour (YYYY-MM-DD)")
    sp.add_argument("--today", "-t", action="store_true", help="Aujourd'hui uniquement")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("push", help="Pousser les jours clos vers Kimai")
    sp.add_argument("--date", "-d", help="Filtrer un jour (YYYY-MM-DD)")
    sp.add_argument("--yes", "-y", action="store_true", help="Executer (sinon dry-run)")
    sp.set_defaults(func=cmd_push)

    args = parser.parse_args()
    if not args.command:
        args.command = "list"
        args.date = None
        args.today = False
        cmd_list(args)
    else:
        args.func(args)


if __name__ == "__main__":
    main()
