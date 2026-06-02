# nectime — notes de redesign

> Mémo de conception pour `nectime.py` (time-tracker maison de Claude Code, en service depuis janv. 2026, push vers Kimai).
> Design only, **pas de code écrit** tant que ce n'est pas tranché. Déplacé ici (depuis la mémoire DeviceLayer) le 2026-06-02.
>
> **2026-06-02 : décision V2 = rewrite.** Nouveau fichier (~300-400 lignes) qui réutilise tel quel `KimaiClient` (nectime.py:31-110), la couche config + `folder_mapping` (taxonomie des dossiers, c'est de la donnée), et la capture via `hook_wrapper.py` + l'estimateur d'activité existant. On réécrit : modèle de données, consolidation/summary, surface CLI. Rupture franche du format local (historique déjà sur Kimai → archiver l'ancien `data/`, pas de migration). Voir section **V2** ci-dessous, qui fait foi ; les sections « Direction validée » plus bas sont l'historique de réflexion.

## V2 — modèle par blocs (arrêté 2026-06-02)

### Modèle de données
- Un **bloc** = `{ folder, project_id?, activity, start_ts, stop_ts }`.
- **Activité** : estimée par l'estimateur existant (gardé tel quel, « marche tant bien que mal », on n'y touche pas).
- **Frontière de bloc** : un nouveau message ouvre un nouveau bloc si **jour différent ET/OU activité différente** ; sinon le message courant ne fait que pousser `stop_ts` du bloc en cours. → tue l'inflation inter-jours par construction (bug des 197h).
- **Pas de dédup parallèle** (point tranché) : si deux projets tournent en parallèle, on garde les deux temps. 10h sur P1 + 10h sur P2 = 20h dans la journée, **assumé**. « C'est pas parce que c'est un agent IA qui bosse que c'est pas du taf. » Kimai sert à savoir **où est passé le temps** (pilotage interne, billes pour le CIR), **pas** à facturer le client → l'inflation par parallélisme n'a aucune conséquence.

### Format de stockage (arrêté 2026-06-02)
- **Un seul `blocks.json`** qui grossit (volume faible, pas de découpage mensuel).
- Un bloc ne stocke **que** : `folder`, `activity`, `start`, `stop`. **Pas** de `type` ni de `project_id` dans le bloc.
- `type` (pro/unknown/off) et `project_id` sont **résolus à la volée** depuis `folder_mapping` au moment du `list`/`push`. → la conversion rétroactive unknown→pro est **gratuite** : on relit le mapping courant, pas besoin de réécrire les vieux blocs.
- `off` n'est jamais stocké : un dossier `off` ne produit aucun bloc et ses blocs existants sont supprimés au moment du `set off`.

```jsonc
// blocks.json
[
  { "folder": "D:/dev/PARAMETRIX", "activity": "dev_embarque",
    "start": "2026-06-02T09:12:33", "stop": "2026-06-02T16:40:01" }
]
```

### Code existant à réutiliser dans le V2
- `KimaiClient` — `nectime.py:31-110` (client REST Kimai, tel quel).
- **Estimateur d'activité** — `estimate_activity(prompt, cwd, config)` dans **`hook_wrapper.py:166`**, piloté par `config["auto_activity"]` (scoring mots-clés). Fonction quasi pure → réutilisable, éventuellement extraite dans un module partagé.
- **Activité → `activity_id` Kimai** — table `config["activity_mappings"][activity]["id"]` (cf. usage `nectime.py:793`). Le maillon de push existe déjà.
- Couche **config + `folder_mapping`** (`nectime.py:557-634`) — taxonomie des dossiers, c'est de la donnée, on garde.
- Capture d'activité côté hook — `hook_wrapper.py` (`update_activity`, déclenché sur UserPromptSubmit).

