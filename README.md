# Kestra Fantasy Football Draft Analysis

Kestra flows that watch a live fantasy football draft and tell you who is worth
taking, for **Sleeper**, **Yahoo Fantasy** and **CBS Sports Fantasy**.

Every poll pulls the draft board, scores the players still available, weights
them against the roster you have already drafted, and logs an alert when
someone good falls further than they should have.

| Flow | Provider | Auth |
| --- | --- | --- |
| `sleeper-draft-assistant` | Sleeper | none — the API is public |
| `yahoo-draft-assistant` | Yahoo Fantasy | OAuth 2.0 (client id, secret, refresh token) |
| `cbs-draft-assistant` | CBS Sports Fantasy | league access token (client id, secret, CBS account) |

All three flows behave identically: same scoring, same roster logic, same log
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

This is the one place the three providers genuinely differ, because their data
differs.

| | Sleeper | Yahoo | CBS |
| --- | --- | --- | --- |
| Source | `search_rank`, normalized | `average_pick` (real ADP) | `players/rankings?type=overall` |
| Accuracy | a proxy | actual draft data | an editorial ranking |
| Pool depth | the whole player universe | top 100 by Yahoo's rank | top 200, fixed |

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

**CBS publishes an overall ranking** — 200 players, `rank` 1..200, sourced from
its own editorial average (`cbs_avg_ppr` by default, `cbs_avg` for non-PPR).
Unlike Sleeper's it needs no normalizing: it is already dense and already an
actual draft ranking, so it goes straight in as `expected_pick`. It is still a
ranking rather than draft data, so it sits between the other two in quality.

CBS *does* have an `players/average-draft-position` resource, which would be
the better source. It has answered every request with `HTTP 500` — for both
football and baseball, authenticated or not, with and without its documented
`position` and `limit` parameters. If it ever comes back, swap
`fetch_ranked_players` for it and the CBS numbers become as good as Yahoo's.

All three analyzers exclude `K` and `DEF` by default. On Sleeper this is doubly
moot: Sleeper gives **no** team defence a search rank at all, so DEF could
never have been recommended. On CBS it is moot too — the overall ranking
carries no kickers or defenses at all.

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
| `flows/cbs-draft-assistant.yml` | CBS flow |
| `analyzer_scripts/draft_analysis.py` | Provider-agnostic scoring, roster needs, snake math, Kestra plumbing |
| `analyzer_scripts/sleeper_fantasy_analyzer.py` | Sleeper API client |
| `analyzer_scripts/yahoo_fantasy_analyzer.py` | Yahoo API client |
| `analyzer_scripts/cbs_fantasy_analyzer.py` | CBS API client |
| `scripts/yahoo_refresh_token.sh` | One-time Yahoo authorization, loads credentials into the KV store |
| `scripts/cbs_access_token.sh` | One-time CBS token mint, loads credentials into the KV store |
| `requirements.txt` | Local development dependencies |

`draft_analysis.py` holds everything the providers share, so a change to the
scoring model applies to all of them. Each flow injects it alongside its own
client as a Namespace File.

## Deploying

Each flow lives in its own namespace, and each namespace holds its own copy of
the analyzer scripts:

| Namespace | Flow | Namespace Files |
|---|---|---|
| `sleeper` | `sleeper-draft-assistant` | `draft_analysis.py`, `sleeper_fantasy_analyzer.py` |
| `yahoo-sports` | `yahoo-draft-assistant` | `draft_analysis.py`, `yahoo_fantasy_analyzer.py` |
| `cbs-sports` | `cbs-draft-assistant` | `draft_analysis.py`, `cbs_fantasy_analyzer.py` |

Because `draft_analysis.py` is copied into every namespace, a change to the
shared scoring model has to be uploaded three times — the script block below
does that.

`nsfiles upload` refuses to write to a namespace that does not exist yet, and a
namespace is created by the first flow deployed into it. On a fresh instance,
run `kestractl flows deploy` **before** the uploads.

