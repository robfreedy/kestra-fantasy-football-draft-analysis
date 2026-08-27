# Kestra Fantasy Football Draft Analysis

Kestra flows that watch a live fantasy football draft and tell you who is worth
taking, for **Sleeper** and **Yahoo Fantasy**.

Every poll pulls the draft board, scores the players still available, weights
them against the roster you have already drafted, and logs an alert when
someone good falls further than they should have.

| Flow | Provider | Auth |
| --- | --- | --- |
| `sleeper-draft-assistant` | Sleeper | none — the API is public |
| `yahoo-draft-assistant` | Yahoo Fantasy | OAuth 2.0 (client id, secret, refresh token) |

Both flows behave identically: same scoring, same roster logic, same log
format, same branches on draft state. Only the client differs.

## How the recommendation works

Each player gets a **value score**: how many picks past their expected slot
they are still on the board.

```
value_score = current_overall_pick - player_expected_pick
```

A player expected at #4 who is somehow still there at pick #31 scores `+27` —
that is a steal. A negative score means taking them now would be a reach.

### Your roster is taken into account

A raw value score will happily tell you to draft a second quarterback. So each
candidate is also classified against the roster you already hold, using the
league's own roster settings, and discounted if it does not fill a need:

| Classification | Meaning | Default discount |
| --- | --- | --- |
| `starter` | A starting slot at that position is still empty | none |
| `flex` | Dedicated slots are full, but a flex slot can take them | 8 picks |
| `depth` | Starting and flex slots are full — this is a bench pick | 20 picks, ×2 for the next one, ×3 after that |

```
1. Jeremiyah Love  (RB) exp #21  value +30            => +30  <-- ALERT
2. Drake Maye      (QB) exp #12  value +39 -20 depth  => +19
3. George Pickens  (WR) exp #33  value +18 -8 flex    => +10
```

Maye had the best raw value on the board, but with a quarterback already
rostered he drops behind a running back you actually need. He is still
*listed*, with the arithmetic shown, because a player who has fallen 39 picks
past expectation is worth knowing about — raise `depth_penalty` to bury backups
harder, or set both penalties to `0` to rank purely on value.

Alerts fire on the adjusted score, so a position you have already filled cannot
raise one on raw value alone.

## Where "expected pick" comes from

This is the one place the two providers genuinely differ, because their data
differs.

| | Sleeper | Yahoo |
| --- | --- | --- |
| Source | `search_rank`, normalized | `average_pick` (real ADP) |
| Accuracy | a proxy | actual draft data |

**Yahoo publishes real average draft position**, so `expected_pick` is simply
the player's ADP and the value score means exactly what it says.

**Sleeper publishes no ADP.** `search_rank` is the closest thing, but it is a
*search-popularity* rank, not a draft position: in the 2025 pool it had only
743 distinct values across 1930 fantasy-relevant players, with ties up to 161
players wide, and it ranked a kicker at 94. It cannot be compared to a pick
number directly, so the Sleeper analyzer sorts the pool and uses each player's
ordinal position, putting it on the same scale as an overall pick number. It is
a reasonable proxy and nothing more — treat Sleeper's numbers as softer than
Yahoo's.

Both analyzers exclude `K` and `DEF` by default. On Sleeper this is doubly
moot: Sleeper gives **no** team defence a search rank at all, so DEF could
never have been recommended.

Players with no NFL team are dropped, since they cannot score. Sleeper's
`status` and `active` flags are not reliable for this — Todd Gurley, retired
since 2021, is still listed as `Active` with a search rank of 27 — but having
no team is a signal that works. Set `REQUIRE_NFL_TEAM=false` to keep free
agents.

## Layout

| Path | Purpose |
| --- | --- |
| `flows/sleeper-draft-assistant.yml` | Sleeper flow |
| `flows/yahoo-draft-assistant.yml` | Yahoo flow |
| `analyzer_scripts/draft_analysis.py` | Provider-agnostic scoring, roster needs, snake math, Kestra plumbing |
| `analyzer_scripts/sleeper_fantasy_analyzer.py` | Sleeper API client |
| `analyzer_scripts/yahoo_fantasy_analyzer.py` | Yahoo API client |
| `requirements.txt` | Local development dependencies |

