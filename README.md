# nectime

Bridge de tracking temps automatique entre Claude Code et Kimai.

## Fonctionnement

nectime s'intègre aux hooks de Claude Code pour tracker automatiquement le temps passé sur chaque projet :

- **SessionStart** : Démarre une session de tracking
- **UserPromptSubmit** : Met à jour `last_activity` à chaque message
- **SessionEnd** : Ferme la session et log les heures

Les sessions sont identifiées par le `session_id` de Claude Code, permettant plusieurs sessions simultanées (même dans le même dossier).

## Installation

### 1. Configuration Kimai

Copier et éditer la config :
```bash
cp config.example.json config.json
```

Remplir :
- `kimai_url` : URL de votre instance Kimai
- `auth_user` : Votre email Kimai
- `auth_token` : Token API (Kimai > Profil > API)
- `activity_mappings` : Mapping des activités (récupérer les IDs via `nectime activities`)

Voir `config.example.json` pour un exemple complet avec l'auto-estimation d'activité.

### 2. Hooks Claude Code

Ajouter la section `hooks` dans `~/.claude/settings.json`.

Voir `claude-hooks.example.json` pour le template, adapter le chemin vers `hook_wrapper.py`.

### 3. Skill Claude Code (optionnel)

Pour utiliser `/nectime` dans Claude Code, ajouter le skill dans `.claude/settings.json` du projet ou global.

## Fichiers de configuration

| Fichier | Description |
|---------|-------------|
| `config.json` | Configuration locale (Kimai, activités, auto-estimation) |
| `config.example.json` | Template de configuration |
| `claude-hooks.example.json` | Template des hooks pour `~/.claude/settings.json` |

## Types de projets

Chaque dossier peut être configuré avec un type :

| Type | Description |
|------|-------------|
| `pro` | Push vers Kimai (nécessite project_id) |
| `perso` | Log local uniquement |
| `pending` | En attente d'assignation projet Kimai |
| `off` | Pas de tracking |

## Commandes CLI

### Statut
```bash
nectime status          # Sessions du dossier courant
nectime status --all    # Toutes les sessions actives
```

### Configuration projet
```bash
nectime set pro 137     # Associer au projet Kimai ID 137
nectime set perso       # Projet personnel (local only)
nectime set pending     # En attente
nectime set off         # Désactiver le tracking
```

### Activité
```bash
nectime activity                 # Voir l'activité courante
nectime activity dev_embarque    # Changer l'activité
```

### Log et Push
```bash
nectime log                      # Voir le log du jour
nectime log --date 2024-01-15    # Log d'une date

nectime summary                  # Résumé consolidé (dry-run)
nectime push                     # Push vers Kimai (avec confirmation)
nectime push --yes               # Push sans confirmation
```

### Gestion
```bash
nectime projects      # Lister les projets Kimai
nectime activities    # Lister les activités Kimai
nectime cleanup       # Fermer les sessions anciennes (> 12h)
nectime edit          # Modifier une entrée non pushée
nectime describe      # Ajouter une description
```

## Structure des données

```
nectime/
├── config.json              # Configuration Kimai
├── data/
│   ├── sessions.json        # Sessions actives {session_id: {...}}
│   ├── local_log.json       # Historique des sessions
│   └── folder_mappings.json # Mapping dossier -> projet
```

### sessions.json
```json
{
  "uuid-session-1": {
    "begin": "2024-01-05T09:00:00",
    "folder": "D:\\dev\\MonProjet",
    "folder_type": "pro",
    "project_id": 137,
    "project_name": "Mon Projet",
    "last_activity": "2024-01-05T10:30:00",
    "current_activity_estimate": "dev_embarque"
  }
}
```

## Workflow typique

1. **Ouvrir Claude Code** dans un dossier projet
   - nectime détecte le dossier et cherche un mapping existant
   - Si nouveau : suggère des projets Kimai similaires

2. **Configurer** (si nouveau dossier)
   ```
   /nectime set pro 137
   ```

3. **Travailler** - `last_activity` mis à jour automatiquement

4. **Fermer Claude Code** - Session loggée automatiquement

5. **Push en fin de journée**
   ```
   /nectime summary    # Vérifier
   /nectime push       # Envoyer vers Kimai
   ```

## Consolidation et Shrink

Lors du push :
- Les sessions sont **consolidées** par jour + projet + activité
- Si le total dépasse 8h/jour, un **shrink** proportionnel est appliqué
- Les commits git de la session sont ajoutés à la description Kimai

## Multi-session

nectime supporte plusieurs sessions Claude Code simultanées grâce au `session_id` fourni par Claude Code. Chaque instance a sa propre session indépendante.

```
Sessions actives: 2
---------------------------------------------------------------------------
  [51f133c0] CircuitForge              | 1h23 | CircuitForge
  [90497c65] Dynasteer                 | 0h45 | SW-Branche_MAIN
```
