#!python3
"""Test de record_activity : ouverture/prolongation de blocs, parallele, off."""
import tempfile
from pathlib import Path
from datetime import datetime

import nectime as n

tmp = Path(tempfile.mkdtemp(prefix="nectime_rec_"))
n.DATA_DIR = tmp
n.BLOCKS_FILE = tmp / "blocks.json"
n.FOLDER_MAPPINGS_FILE = tmp / "folder_mappings.json"
n.save_folder_mappings({
    "D:\\dev\\P1": {"folder_type": "pro", "project_id": 101, "project_name": "P1", "custom_activity": None},
    "D:\\dev\\OFF": {"folder_type": "off", "project_id": None, "project_name": "OFF", "custom_activity": None},
})

def t(day, h, m=0):
    return datetime.fromisoformat(f"{day}T{h:02d}:{m:02d}:00")

D1, D2 = "2026-05-25", "2026-05-26"

# Messages interleaves : P1 et P2 en parallele le meme jour, puis changement d'activite, puis jour+1
seq = [
    ("D:\\dev\\P1", "dev_embarque", t(D1, 9, 0)),    # ouvre bloc A
    ("D:\\dev\\P2", "dev_applicatif", t(D1, 9, 30)),  # ouvre bloc B (autre dossier)
    ("D:\\dev\\P1", "dev_embarque", t(D1, 10, 0)),   # prolonge A
    ("D:\\dev\\P2", "dev_applicatif", t(D1, 11, 0)),  # prolonge B
    ("D:\\dev\\P1", "doc", t(D1, 14, 0)),            # change activite -> ouvre bloc C
    ("D:\\dev\\P1", "dev_embarque", t(D2, 9, 0)),    # jour+1 -> ouvre bloc D
    ("D:\\dev\\OFF", "dev_applicatif", t(D1, 9, 0)),  # dossier off -> ignore
]
for folder, act, ts in seq:
    n.record_activity(folder, act, ts)

blocks = n.load_blocks()
print(f"{len(blocks)} bloc(s) (attendu : 4) :")
for b in blocks:
    dur = (datetime.fromisoformat(b["stop"]) - datetime.fromisoformat(b["start"])).total_seconds() / 60
    print(f"  {b['folder']:<14} {b['activity']:<16} {b['start'][:16]} -> {b['stop'][11:16]}  ({dur:.0f} min)")

assert len(blocks) == 4, f"FAIL: {len(blocks)} blocs au lieu de 4"
# bloc A (P1 dev_embarque D1) prolonge a 10h -> 60 min
a = next(b for b in blocks if b["folder"].endswith("P1") and b["activity"] == "dev_embarque" and b["start"][:10] == D1)
assert a["stop"][11:16] == "10:00", f"FAIL: bloc A stop={a['stop']}"
# aucun bloc OFF
assert not any(b["folder"].endswith("OFF") for b in blocks), "FAIL: bloc off enregistre"
print("\nOK : 4 blocs, A prolonge a 10h, parallele P1/P2 distinct, OFF ignore.")