Deployment uses [`kestractl`](https://github.com/kestra-io/kestractl), Kestra's
standalone CLI (a separate release from the server binary; the server's own
`kestra` CLI no longer pushes flows to a remote instance). Configure a context
once:

```bash
kestractl config add local http://localhost:8080 default \
  --username 'admin@kestra.io' --password 'PASSWORD' --default
```

Then deploy flows and scripts:

```bash
# Flows first: each file's own `namespace:` field decides where it lands, and
# deploying is what creates the namespace the uploads below need.
kestractl flows validate ./flows/
kestractl flows deploy ./flows/ --override

# Namespace Files: the shared module plus each provider's client
for ns in sleeper yahoo-sports cbs-sports; do
  kestractl nsfiles upload "$ns" analyzer_scripts/draft_analysis.py \
    draft_analysis.py --override
done
kestractl nsfiles upload sleeper analyzer_scripts/sleeper_fantasy_analyzer.py \
  sleeper_fantasy_analyzer.py --override
kestractl nsfiles upload yahoo-sports analyzer_scripts/yahoo_fantasy_analyzer.py \
  yahoo_fantasy_analyzer.py --override
kestractl nsfiles upload cbs-sports analyzer_scripts/cbs_fantasy_analyzer.py \
  cbs_fantasy_analyzer.py --override
```

`--override` is what makes both commands idempotent; without it they refuse to
replace anything that already exists.

### Requirements on the Kestra instance

- The **`io.kestra.plugin:plugin-script-python`** plugin. If it is missing,
  install it and restart:
  ```bash
  docker exec <container> /opt/java/openjdk/bin/java -jar /app/kestra \
    plugins install io.kestra.plugin:plugin-script-python:LATEST \
    -p /app/plugins -c /etc/config/application-license.yml
  ```
- A working **Docker task runner** (all three flows run their analyzer in
  `python:3.13-slim`), which needs the Docker socket mounted into Kestra.
- For Yahoo and CBS: nothing further — both read their credentials from the
  namespace **KV store** rather than Namespace Secrets, because
  `kestra.secret.type` is unset on the 2.0 RC this runs against and the secrets
  controller cannot start without it. Set `kestra.secret.type: jdbc` in the
  server config to switch them back to `secret()`, which is log-masked where KV
  is not.

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

> **Status: blocked on Yahoo API approval.** Yahoo now gates Fantasy Sports API
> access behind a review process (<https://sports.yahoo.com/developer/access/>),
> and until an application is approved the **Fantasy Sports** permission does
> not appear in the app's API Permissions at all — the console offers only the
> OpenID Connect scopes. A token minted without that entitlement is valid but
> every `fantasysports.yahooapis.com` call returns
> `oauth_problem="additional_authorization_required"`. Nothing in this flow can
> be tested until approval lands. Use the Sleeper flow in the meantime.
>
> Application submitted 2026-08-31; no approval timeline is published. On
> approval, the credentials already in the KV store stay valid, but
> `scripts/yahoo_refresh_token.sh` must be re-run: permissions are not
> retroactive, so an existing grant keeps its original (scope-less) consent.

### One-time setup

1. Apply for Fantasy Sports API access at
   <https://sports.yahoo.com/developer/access/> — say that use is limited to a
   single personal league and pick the "Small (<1,000 users)" band. Once
   approved, register an app at <https://developer.yahoo.com/apps/> (or attach
   the approval to an existing one) and enable **Fantasy Sports → Read**.
   Yahoo calls the credentials Consumer Key and Consumer Secret. Read access is
   all this flow needs; Yahoo does not currently grant write access.
2. Authorize the app once as yourself to obtain a **refresh token**. Yahoo's
   access tokens last one hour; the refresh token is long-lived and is what
   lets the flow run unattended. Run `scripts/yahoo_refresh_token.sh`, which
   walks through the authorization and loads all three credentials into the KV
   store for you. Yahoo requires PKCE, so the `code_verifier` has to survive
   between the authorize step and the token exchange — hence a script rather
   than two `curl` calls.
3. Add three **KV store entries** on the `yahoo-sports` namespace:
   ```bash
   kestractl kv set yahoo-sports STRING YAHOO_CLIENT_ID     "$CLIENT_ID"
   kestractl kv set yahoo-sports STRING YAHOO_CLIENT_SECRET "$CLIENT_SECRET"
   kestractl kv set yahoo-sports STRING YAHOO_REFRESH_TOKEN "$REFRESH_TOKEN"
   ```
   **Namespace Secrets are the better home for these** — `secret()` values are
   masked in execution logs and KV values are not. Both the Yahoo and CBS flows
   read them with `kv()` only because `kestra.secret.type` is unset on the 2.0
   RC used here: with no primary `SecretInterface` bean, the secrets
   controllers register against the two ungated ones in the image and then fail
   injection with "Multiple possible bean candidates". `GET
   /api/v1/<tenant>/secrets` gives the honest diagnosis. Setting
   `kestra.secret.type: jdbc` in the server config and restarting fixes it;
   after that, switch the `kv()` calls in `flows/yahoo-draft-assistant.yml` and
   `flows/cbs-draft-assistant.yml` back to `secret()` and store them as secrets
   instead — either through the UI (Namespaces → the namespace → Secrets) or as
   `SECRET_<NAME>` env vars holding the base64-encoded value:
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

## CBS Sports

CBS's Fantasy API is the **v3.0 Fantasy Platform API**. Its developer portal
(`developer.cbssports.com`) was retired years ago and the API is formally
deprecated, but `api.cbssports.com` still serves every resource this flow
needs. Everything below was checked against the live API on 2026-09-02; the
response shapes come from the archived portal documentation.

Reading a private league needs a **league access token**, so this takes more
setup than Sleeper and less than Yahoo — there is no app to register and no
approval to wait for.

> **What has and has not been verified.** The public half is confirmed live:
> the two token endpoints, `players/rankings?type=overall` (200 players, dense
> rank, `cbs_avg_ppr`/`cbs_avg`), `players/list`, `positions`, and the exact
> error strings the analyzer branches on (`Failed Authentication: error -
> invalid access token`, `Missing league_id`). The **league-scoped** resources
> — `league/details`, `rules`, `draft/config`, `draft/order`, `draft/results`,
> `teams` — were confirmed to exist and to be gated on the token, but their
> response bodies could not be read without membership in a CBS league, so the
> parsing is written against the archived portal's documented shapes. The whole
> flow was run end to end against a fixture built from those shapes: pre-draft,
> mid-draft with and without alerts, and complete, plus the token mint-and-cache
> path. Point `cbs_base_url` at a fixture to do the same. If a field turns out
> to differ on a real league, the parser reads by key name throughout, so the
> fix is local.

### One-time setup

1. **Find your league id.** A CBS league *is* a subdomain. Open your league and
   look at the address bar:

   ```
   https://myleague.football.cbssports.com/
           └─ league id ─┘
   ```

   That is what goes in `league_id`. The full URL can be pasted in place of the
   bare id, and it is **not** your CBS user id or your team name.

2. **Mint an access token.** Run `scripts/cbs_access_token.sh`. It walks the
   two-hop token flow, checks the token actually reaches your league, and loads
   everything into the `cbs-sports` KV store:

   ```bash
   scripts/cbs_access_token.sh
   ```

   It asks for four things:

   | Prompt | What to enter |
   | --- | --- |
   | Client id | Any stable string, e.g. `kestra-draft-assistant` |
   | Client secret | Any stable string — pick one and keep it |
   | CBS user id | The **email address** you sign in to CBS Sports with |
   | League id | The subdomain from step 1 (blank to skip the check) |

   The client id and secret name your application rather than authenticating
   it: the portal that used to issue v3.0 API credentials is gone, and CBS's
   token endpoints accept any non-empty pair. Pick a pair and keep using it, so
   a re-run mints a token for the same identity.

   The script's league check is the part that matters. CBS hands out tokens
   freely and only refuses at the resource, so a token that looks fine can
   still fail on `league/details` — better to find that out now than mid-draft.
   On success it prints your league name, team count and draft state.

3. **Set `league_id`** on the flow to the same value, so the scheduled trigger
   (which uses the inputs' defaults) watches the right league.

The flow can also mint its own token: if `cbs_access_token` is missing from the
KV store but `CBS_CLIENT_ID`, `CBS_CLIENT_SECRET` and `CBS_USER_ID` are there,
`analyze_draft` mints one and `cache_access_token` writes it back. Running the
script is still the better first step, because it verifies league access and
the flow cannot.

### Inputs

| Input | Default | Purpose |
| --- | --- | --- |
| `league_id` | – | Your league's CBS subdomain |
| `alert_threshold` | `10.0` | Picks past their rank before flagging |
| `top_n` | `5` | Recommendations per poll |
| `exclude_positions` | `K,DEF` | Positions to leave out |
| `flex_penalty` | `8.0` | Discount for a flex-only fill |
| `depth_penalty` | `20.0` | Discount for an already-filled position |
| `rankings_source` | `cbs_avg_ppr` | `cbs_avg_ppr` (PPR) or `cbs_avg` (non-PPR) |
| `cbs_base_url` | CBS API | Override only for a test fixture |
| `cbs_token_url` | CBS OAuth | Override only for a test fixture |

Your own team is found via CBS's `logged_in_team` flag, so there is no team id
to configure. If no team in the league is owned by the account the token was
minted for, the analysis says so and leaves `is_my_pick` null rather than
guessing.

`rankings_source` is not validated by CBS: it serves `cbs_avg_ppr` for anything
it does not recognize. The analyzer logs that substitution and reports what was
actually used as the run's `rankings_source`, so a typo shows up in the logs
rather than silently scoring against the wrong ranking.

### The access token cache

CBS publishes no expiry for these tokens, so unlike Yahoo there is nothing to
refresh on a timer. The token is cached in the KV store with a 30-day TTL and
reused on every poll. If CBS ever rejects it, the analyzer fails with a message
naming `scripts/cbs_access_token.sh` — and the next poll mints a fresh one from
the stored credentials anyway.

### What CBS tells you that the others do not

CBS is the only one of the three that **publishes the draft order before the
draft starts** (`league/draft/order`), which has two consequences:

- Your draft slot is known in the pre-draft branch, not just once picking
  begins. Yahoo cannot do this at all; it reveals the order only as round one
  is picked.
- A **custom or keeper draft order** comes out right. The flow looks up the
  current pick on the published board first and only falls back to snake
  arithmetic when that pick is not listed — CBS documents the order as round
  one only for a snake or manual order, and every round for a custom one.

CBS also states outright whether the order reverses (`order_type` is `snake` or
`nonsnaking`), where Yahoo has to have it inferred from the board.

Roster needs come from `league/rules`: `max_active` per position is that
position's starting slots, flex slots appear in the same list under their own
abbreviations (`RB-WR`, `WR-TE`, `RB-WR-TE`, `FLEX`), and the bench is a roster
*status* ("Reserve Players") rather than a slot. CBS's own position codes are
normalized to the shared set — `DST`/`D`/`ST` all become `DEF`, `TQB` becomes
`QB`, `TK` becomes `K` — and individual defensive slots (`DL`, `LB`, `DB` and
their flex) are ignored, since this tool does not recommend for them.

### Polling cost

The CBS trigger polls every **minute**, like Yahoo. Each poll costs seven API
calls — league details, rules, draft config, draft order, the draft board, the
teams list, and the ranking — where a Sleeper poll costs two. CBS's v3.0 API is
deprecated and publishes no rate limit, which is a reason to be conservative
rather than a licence not to be.

### Auction drafts

CBS supports auctions, and the analyzer maps their `bidding` and `nominating`
draft states onto `drafting` so the live branch still fires. The **value
scoring is built for a snake draft**, though: it compares a player's expected
pick against the current overall pick, which in an auction is a much weaker
signal than the money left on each roster. The recommendations are still
roster-need-aware and still tell you who the best remaining players are; treat
the numbers as ordering rather than arithmetic.

## Running automatically

To poll during your draft, make sure the id input has the right default —
scheduled runs use the inputs' defaults — then enable that flow's
`poll_during_draft` trigger. No flow overlaps its own runs.

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

# CBS
CBS_LEAGUE_ID=myleague \
CBS_CLIENT_ID=... CBS_CLIENT_SECRET=... CBS_USER_ID=you@example.com \
  python cbs_fantasy_analyzer.py
```

Run from inside `analyzer_scripts/` so `draft_analysis.py` is importable.

Each script writes `draft_analysis.json`, prints a readable summary, and emits
the analysis in Kestra's `::{"outputs": ...}::` format. Every flow input has an
environment-variable equivalent — see each module's docstring.
