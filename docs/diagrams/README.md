# Two diagrams, and when to use them

Not a deck. The runbook opens with "No slides", and that is right: the product
demonstrates itself for fifteen minutes and a picture of it competes with the
thing itself. These two exist because they answer questions the product cannot
answer on screen.

Do not put either in the walkthrough. Have them open in another window for the
questions, and send them afterwards.

| File | Use it when someone asks |
|---|---|
| `trust-boundary` | "So where's the AI?" · "Does the AI calculate the steel?" · "What if the model is wrong?" |
| `scale-path` | "What happens with fifty estimators?" · "Is this production-ready?" · "What would it take to roll out?" |

Each comes as `-light` and `-dark`, `.svg` for slides that scale and `.png`
(2×) for tools that mangle SVG. Colours are the product's own palette, and
every text pair clears WCAG AA against the ground it sits on.

## `trust-boundary` — where the AI sits

The one correction worth making out loud. "AI-assisted" is heard as "the AI
works out the numbers", and once a room believes that, every governance claim
afterwards sounds like mitigation for an unreliable machine. The picture puts
the model in one box, with a fence: it proposes what a callout means, and it
never computes a quantity and never approves a release. The arithmetic is
deterministic, the missing facts come from an engineer, and all of it lands in
the evidence chain.

Say it in one line: **a model that proposes is useful; a model that computes
the quantity is the thing we're replacing.**

## `scale-path` — today versus production

This one is uncomfortable on purpose. Run state is held in the application
process, and that is the single reason the hosted service is pinned to one
instance — `MaxSize 1` in `deploy/deploy-apprunner.sh`, with the reason in a
comment. Not a capacity decision: a second instance would serve runs it holds
no state for.

Show it before anyone finds it. A limit you name yourself, with the fix drawn
next to it, reads as engineering judgement. The same limit surfaced by their
architect in week three reads as something you hoped they would not notice.

The other four rows are additive and independent — sign-in, intake and the
project store can each move on their own schedule. Only run state gates the
app tier, which is why it is the only row with an arrow on both sides.

## Regenerating

Both are generated from one script so light and dark cannot drift:

```bash
python docs/diagrams/make_diagrams.py
```

Edit the script, not the SVG. If a claim in a diagram stops being true of the
build — run state moves to a store, a model gets configured — change it there
the same day. A diagram that outlives its facts is worse than no diagram,
because someone will present it.