### Types de dossier (remplace pro/perso/pending/off)
Trois types seulement : **`pro`**, **`unknown`** (défaut), **`off`**.
- `pro` **avec** `project_id` → on logge **et** on peut pousser. Le passage en pro convertit **rétroactivement** les blocs `unknown` passés du dossier en poussables (point 6).
- `pro` **sans** `project_id` → on logge mais on ne peut pas pousser (pas de cible).
- `unknown` → on logge en attendant l'attribution, **pas poussable**.
- `off` (= l'ancien « perso ») → on **ne logge plus rien**, on **dégage les blocs existants** du dossier. Le hook reste **silencieux** sur un dossier off (pas de message à chaque prompt : ce serait du spam et contraire au sens de « off »). ⚠️ `set off` est destructif → soumis à la règle dry-run/`-y`.

### Day-cap / ajustement (au **push** uniquement)
- Appliqué **uniquement au push** (et affiché à titre indicatif dans `list`, qui montre brut + ajusté).
- **Par jour-total, zone morte** (tranché 2026-06-02, retour à du simple après avoir essayé le par-projet) : on calcule `sum(jour)` sur **tous** les blocs du jour, puis un **ratio unique** appliqué à toutes les entrées du jour :
  - `sum < 8h` → ratio `8h / sum` (floor, étire à 8h)
  - `sum > 12h` → ratio `12h / sum` (shrink, ramène à 12h)
  - `[8h, 12h]` → ratio 1 (temps réel, on ne touche rien)
- Le par-projet (ceiling 12h/projet) a été **abandonné** : trop de complications, et le slotting (jour, activité) tue déjà l'inflation des sessions oubliées (le `stop` = dernier message, une session sans message ne gonfle pas). Le cap par jour suffit.
- Config : `expand_limit_hours=8`, `shrink_limit_hours=12`.
- Week-end : **heures réelles, pas d'ajustement** (on ne se facture pas 8h un samedi).
- **Floor à 8h assumé** : une journée à 2h est étirée à 8h. « Je glande pas le reste du temps », 99% du boulot passe par un Claude Code lancé sur le projet (même pour du câblage → un CC ouvert juste pour loguer le temps ; activité estimée approximative, tant pis).
- Conséquence parallélisme : une journée multi-projets > 12h est ramenée à 12h **au prorata** (on garde la répartition, on perd la magnitude absolue — OK car Kimai sert à voir *où* est passé le temps, pas la facture).

### Pas de description / commentaire Kimai
Olivier n'utilise pas le champ description. L'option « résumer chaque bloc via un LLM et concaténer » est jugée **trop lourde** → **abandonnée**. On pousse des entrées nues. (Piste gratuite si besoin un jour : générer depuis `get_git_commits` du jour, déjà dispo.)

### Pas d'édition pré-push (YAGNI)
Pas de commande `edit`. Cas jamais rencontré ; si une mauvaise attribution arrive, correction directe **dans Kimai**. On reverra le moment venu.

### Règle transverse : dry-run par défaut
**Toutes** les commandes sont en **dry-run par défaut** (preview de ce qui se passerait). `-y`/`--yes` pour exécuter réellement. Couvre le push, le `set off` destructif, etc. — un seul garde-fou uniforme, plus de footgun.

### Push
- Pousse les jours **clos** (< aujourd'hui) ; le jour courant reste vivant (`stop_ts` bouge encore) → pas de double-push.
- Ne pousse que les blocs `pro` **avec** `project_id`.
- Après push réussi → **nettoyage des blocs poussés**.

### Surface CLI (3 commandes)
- `/nectime set <pro|unknown|off> [project_id]` — type du dossier courant (+ id projet si pro). Doit aussi résoudre l'activité Kimai (estimateur + mapping existant). Dry-run par défaut.
- `/nectime list` — temps consolidés **par jour / par projet / par activité** (temps bruts ; cap éventuellement affiché). Fait aussi office de `status` (filtre « aujourd'hui »).
- `/nectime push` — dry-run par défaut, `-y` pour pousser.

### Récap des bornes (toutes tranchées)
- **Tout par jour-total**, un ratio unique par jour : floor 8h, ceiling 12h, zone morte `[8h,12h]`.
- **Synthèse push** : chaque entrée part de l'**heure de début réelle** du bloc, durée = ajustée. Chevauchements autorisés sur Kimai, débordement minuit assumé.
- **Week-end** : heures réelles, aucun ajustement.

→ **Tous les points de conception sont tranchés. Prochaine étape = squelette de code V2.**

## État d'implémentation (2026-06-02)
- **Cœur V2 écrit et testé** : `nectime2.py` (~370 lignes, coexiste avec l'ancien `nectime.py`). Tests : `test_v2.py` (consolidation/cap/list/push dry) + `test_record.py` (ouverture/prolongation de blocs, parallèle, off). Les deux passent.
- Décision Kimai : **chevauchements autorisés** sur l'instance (réglage déjà activé) → au push, chaque entrée part de **9h00 + sa durée**, pas d'empilage, pas de débordement minuit sur les journées parallèles.
- Comportement à connaître : le **floor 8h/jour-total inclut les blocs unknown/sans-id** dans le total du jour. Conséquence : si une partie du jour n'est pas attribuée, le projet poussable ne reçoit que sa part proportionnelle (le jour Kimai peut être < 8h tant que le reste est unknown). Auto-corrigé dès attribution (les blocs unknown deviennent poussables rétroactivement). Jugé acceptable, à revoir si ça gêne.
### SWAP EFFECTUÉ — V2 EN PROD (2026-06-02 ~17:50)
- **Renommage** : `nectime2.py`→`nectime.py`, `hook_wrapper2.py`→`hook_wrapper.py`. Anciens conservés en `nectime.v1.bak` / `hook_wrapper.v1.bak`. Import du hook corrigé (`import nectime`).
- **`settings.json`** inchangé (pointait déjà sur `hook_wrapper.py`) → le hook V2 est **live** : chaque UserPromptSubmit écrit/prolonge un bloc dans `data/blocks.json`.
- **Slash command** `~/.claude/commands/nectime.md` réécrit (list/set/push, dry-run par défaut) et basculé sur le python du venv (qui a `requests`).
- **Migration `folder_mappings.json`** : 16 `perso`→`off` (PARAMETRIX inclus — confirmé « du gros perso » par Olivier, reste off). `pro` inchangés (3 sans project_id restent loggés-non-poussables : Test-Portes-Z2N, TEXYS-ECOSYSTEM, HUMS).
- **Ancien `data/` archivé puis zippé** : `data/v1_archive/nectime_v1_data_2026-06-02.zip` (sessions.json + local_log.json + folder_mappings.v1.json ; bruts supprimés). local_log gardé pour l'historique fin (CIR). Le `folder_mappings.json` migré reste en service dans `data/`.
- **Push Kimai validé** en vrai : timesheet test `id=27164` (Hors-Projet, 2026-06-02 09:00-09:30) — à supprimer dans Kimai.
- Tests `test_v2.py` / `test_record.py` / `test_hook.py` : passent après renommage.
- **Restant (optionnel)** : supprimer la timesheet test dans Kimai ; après quelques jours d'usage réel, supprimer les `.v1.bak` et `data/v1_archive/` si tout va bien.

## Défaut de fond
La durée est calculée comme `real_minutes = last_activity - begin` (span horloge), pas le temps réellement bossé. Une session laissée ouverte plusieurs jours crache des totaux délirants (ex. **197h pour une session d'une semaine**). Les heuristiques shrink/expand (cap ~12h/j, floor ~8h/j) servent de cautère mais déforment dans les deux sens.

## Contrainte clé (clarifiée 2026-05-29)
Olivier est payé à la journée, sans heures sup, et les clients sont facturés à la journée. **La précision du temps bossé n'a donc aucune importance.** L'idée heartbeat/idle-timeout a été *rejetée* (sur-ingénierie). Le day-cap (shrink) est une *feature*, pas un bug. Seul compte : quel(s) projet(s) sur quel jour, et à la louche dans quelle proportion.

## Direction validée (conception)
- Suivre les dossiers **pro + sans-étiquette** ; les dossiers **perso** sont marqués perso et ignorés. Logger le non-mappé en pro ; quand une relation projet Kimai est trouvée plus tard, la binder et convertir les entrées rétroactivement.
- Slots indexés par **(jour, activité)**. Premier message d'un (jour, activité) fixe `begin` ; chaque message suivant écrase `end` ; seul même-jour + même-activité écrase, sinon nouveau slot. Tue l'inflation inter-jours par construction (le bug des 197h).
- Bruit d'activité (flip-flop en touchant un seul .md) géré mollement **au moment du résumé**, par regroupement des activités du jour — pas besoin que ce soit parfait.
- Ajustement par jour **asymétrique avec zone morte** : shrink seulement si jour > 16h (vers 16h), inflate seulement si jour < 8h (vers 8h), laisser `[8h, 16h]` intact. (Plafond relevé de 12h→16h le 2026-05-29 : les vraies longues journées d'Olivier dépassent souvent 12h, donc 12h amputerait du boulot réel. Le cap n'attrape plus que les sessions oubliées/non-fermées, pas les marathons honnêtes — l'inflation inter-jours est déjà tuée par le slotting (jour, activité).) Raison assumée : « c'est pas pour la compta, c'est pour mon ego » — garder des journées crédibles-mais-flatteuses, ne jamais afficher l'impossible. `calculate_adjustment_ratio` supporte déjà `expand_limit`/`shrink_limit` ; aujourd'hui les deux sont figés à 720 min — juste les découpler (480 = 8h, 960 = 16h) pour ouvrir la zone morte.
- Répartir un jour entre projets/activités pondéré par **nombre de messages** plutôt que par span horloge (évite qu'un projet juste survolé pèse autant qu'un projet pioché en parallèle). Heuristique imparfaite assumée, suffisante vu la facturation au jour. **Point encore en réflexion.**

## Simplification des options (ajouté 2026-06-02)
Reproche d'Olivier : **trop d'options, la CLI est obèse.** État actuel = **14 sous-commandes** :
`status, start, stop, cancel, cleanup, projects, activities, log, set, activity, push, edit, describe, summary`.

Objectif : dégraisser vers un cœur de ~5-6 commandes réellement utilisées au quotidien, et reléguer le reste en sous-options ou supprimer.

Pistes (à valider — rien de tranché) :
- **Cœur à garder** : `status` (défaut sans argument), `push`, `summary`, `set`. Ce sont les gestes quotidiens.
- **Cycle de vie session** `start` / `stop` / `cancel` : en grande partie automatisés par les hooks Claude Code. À questionner : sont-ils encore lancés à la main ? Si non → les masquer (commandes « avancées »), pas forcément les supprimer.
- **`projects` / `activities`** : pure introspection Kimai. Candidats à fusionner sous un seul `list [projects|activities]`, ou à mettre derrière un flag de `summary`/`set`.
- **`activity`** (afficher/changer l'activité courante) recoupe `set --activity` : doublon possible à fusionner.
- **`cleanup`** : maintenance ; peut devenir `status --cleanup` ou rester caché.
- **`edit` / `describe`** : édition d'entrées non-pushées. Utiles mais rares → ok à garder, mais à fiabiliser (cf. bug `cmd_edit` ci-dessous) avant de les exposer.
- Réflexe transverse : beaucoup de commandes répètent `--folder/-f` (défaut cwd) et `--date/-d`. Bon défaut déjà en place ; vérifier qu'aucune option n'oblige à taper ce qui devrait être implicite.
- **À faire d'abord** : instrumenter / se souvenir des commandes réellement tapées sur les 6 derniers mois pour décider *par l'usage*, pas à l'instinct. (Le log local ou l'historique shell peut servir.)

## Bugs connus (vus 2026-05-29)
1. `data/sessions.json` et `data/local_log.json` corrompus par une écriture courte par-dessus du contenu plus long (queue JSON orpheline) — corrigés à la main par troncature.
2. `cmd_edit` plante en `TypeError: unsupported format string passed to NoneType` sur les entrées où `project_name` est null (ex. dossiers H2).

## TODO connexe (déjà noté dans `TODO.md`)
Wrapper résilient `hook_safe.bat` retournant toujours exit 0, pour pouvoir modifier `hook_wrapper.py` sans bloquer les sessions Claude Code actives.
