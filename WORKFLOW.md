# How to build in small verified steps (and use an AI without losing the wheel)

## 1. The loop

Every increment, without exception:

```
name the seam  →  write the signature  →  predict the answer  →  write/generate the body  →  RUN IT  →  compare to the prediction
```

"Small" means: **one thing you can check in one run.** Not one function, not one file - one
*claim* you can confirm or refute. If you cannot say in advance what the output should
be, the piece is still too big or still too vague.

The step people skip is **predict the answer**. Running first and then looking at the
output and thinking "yes, that looks about right" is not testing - it is being reassured.
You will accept a wrong answer that looks plausible. Write the expected value down, out
loud or in a comment, *before* you run.

## 2. How to find the seams (splitting the work)

Two heuristics, and they cover almost everything:

**Split where the type changes.** `str` -> `TriageResult` -> `Action` -> `JiraIssue`. Each
arrow is a seam, each seam is a function, each function is separately checkable.

**Split I/O away from judgment.** This is the one that matters most:

- **Pure functions hold all the thinking.** No network, no files, no clock, no randomness.
  Same input always gives the same output, so you can test them instantly and forever.
- **I/O shells hold no thinking.** They call an API and hand back data. Thin enough that
  there is nothing to get wrong.

Why: judgment is what breaks, and judgment inside an I/O function can only be tested by
doing I/O - which is slow, costs money, needs the network, and fails for unrelated
reasons. Pull the judgment out and it becomes a function you can check in 50ms.

This project, seen through that lens:

| piece | kind | how you test it |
|---|---|---|
| `sanitize_ticket` | pure | call it, no key needed |
| size guards | pure | call it, no key needed |
| `force_human_review` | pure | call it, no key needed |
| `classify_ticket` | I/O shell | one real call, eyeball the object |
| `route` (step 4) | pure | table of inputs -> expected actions |
| `jira_client` (step 5) | I/O shell | mock mode, then one real call |
| retry/backoff (step 6) | pure logic + I/O | test the *policy* pure, the call mocked |

Notice how much is pure. That is not an accident - it is the design goal. **If most of your
logic needs the network to test, the design is wrong, not the tests.**

## 3. The verification ladder

Each rung is stronger than the last. Most people stop at rung 2 and believe they are done.

1. It imports.
2. It runs without raising.
3. It returns the right **type**.
4. It returns the **value you predicted** on a case you chose in advance.
5. It **fails correctly** on bad input - the right error, with a useful message.

**Rung 5 is the one that matters most**, because it is the rung everyone skips. It is also the rung that catches the bugs that never raise: a
`.replace()` whose result is thrown away, a config lookup that silently returns the
default, a truncation nobody logs. Those pass rungs 1-3 perfectly.

Habit: for every function, write down one input that should work and one that should
fail. If you cannot think of a failing input, you do not understand the function yet.

## 4. Testing without ceremony

You do not need a test framework to start, and building one before you have
anything to test is work that teaches you nothing.

**Tier 1 - scratch script (use this now).** One file outside the package, imports your
functions, calls them with a handful of inputs, prints results. Ten seconds to write, run
it after every change. Throwaway.

**Tier 2 - `pytest` (worth it for pure functions once they stabilise).** A table of
input -> expected output. Runs in milliseconds, no key, no network. Only worth writing for
things that will not change shape again.

**Never** test the LLM's *judgment* in an automated test - it is non-deterministic and the
test will flake. Test your *plumbing* automatically; check the model's quality by hand on
a fixed sample set.

## 5. Using an AI correctly

The rule underneath all of it: **you own every decision; it owns the typing.**

**Ask for the interface before the implementation.** The single most useful prompt is:

> "Don't write code yet. What are the seams here, what should the signatures be, and what
> are the failure modes I'm not thinking of?"

Design is where the value is. If you let it generate first, you inherit its design by
default and you will not even notice you made a choice.

**Give it constraints, not just the task.** "Write a ticket classifier" gets you a
free-form dict and a bare `except`. "Pure function, no I/O, must reject input over N
chars rather than truncate, must distinguish retryable from terminal failures" gets you
something you would defend.

**Have it attack your design before it builds it.** "Here's my schema - what would an
experienced reviewer object to?" is worth more than any amount of generated code.

**Cap what you accept unread.** More than about 20 lines you have not read line by line is
too much. Not a style rule - a comprehension limit. You cannot defend what you skimmed.

**Ask for the measurement, not the claim.** "Is 400 enough?" gets you an opinion. "Write
a script that prints actual token usage across five real tickets" gets you a number. An
AI's confident guess and yours fail the same way; only the measurement is different.

**Then run it yourself.** Generated code is *plausible* before it is *correct*. Plausible
is precisely what a language model optimises for.

## 6. The failure mode to watch for in yourself

Writing faster than you run. An AI makes this worse, not better: the unverified blocks get
bigger and look more convincing. Under time pressure the trap is generate 60 lines ->
they look right -> clock runs out -> someone asks about line 34.

Generate small. Run every time. If you cannot explain a line, delete it or read it until
you can.
