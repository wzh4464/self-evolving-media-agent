<div align="center">

# self-evolving-media-agent

**An anime library agent that finds the gaps in its own rules — and writes new ones.**

English | [中文](README.zh.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

</div>

---

Dedupe, rename, align to TMDB, purge dead torrents — across a qBittorrent +
AutoBangumi library. Every rule it ships with came from a real problem that cost
real hours. And when it hits something *no* rule can explain, it drafts a new
rule, proves it on your actual library, and only then puts it into service.

## The problems it was built from

This started as a multi-day manual cleanup that kept hitting the same walls:

| Problem | What it looked like by hand |
|---|---|
| Torrents added outside AutoBangumi | Missing `ab:` tag → never auto-renamed → re-fixing a fresh batch every few days |
| Two releases of one episode | AutoBangumi retries the same rename every 60s, forever, never converging |
| Deduping by filename | AutoBangumi renames files — filenames lie, both ways |
| Trusting qBittorrent's `name` field | `renameFile` never updates it → hundreds of false "unrenamed" reports |
| Directory ≠ TMDB official title | Scraper matches the wrong show, or nothing at all |
| Dead torrents | Days of waiting before noticing zero seeders exist anywhere |

Each is now a rule. One command finds all of them.

## Self-evolution, with teeth

```
diagnose → find residue (a real problem no rule can act on)
         → LLM drafts a declarative rule
         → shadow-validate against your live library
         → promote to .agents/rules/  |  or reject with reasons
```

Shadow validation is a hard gate. **All five** must pass:

1. It actually covers the samples it was drafted for
2. **Zero false positives** — it must not match a single already-correct file
3. Hits ≤ 3× the residue cluster size (catches over-broad rules)
4. No overlap with an existing rule that can already act on those files
5. Action args match the executor contract, and **contain no placeholders**
   (`new_name: ""`, `tmdb_id: 0`, `title: "TBD"` are all rejected)

Real output from a first run against a 173-show / 2404-file library:

```
🚫 rejected: unrenamed-vcb-raw
   - matched 83 files, far beyond the 27-file residue — rule too broad
   - overlaps existing rule extras-in-library
🎉 promoted: vcb-studio-sp-shorts-unrenamed   (50 hits, zero false positives)
🎉 promoted: vcb-studio-preview-shorts-unrenamed  (12 hits, zero false positives)
```

Every proposal — promoted *and* rejected — is written up as an
[Agent Note](.agents/notes/) recording the reasoning, the alternatives
considered, and the risks. Rejections are kept so the same bad idea doesn't
get re-proposed next week.

### Evolved rules are data, never code

The agent emits **declarative JSON**, interpreted by a fixed evaluator. It can
only compose conditions from a closed vocabulary of fields and operators — it
can never ask the kernel to execute arbitrary logic.

```json
{
  "id": "vcb-studio-sp-shorts-unrenamed",
  "kind": "special_episode_unrenamed",
  "match": {"all": [
    {"field": "parent_dir", "op": "eq", "value": ".shorts"},
    {"field": "filename", "op": "regex", "value": "^\\[VCB-Studio\\].*\\[SP\\d{2}_\\d{2}\\].*\\.mkv$"}
  ]},
  "action": null
}
```

This isn't a stylistic preference. The agent runs fully autonomous, with delete
permission, over files that are often **unrecoverable** (dead torrents, long-finished
seasons). Torrent and file names are untrusted external input. `exec()`-ing model
output under those conditions is not a risk worth taking — the reasoning is
written up in [an Agent Note](.agents/notes/implemented/architecture/2026-08-17-declarative-rule-dsl.md).

## Safety

Even in full-auto mode:

- **Quarantine, not deletion.** Removals move to `state/trash/<date>/`, restorable
  for 30 days before being purged for real.
- **Per-run caps.** More than 50 files or 200 GB in one pass? The whole batch is
  skipped and flagged — a wrong rule can't run away.
- **Full audit trail.** Every action, skip, and failure lands in `state/audit.jsonl`.
- **Defense in depth.** Action args are validated at proposal time *and* again at
  execution time.
- **Parse-failure guard.** If more than 3 files claim to be the same episode, that's
  treated as a parsing bug, not a duplicate — and nothing gets deleted.

That last one isn't hypothetical. On its very first dry run this agent proposed
deleting 11 episodes of a 12-episode season, because collection torrents share one
name across all their files. The dry run caught it; the fix and the reasoning are
[recorded as a bug-fix note](.agents/notes/implemented/bug-fix/2026-08-17-collection-torrent-episode-collapse.md).
Two more self-defects surfaced the same way. **Rules will be wrong — full autonomy
is only safe because being wrong is recoverable.**

## Quick start

```sh
git clone https://github.com/wzh4464/self-evolving-media-agent
cd self-evolving-media-agent
cp .env.example .env && chmod 600 .env   # fill in your endpoints and keys
uv sync
```

```sh
uv run media-agent scan                # what's in the library right now
uv run media-agent diagnose            # run every rule, read-only
uv run media-agent apply --dry-run     # preview fixes
uv run media-agent apply               # execute
uv run media-agent evolve              # draft rules for the blind spots
uv run media-agent run                 # one full autonomous cycle
```

Start with `diagnose`, then `apply --dry-run`. Only flip `AUTO_APPLY=true` once
you've read what it wants to do.

A launchd plist for a 6-hourly cycle is in [`deploy/`](deploy/).

### What it needs

| Service | Required? | Without it |
|---|---|---|
| qBittorrent WebUI | yes | — |
| AutoBangumi | optional | Loses `ab:` tag rules and subscription awareness |
| TMDB API key | optional | Title-alignment rules skip (free at [themoviedb.org](https://www.themoviedb.org/settings/api)) |
| LLM (OpenAI-compatible) | optional | Self-evolution skips; everything else works |

Built against DeepSeek V4 Pro, but any OpenAI-compatible chat endpoint works —
set `LLM_BASE` / `LLM_MODEL`.

## Architecture

Everything is a plugin; capabilities are separated from providers; decisions
settle into Agent Notes. The shape is borrowed from
[deepseek-harness](https://github.com/deepseek-ai).

```
media_agent/
  kernel.py       Finding/Action/Context/Registry + the declarative rule interpreter
  naming.py       Episode parsing, normalization, quality ranking — every line earned
  dedup.py        Content hashing (size + first/last 8 MB)
  scan.py         Disk + qBittorrent + AutoBangumi → one LibraryState
  plugins/        The nine built-in detectors
  actions.py      Executor + quarantine + caps + audit log
  evolution.py    Residue → propose → shadow-validate → promote
.agents/
  notes/          Agent Notes, path-encoded {lifecycle}/{class}/date-title.md
  rules/          Evolved rules (JSON), auto-mounted on the next run
```

Read [AGENTS.md](AGENTS.md) before changing anything — it lists six constraints
that are not up for debate, each one paid for in lost hours.

## Honest limitations

- **Tuned for Chinese-subtitled anime releases.** Episode parsing covers the naming
  conventions of mikan/dmhy fansub groups. Western TV releases mostly work; your
  mileage will vary.
- **The DSL can't express everything.** Some genuine problems can't be written as a
  rule with the current field/operator vocabulary. Those land in
  `.agents/notes/rejected/` and are the evidence for extending the vocabulary — a
  human decision, never the model's.
- **TMDB is treated as authoritative.** If it has no localized title for a show, the
  agent writes an NFO pinning the TMDB ID rather than guessing.
- **Only tested on one library.** 173 shows, 2404 files, macOS. Expect rough edges
  elsewhere — issues and PRs welcome.

## License

[MIT](LICENSE)