`draft_analysis.py` holds everything both providers share, so a change to the
scoring model applies to both. Each flow injects it alongside its own client as
a Namespace File.

## Deploying

Both flows run their analyzer as **Namespace Files**, so the scripts have to be
uploaded as well as the flows:

```bash
KESTRA=http://localhost:8080/api/v1/default
NS=test.robs-test
AUTH='admin@kestra.io:PASSWORD'

# Scripts: the shared module plus whichever clients you need
for f in draft_analysis.py sleeper_fantasy_analyzer.py yahoo_fantasy_analyzer.py; do
  curl -u "$AUTH" -X POST "$KESTRA/namespaces/$NS/files?path=/$f" \
    -F "fileContent=@analyzer_scripts/$f"
done

# Flows
for f in flows/sleeper-draft-assistant.yml flows/yahoo-draft-assistant.yml; do
  curl -u "$AUTH" -X POST "$KESTRA/flows" \
    -H "Content-Type: application/x-yaml" --data-binary "@$f"
done
```

Re-deploying an existing flow uses `PUT $KESTRA/flows/$NS/<flow-id>`.

### Requirements on the Kestra instance

- The **`io.kestra.plugin:plugin-script-python`** plugin. If it is missing,
  install it and restart:
  ```bash
  docker exec <container> /opt/java/openjdk/bin/java -jar /app/kestra \
    plugins install io.kestra.plugin:plugin-script-python:LATEST \
    -p /app/plugins -c /etc/config/application-license.yml
  ```
- A working **Docker task runner** (both flows run their analyzer in
  `python:3.13-slim`), which needs the Docker socket mounted into Kestra.
- For Yahoo only: a working **secret backend**, since the flow reads its
  credentials via `{{ secret(...) }}`.

## Sleeper

Set `league_or_draft_id` to whichever id you have — a **league id** from
`https://sleeper.com/leagues/<league_id>/...` or a **draft id** from
`https://sleeper.com/draft/nfl/<draft_id>`. Which kind you gave is detected
automatically. That id is **not** your user id.

`user_id` takes your **username** as well as your numeric id — Sleeper keys the
draft order by numeric id, so a username is looked up for you. Without that
lookup, "you are on the clock" would silently never fire.

Both id inputs also accept the **full URL** you copied them from.

| Input | Default | Purpose |
| --- | --- | --- |
| `league_or_draft_id` | – | The league **or** draft to watch; kind auto-detected |
| `user_id` | – | Your Sleeper username or user id |
| `alert_threshold` | `10.0` | Picks past expectation before flagging |
| `top_n` | `5` | Recommendations per poll |
| `exclude_positions` | `K,DEF` | Positions to leave out |
| `flex_penalty` | `8.0` | Discount for a flex-only fill |
| `depth_penalty` | `20.0` | Discount for an already-filled position |
| `sleeper_base_url` | Sleeper API | Override only for a test fixture |

### Mock drafts

A Sleeper mock belongs to no league, so it has only a draft id. Put that in
`league_or_draft_id`, from the mock's URL:

```
https://sleeper.com/draft/nfl/1398327930542669824
                              └───── draft id ─────┘
```

Nothing else changes — the id is probed as a draft first and a league second.
The logs say `Mock draft` and the analysis carries `draft_info.is_mock: true`.
Two things differ in a mock, both handled: there are no rosters, so
`on_the_clock.roster_id` is `null`; and a mock against bots may report no draft
order, so `is_my_pick` stays `null`.

**Yahoo has no mock-draft equivalent** — mocks are a separate pre-draft product
and are not exposed as league resources in the API.

### The player pool cache

Sleeper's player endpoint is ~14MB and they ask that it be called at most once
per day. Because every Kestra task run starts with a clean working directory,
the analyzer prunes the pool to the fields it needs (~630KB) and the flow
stores it back as the `sleeper_players_cache.json` Namespace File, re-injected
on later runs. The upload only happens when the pool was actually refreshed, so
a 30-second poll costs two small draft API calls.

## Yahoo

Yahoo's API is OAuth 2.0 only — there is no public read path — so this takes
more setup than Sleeper.

### One-time setup

1. Register an app at <https://developer.yahoo.com/apps/> with **Fantasy
   Sports → Read** permission. Yahoo calls the credentials Consumer Key and
   Consumer Secret.
