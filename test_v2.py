#!python3
"""Test du coeur nectime2 sur des blocs bidons, sans toucher aux vraies donnees ni au reseau."""
import json
import tempfile
from pathlib import Path
from datetime import datetime

import nectime as n

# Rediriger les chemins vers un dossier temporaire
tmp = Path(tempfile.mkdtemp(prefix="nectime_test_"))
n.DATA_DIR = tmp
n.BLOCKS_FILE = tmp / "blocks.json"
n.FOLDER_MAPPINGS_FILE = tmp / "folder_mappings.json"

# Mappings bidons
mappings = {
    "D:\\dev\\P1": {"folder_type": "pro", "project_id": 101, "project_name": "Projet-P1", "custom_activity": None},
    "D:\\dev\\P2": {"folder_type": "pro", "project_id": 102, "project_name": "Projet-P2", "custom_activity": None},
    "D:\\dev\\NEW": {"folder_type": "unknown", "project_id": None, "project_name": "NEW", "custom_activity": None},
    "D:\\dev\\NOID": {"folder_type": "pro", "project_id": None, "project_name": "NOID", "custom_activity": None},
}
n.save_folder_mappings(mappings)


def block(folder, activity, day, start_h, stop_h):
    return {"folder": folder, "activity": activity,
            "start": f"{day}T{start_h:02d}:00:00", "stop": f"{day}T{stop_h:02d}:00:00"}


MON = "2026-05-25"   # lundi (semaine)
TUE = "2026-05-26"   # mardi (semaine)
SAT = "2026-05-30"   # samedi (week-end)
today = datetime.now().date().isoformat()

blocks = [
    # lundi : P1 seul, 2h -> floor jour-total a 8h
    block("D:\\dev\\P1", "dev_embarque", MON, 9, 11),
    # mardi : P1 14h -> ceiling 12h
    block("D:\\dev\\P1", "dev_embarque", TUE, 7, 21),
    # mardi : P2 en parallele 10h -> pas de ceiling (jour-total inflate, assume)
    block("D:\\dev\\P2", "dev_applicatif", TUE, 8, 18),
    # samedi : P1 5h -> heures reelles (pas d'ajustement)
    block("D:\\dev\\P1", "doc", SAT, 10, 15),
    # unknown : non poussable
    block("D:\\dev\\NEW", "dev_applicatif", MON, 14, 17),
    # pro sans id : non poussable
    block("D:\\dev\\NOID", "cablage", MON, 9, 12),
    # aujourd'hui : jour ouvert, non poussable
    block("D:\\dev\\P1", "dev_embarque", today, 9, 12),
]
n.save_blocks(blocks)

config = n.load_config()

print("=" * 70)
print("CONSOLIDATE (verif cap)")
print("=" * 70)
for g in n.consolidate(n.load_blocks(), config):
    print(f"  {g['date']} {g['folder_type']:<8} {str(g['project_name']):<12} "
          f"{g['activity']:<16} brut={g['raw_minutes']/60:.1f}h adj={g['adj_minutes']/60:.1f}h")

print()
print("=" * 70)
print("LIST")
print("=" * 70)
class A: date=None; today=False
n.cmd_list(A())

print()
print("=" * 70)
print("PUSH (dry-run)")
print("=" * 70)
class B: date=None; yes=False
n.cmd_push(B())

print()
print("(dossier test:", tmp, ")")
