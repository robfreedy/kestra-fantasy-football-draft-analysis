# Kestra Fantasy Football Draft Analysis

A [Kestra](https://kestra.io) flow that watches a live [Sleeper](https://sleeper.com)
fantasy football draft and tells you who is worth taking.

Every poll it pulls the draft board, scores the players still available, and
logs an alert when someone falls well past where they should have gone.

## How the recommendation works

Each player gets a **value score**: how many picks past their expected slot they
are still on the board.

```
value_score = current_overall_pick - player_overall_rank
```

A player ranked #4 who is somehow still there at pick #31 scores `+27` — that is
a steal. A negative score means taking them now would be a reach.

Rankings come from Sleeper's `search_rank`. A caveat worth knowing: **that field
is a search-popularity rank, not a true ADP.** It is heavily tied (one rank can
be shared by 100+ players) and it rates kickers and defenses far more highly
than they are actually drafted. The analyzer normalizes it into a dense 1..N
ordering so it is at least on the same scale as a pick number, and excludes
`K` and `DEF` by default. If you want real ADP, plug a different ranking source
into `build_overall_ranks()`.

## Layout

| Path | Purpose |
| --- | --- |
| `flows/fantasy-draft-assistant.yml` | The Kestra flow |
| `analyzer_scripts/fantasy_football_analyzer.py` | Draft analysis, deployed as a Namespace File |
| `requirements.txt` | Local development dependencies |

## Deploying

The flow runs the analyzer as a **Namespace File**, so both have to be uploaded.
Set `KESTRA` and `NS` to match your instance:

```bash
KESTRA=http://localhost:8080/api/v1/default
NS=test.robs-test
AUTH='admin@kestra.io:PASSWORD'

# 1. The analyzer script
curl -u "$AUTH" -X POST "$KESTRA/namespaces/$NS/files?path=/fantasy_football_analyzer.py" \
  -F "fileContent=@analyzer_scripts/fantasy_football_analyzer.py"

# 2. The flow
curl -u "$AUTH" -X POST "$KESTRA/flows" \
  -H "Content-Type: application/x-yaml" \
  --data-binary @flows/fantasy-draft-assistant.yml
```

Re-deploying an existing flow uses `PUT $KESTRA/flows/$NS/fantasy-draft-assistant`.

### Requirements on the Kestra instance

- The **`io.kestra.plugin:plugin-script-python`** plugin. If it is missing,
  install it and restart:
  ```bash
  docker exec <container> /opt/java/openjdk/bin/java -jar /app/kestra \
    plugins install io.kestra.plugin:plugin-script-python:LATEST \
    -p /app/plugins -c /etc/config/application-license.yml
  ```
- A working **Docker task runner** (the flow runs the analyzer in
  `python:3.13-slim`), which needs the Docker socket mounted into Kestra.

## Running it

Set `league_id` to the number in your Sleeper league URL
(`https://sleeper.com/leagues/<league_id>/...`). That is **not** the same as
your user id.

Every id input also accepts the **full URL** you copied it from, so pasting the
address bar works.

| Input | Default | Purpose |
| --- | --- | --- |
| `league_id` | – | Sleeper league to watch |
| `draft_id` | – | Watch a draft directly — **how you follow a mock draft**; wins over `league_id` |
| `user_id` | – | Your Sleeper user id, so the flow says when you are on the clock |
| `alert_threshold` | `10.0` | Picks past rank before a player is flagged |
| `top_n` | `5` | How many recommendations per poll |
| `exclude_positions` | `K,DEF` | Positions to leave out |
| `sleeper_base_url` | Sleeper API | Override only to point at a test fixture |

### Mock drafts

A mock draft belongs to no league, so there is no league id to find it by — set
**`draft_id`** instead, from the mock's URL:

```
https://sleeper.com/draft/nfl/1272518225074081792
                              └────── draft_id ──────┘
```

Paste either the id or the whole URL. `draft_id` takes precedence over
`league_id` when both are set, so you can leave a league configured and
temporarily point the flow at a mock. Everything else — scoring, alerts, the
value threshold — behaves identically; the logs just say `Mock draft` and the
analysis carries `draft_info.is_mock: true`.

Two things differ in a mock, both handled:

- There are no rosters, so `on_the_clock.roster_id` is `null`. The draft slot
  is still reported.
- A mock against bots may have no draft order, so `on_the_clock.is_my_pick`
  can be `false` even on your turn. Set `user_id` if the mock has real
  participants and Sleeper will report the order.

To poll automatically during your draft, give `league_id` a default (or add an
`inputs:` block to the trigger) and enable the `poll_during_draft` trigger. It
runs every 30 seconds and will not overlap runs.

### The player pool cache

Sleeper's player endpoint is ~14MB and they ask that it be called at most once
per day. Because every Kestra task run starts with a clean working directory,
the analyzer prunes the pool to the fantasy-relevant fields (~630KB) and the
flow stores it back as the `sleeper_players_cache.json` Namespace File, which is
re-injected on later runs. The upload only happens when the pool was actually
refreshed, so a 30-second poll costs two small draft API calls.

## Local development

```bash
uv pip install -r requirements.txt

export SLEEPER_LEAGUE_ID=your_league_id
export SLEEPER_USER_ID=your_user_id     # optional
python analyzer_scripts/fantasy_football_analyzer.py

# ...or follow a mock draft, by id or by URL
SLEEPER_DRAFT_ID=https://sleeper.com/draft/nfl/1272518225074081792 \
  python analyzer_scripts/fantasy_football_analyzer.py
```

The script writes `draft_analysis.json`, prints a readable summary, and emits
the analysis in Kestra's `::{"outputs": ...}::` format. Every setting listed in
the table above has an environment-variable equivalent — see the module
docstring.
