# How language models actually use this server

Everything here is measured against a live Qlik Sense Enterprise 31.62 with
ten models — Claude Sonnet and Haiku, GLM-5.2 and GLM-5-Turbo, and six Codex
models (GPT-5.6-Sol, -Terra, -Luna, GPT-5.5, GPT-5.4-Mini,
GPT-5.3-Codex-Spark) — over roughly 400 runs in August 2026. Where a number
appears, it came from a run, not from an expectation.

## What a question costs

| Kind of question | Tool calls | Wall clock |
|---|---:|---:|
| One figure ("what is the average order value") | 2 | 25-40 s |
| One figure with a filter ("revenue for 2024") | 2-3 | 25-45 s |
| Unfamiliar app, open-ended ("work out what this is and count something") | 4-13 | 50-90 s |
| Chain of five dependent steps | 6-8 | 70-290 s |

Two calls is the normal shape of a simple answer: read the data model, then
compute. Nothing needs to precede that — 93% of runs opened with
`get_app_details`, and `get_about` plus `get_apps` together accounted for
2.5% of all calls.

## Where models get it right, and where they don't

On questions with a known answer, nine models out of ten land between 94%
and 100%. The failures cluster in one place: **questions with a period**.

Two real mistakes from the logs, both on "revenue for 2024":

- one model computed the whole period and reported 49 989 556 885 — the
  filter silently did not apply, and nothing in the reply said so;
- another computed the neighbouring year and stated it with confidence.

Neither could be caught by the server: the queries were valid. This is the
central risk of Qlik through a model — **a wrong answer looks exactly like a
right one**, because Qlik answers a bad filter with a number rather than an
error. The mitigations in this server (unknown-field refusal, all-zero
measure warnings, sample values in `get_app_details`) exist for that reason.

## What a deep investigation looks like

Given a chain where each step depends on the previous answer, models do walk
it rather than firing one query. A measured example — Claude Sonnet, eight
calls, 77 seconds:

1. region with the highest revenue → Ufa, 5 004 895 509.97
2. leading category inside Ufa → Beta, 628 834 192.49
3. that pair across 2024 and 2025 → fell, 317.1M to 311.7M (-1.71%)
4. split the fall into order count and average order value → orders -1.05%,
   average -0.67%
5. conclusion: both factors, neither dominant

That is the useful shape: the model narrows, then explains, then checks
whether the explanation holds.

## Sessions are the scarce resource, not the server

Qlik allows **five concurrent sessions per user**. Every start of this
server opens one, and before the `ttl` segment was added to the WebSocket
URL it lingered for the proxy's inactivity timeout — minutes. Measured
before the fix: five starts in a row passed, the sixth was refused, and
failures lined up with the count of starts rather than with how many agents
were running.

Practical consequences:

- one long-lived server process per person, not one per request. Any number
  of questions, apps and sockets ride on a single session;
- a separate Qlik identity per person or per automated consumer;
- clearing proxy sessions (`delete-user-sessions`) does not free the Engine
  side immediately — it takes minutes. Waiting beats hammering the endpoint.

## Measuring models honestly is harder than it looks

Three traps found while running the benchmarks, all of which produced
flattering-but-false results:

**The model reads the answers off the disk.** A model with shell access will
find previous results, logs, and even the benchmark's own expected-answer
file. One model reported a figure to the kopeck with zero tool calls, citing
the log file it read it from.

**It reads its own memory.** Codex stores every past conversation under
`CODEX_HOME`. Forty-seven files there contained the expected figure from
earlier runs. With an empty home and `--ephemeral`, the same model made four
calls and computed the answer properly — the earlier "refuses to use tools"
verdict was an artefact.

**The machine, not the model, fails.** Ten model clients at once exhausted
local resources: processes failed to start (Windows 0xC0000142) or hung for
ten minutes without reaching the server. Runs like that must be counted
apart, or they read as model failures.

A run that produces the right number with zero tool calls is not a success —
count it separately.

## Reply size

`get_app_details` is the largest reply in normal use and stays small: 3.4k
characters for an 11-field model, 6.5k for 33 fields. Past 60 fields it
switches to a `columns` + `rows` table — 16k for 300 fields instead of the
~32k the same content costs as objects.

Field metadata carries what cannot be inferred and drops what can: the
load-script comment (the only human description a column has) and Qlik's
tags as one string (`"numeric integer"`); no per-field row count, since it
belongs to the table.
