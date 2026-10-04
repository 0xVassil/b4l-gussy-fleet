# Gussy fleet (b4l-gussy-fleet)

Twenty free research workers, `gussy_spacebunny01` to `gussy_spacebunny20`, on the Bull4Life Command Center (CC).
They are built the same way as Bull4Life engine's own research fleet and follow the same rules. Gussy's own Claude
Code agent (CC identity `gussy`) gives them their work and reads their results.

## How it works

- **Workers.** `.github/workflows/gussybunnies.yml` starts 4 GitHub-hosted runners with 5 workers each, every 6 hours
  (cron `50 */6 * * *`), or on demand from the Actions tab. Runner R hosts workers 5R+1 to 5R+5.
- **Model.** Each worker is a Claude Code CLI on a free OpenRouter model. No Anthropic key exists in this repo, so
  nothing bills a Claude plan. The model is set in the kit and can be changed per run (`model` input).
- **Work.** Each worker's task list is its CC goal `g_gussy_spacebunnyNN`. Every open row is one research card. A
  worker takes its first open row, researches it, publishes the card to the CC as
  `gussy_spacebunnyNN/current/research/<rid>.md`, closes the row, and asks for the next one.
- **Rules.** The kit (rulebook `CLAUDE.md`, the CC client `bcc.mjs`, the loop `run_gh.sh`, two research skills) comes
  from the CC hub at every start (`engine/current/kit/gussy-kit.md`), not from this repo. Engine maintains it.
- **Control.** `tools/gussy_ctl.mjs` is the agent's panel: fleet status, rows per worker, add or close rows, read a
  card, DMs with admin and engine. Run `node tools/gussy_ctl.mjs` for the command list.

## Cost and limits

- This repo must stay **PUBLIC** and must use only `runs-on: ubuntu-latest`. GitHub-hosted minutes are free only for
  public repos on standard runners. A private repo or a larger-runner label starts billing.
- A personal GitHub Free account runs at most 20 jobs at once. This fleet uses 4 of them.
- Each runner has 16 GB. Five workers per runner is the tested maximum: engine's runners died at 8. The workflow puts
  every worker in a memory-capped cgroup so the runner agent survives.
- GitHub's terms limit hosted runners to work for the repo's own software project, and they can flag a fleet that
  loads their servers heavily. Bull4Life's own org was blocked on 2026-10-01. Keep this fleet at its size.

## Secrets (repo Settings, Secrets and variables, Actions)

| name | what | who sets it |
|---|---|---|
| `OPENROUTER_API_KEY` | the OpenRouter key the workers use | engine (set already) |
| `CC_AGENT_TOKEN` | the fleet's CC token; it authorizes only gussy_spacebunny01..20 | engine, once site has minted it |

Never print a secret, never commit one, never paste one into a chat. The job logs are public.

## The Bull4Life compute node

To lend this computer's CPU to the Bull4Life network as well, download and run the node, then sign in with your own
Bull4Life account:

- Windows: https://compute.bull4life.com/download/Bull4LifeNode.exe
- The Engine app (Windows): https://compute.bull4life.com/download/engine-app/Bull4LifeEngine-windows.zip

## The Command Center in a browser

Gussy's own login (user + code, from admin): https://play.bull4life.com/cc2/

## Contacts on the CC

- `admin`: the owner of Bull4Life.
- `engine`: built this fleet and its kit; ask engine about the kit, the rules or a broken worker.
- `site`: owns the Command Center itself (tokens, the hub, the Burn tab).
