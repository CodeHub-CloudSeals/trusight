# Demo data

`corpus/` is where the reference projects live: one directory per project,
each holding the source drawings as `Input*.pdf` and, for evaluation only,
the client's own bar list as `Output*.pdf`.

    data/corpus/
      Project 1 - Kingston Pipe Foundations/
        Input 1 - ....pdf
        Output - R01_Bar List.pdf
      Project 5 - Atlanic Cages/
        ...

**`corpus/` is git-ignored.** Client drawings must not reach the public
repository, so nothing here is committed and nothing here is recoverable from
git. Keep the originals wherever the client sent them.

Two separate decisions, often confused:

* **Running locally against a corpus** is free of consequence — point
  `TRUSTSIGHT_CORPUS` at the folder and nothing leaves the machine.
* **Baking a corpus into the container image** puts client PDFs inside an
  artefact that gets pushed to a registry and pulled by a service. The
  registry is private, but an image is far easier to hand to someone than a
  folder is. Do it only when someone has decided it is acceptable for that
  client's drawings, and say so in writing.

With no corpus present the app serves the seeded Atlantic walkthrough alone,
and `/ready` says so rather than appearing complete.

## Ground truth stays out of inference

`Output*.pdf` is read by the evaluation harness and by the post-generation
comparison. It is never an input to extraction. The separation is what makes
"matches the reference exactly" a result rather than a restatement.

## `fallback/` — recorded runs

`scripts/freeze_run.py` drives a running server through a full walkthrough,
checks the result against the project's own reference bar list, and — only if
it reconciles — writes the finished run here:

    data/fallback/<project-slug>/
      manifest.json     what was captured, when, and whether it reconciled
      workspace.json    the exact payload every screen reads
      scene.json        the spatial view
      page-1.png …      rendered source pages
      exports/          the client pack, generated from that same run

The app then serves it at run id `recorded-<project-slug>`, read-only. Every
screen shows an amber *Recorded run* banner and clarifications are refused
with HTTP 409, because the one thing a fallback must never do is pass for a
live run.

**`fallback/` is git-ignored**, for the same reason `corpus/` is: it contains
rendered client drawings and the client's own quantities. Freeze one on the
host that will serve the demo.

`/ready` warns when no recorded run is present. That is a warning rather than
a blocker: the demo can run without one, but if the live path fails mid-
session there is nothing to switch to.
