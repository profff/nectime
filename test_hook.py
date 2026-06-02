#!python3
"""Test du hook V2 : rejoue des UserPromptSubmit, verifie les blocs ecrits."""
import tempfile
from pathlib import Path

import nectime as nt
import hook_wrapper as hk

tmp = Path(tempfile.mkdtemp(prefix="nectime_hook_"))
nt.DATA_DIR = tmp
nt.BLOCKS_FILE = tmp / "blocks.json"
nt.FOLDER_MAPPINGS_FILE = tmp / "folder_mappings.json"
nt.save_folder_mappings({
    "D:\\dev\\P1": {"folder_type": "pro", "project_id": 101, "project_name": "P1", "custom_activity": None},
    "D:\\dev\\OFF": {"folder_type": "off", "project_id": None, "project_name": "OFF", "custom_activity": None},
})


def msg(cwd, prompt):
    hk.handle_event({"hook_event_name": "UserPromptSubmit", "cwd": cwd,
                     "session_id": "s1", "prompt": prompt})


print(">>> sequence de messages")
msg("D:\\dev\\P1", "bug firmware stm32 sur l'uart")   # -> dev_embarque, 1er bloc (msg Kimai)
msg("D:\\dev\\P1", "ajoute un timer et un dma")         # -> dev_embarque, prolonge
msg("D:\\dev\\P1", "mets a jour le readme documentation")  # -> doc, nouveau bloc (msg change)
msg("D:\\dev\\NEW", "fais un script python")            # dossier non mappe -> non attribue
msg("D:\\dev\\OFF", "un truc perso")                    # off -> ignore, pas de bloc

print("\n>>> blocs ecrits :")
blocks = nt.load_blocks()
for b in blocks:
    print(f"  {b['folder']:<14} {b['activity']:<16} {b['start'][11:19]} -> {b['stop'][11:19]}")

acts = sorted({(Path(b['folder']).name, b['activity']) for b in blocks})
assert ("P1", "dev_embarque") in acts, "FAIL: bloc dev_embarque manquant"
assert ("P1", "doc") in acts, "FAIL: bloc doc manquant"
assert not any(Path(b['folder']).name == "OFF" for b in blocks), "FAIL: dossier off enregistre"
assert any(Path(b['folder']).name == "NEW" for b in blocks), "FAIL: dossier non mappe pas logge"
# P1/dev_embarque doit etre un seul bloc prolonge (2 messages), pas deux
p1_emb = [b for b in blocks if Path(b['folder']).name == "P1" and b['activity'] == "dev_embarque"]
assert len(p1_emb) == 1, f"FAIL: {len(p1_emb)} blocs P1/dev_embarque au lieu de 1"
print("\nOK : estimation correcte, prolongation, off ignore, dossier non mappe logge.")