2. Authorize the app once as yourself to obtain a **refresh token**. Yahoo's
   access tokens last one hour; the refresh token is long-lived and is what
   lets the flow run unattended.
3. Add three **Namespace Secrets**: `YAHOO_CLIENT_ID`, `YAHOO_CLIENT_SECRET`
   and `YAHOO_REFRESH_TOKEN`. Either through the Kestra UI
   (Namespaces → your namespace → Secrets), or as `SECRET_<NAME>` environment
   variables on the Kestra server holding the base64-encoded value:
   ```bash
   SECRET_YAHOO_CLIENT_ID=$(printf %s "$CLIENT_ID" | base64)
   ```
4. Set `league_key` to your league.

### The league key

A Yahoo league key looks like `449.l.123456`. The numeric prefix is Yahoo's
**game key**, which changes every season, so a key that worked last year will
not this year. A bare league id or the league URL also works and is completed
using the `game_key` input, which defaults to `nfl` — Yahoo resolves that to
the current season.

| Input | Default | Purpose |
| --- | --- | --- |
| `league_key` | – | The league to watch, e.g. `449.l.123456` |
| `game_key` | `nfl` | Game key used to complete a bare league id |
| `alert_threshold` | `10.0` | Picks past ADP before flagging |
| `top_n` | `5` | Recommendations per poll |
| `exclude_positions` | `K,DEF` | Positions to leave out |
| `flex_penalty` | `8.0` | Discount for a flex-only fill |
| `depth_penalty` | `20.0` | Discount for an already-filled position |
| `player_pages` | `4` | 25-player pages of available players to pull |
| `yahoo_base_url` | Yahoo API | Override only for a test fixture |
| `yahoo_token_url` | Yahoo OAuth | Override only for a test fixture |

Your own team is found via Yahoo's `is_owned_by_current_login` flag, so there
is no team id to configure.

### The access token cache

Yahoo access tokens last one hour, which is shorter than a draft. The flow
caches the token in Kestra's KV store with its expiry and refreshes only when
it is within two minutes of expiring — refreshing on every poll would mean
hitting Yahoo's token endpoint once a minute for the whole draft, which their
terms discourage.

Only the rotating access token is cached; the long-lived refresh token stays a
Secret. If Yahoo ever issues a *new* refresh token, the analyzer logs a loud
warning, because the stored secret is then stale and later polls will fail once
the old one is rejected.

### Polling cost

The Yahoo trigger polls every **minute**, not every 30 seconds like Sleeper.
Each poll costs several API calls — league settings, the draft board, your
team, one per player page, and one batch lookup for your roster's names —
where a Sleeper poll costs two. Yahoo throttles noisy clients. Lower
`player_pages` to make each poll cheaper.

### What Yahoo does not tell you

Yahoo does not publish the draft order. It is only revealed as round one is
picked, so early in round one the flow cannot say whose turn it is and reports
`is_my_pick: null` rather than guessing `false`. From your first round-one pick
onward, your slot is known and the clock works normally.

Yahoo's `draft_type` says *live*/*auction*/*offline* and never whether the
order reverses, so snake-versus-linear is inferred from the board itself by
comparing round two's team order to round one's.

## Running automatically

To poll during your draft, make sure the id input has the right default —
scheduled runs use the inputs' defaults — then enable that flow's
`poll_during_draft` trigger. Neither flow overlaps runs.

## Local development

```bash
uv pip install -r requirements.txt
cd analyzer_scripts

# Sleeper
SLEEPER_LEAGUE_OR_DRAFT_ID=your_league_or_draft_id \
SLEEPER_USER_ID=your_username \
  python sleeper_fantasy_analyzer.py

# Yahoo
YAHOO_LEAGUE_KEY=449.l.123456 \
YAHOO_CLIENT_ID=... YAHOO_CLIENT_SECRET=... YAHOO_REFRESH_TOKEN=... \
  python yahoo_fantasy_analyzer.py
```

Run from inside `analyzer_scripts/` so `draft_analysis.py` is importable.

Each script writes `draft_analysis.json`, prints a readable summary, and emits
the analysis in Kestra's `::{"outputs": ...}::` format. Every flow input has an
environment-variable equivalent — see each module's docstring.
