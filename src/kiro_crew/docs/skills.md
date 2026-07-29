# Skills

Skills are markdown files that give KiroCrew specialized knowledge for specific
workflows. They live in `~/.kiro/crew/skills/` as `SKILL.md` files.

## How Skills Work

- **Always-on skills**: full content injected into every session (use sparingly)
- **On-demand skills**: summary loaded at session start; full content read when
  the topic comes up
- **Triggered skills**: automatically loaded when the user's message matches
  trigger words (≥70% word overlap)

## Skill Structure

```
~/.kiro/crew/skills/
├── my-skill/
│   └── SKILL.md
├── utils/
│   └── url-shortener/
│       ├── SKILL.md
│       └── shorten.sh    # auxiliary scripts
└── code/
    └── git-workflow/
        └── SKILL.md
```

Each skill is a directory containing at least `SKILL.md`. Nested directories
are supported.

## SKILL.md Format

```markdown
---
name: my-skill
description: What this skill does (shown in summaries)
always: false
triggers: keyword1, keyword2, multi word trigger
---

# Skill Content

Instructions, examples, and reference material that the agent reads
when this skill is activated.
```

### Frontmatter Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Display name |
| `description` | Yes | One-line summary (shown in skill listings) |
| `always` | No | `true` to inject full content every session |
| `triggers` | No | Comma-separated trigger phrases for auto-loading. Prefix with `!` for negative triggers (e.g. `!test` excludes when "test" appears) |

## Creating Skills

### Via Dashboard

Overview → Skills tab → "+ New" button → enter name and content.

### Via Chat

Ask KiroCrew: "Create a skill called X that does Y"

### Manually

Create `~/.kiro/crew/skills/my-skill/SKILL.md` with frontmatter and content.

## Built-in Skills

KiroCrew ships with built-in skills that are synced from the project's
`skills/` directory on startup. These cover common workflows like URL
shortening, code search, and writing assistance.

## Skill Sources (Priority Order)

1. `$KIROCREW_PROJECT_DIR/skills/` — project-level (edit without rebuilding)
2. Built-in skills bundled in the Python package

Both are synced into `~/.kiro/crew/skills/` on startup. Newer source files
overwrite older ones (mtime-based). User-created skills in
`~/.kiro/crew/skills/` persist as long as they don't share a name with a
project-level or built-in skill — if they do, the source version wins when
it's newer.

## Linked Skill Repos (share skills across a team)

Link a git repo and KiroCrew mirrors its skills into this instance. Everyone who
links the same repo gets the same skills, so one person's skill becomes a team
standard instead of a file people copy around.

Settings → Skills → **Linked skill repos**: enter the repo URL, a short
kebab-case name, the branch, and (optionally) the subdirectory holding the
skills. Linking clones immediately and reports how many skills it found, so a
wrong URL, branch, or subdirectory fails right there instead of silently
producing an empty skill set.

```json
{
  "skills": {
    "sources": [
      {
        "name": "team-skills",
        "repo": "https://github.com/your-org/team-skills.git",
        "branch": "main",
        "subdir": "skills",
        "enabled": true
      }
    ]
  }
}
```

How it behaves:

- **Mirrored, not copied.** The clone lives at
  `$KIROCREW_HOME/skill-sources/<name>/` and is mounted as an additional
  read-only skills root. Shared skills are *lower* precedence than your own, so
  a skill you wrote locally with the same name always wins and a sync can never
  overwrite your work.
- **A sync is a mirror update, not a merge.** Syncing fetches and hard-resets the
  mirror, which is how an upstream skill *deletion* propagates. Any local edit
  inside the mirror is discarded — edit shared skills in the repo, not here.
- **Failures keep the last good state.** If a sync fails, the previously synced
  commit stays mounted and the row reports the failure plus the commit it is
  still serving. Stale, but never silent.
- **When it syncs.** On link, on demand via the Sync button, and in the
  background at gateway startup.
- **Auth and host trust.** The clone uses the gateway host's existing git
  credentials (SSH agent or credential helper), so a private team repo works if
  `git clone` works for you on that machine. The host must be a well-known public
  forge or a host you configured as an app registry.

Only skills are shared. Memory, lessons, and personal settings are never part of
a linked repo.
