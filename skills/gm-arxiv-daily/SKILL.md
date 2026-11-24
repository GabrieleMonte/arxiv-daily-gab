---
name: gm-arxiv-daily
description: Read the day's arXiv mailing in astro-ph.CO and hep-ph (new submissions and cross-lists) and pick the papers Gabriele Montefalcone should read — a top list of at most 5, never padded, plus up to 5 "also interesting" (more only when a paper ties directly to their own research) and at most one wildcard, each with a one-line reason. Ranking follows Gabriele's interest profile, built from ~935 papers they saved since Dec 2023, their INSPIRE papers, and interviews. Use whenever Gabriele asks for today's arXiv, the daily papers, the morning digest, "what's on arXiv", "anything good today", to rank or triage an arXiv listing (live or a saved listing JSON), or when a scheduled routine runs the daily arXiv email. Also use to backtest the ranking on a past mailing.
---

# Gabriele's daily arXiv ranking

Read **every** paper in the mailing, judge it against Gabriele's profile, and return a short,
honest list. Gabriele saves ~1.2 papers per day out of ~47; when they ranked days by hand they
chose 2-5 top papers and never padded. A short list on a quiet day is the correct answer.

## Inputs

- A listing JSON from `scripts/fetch_listing.py` (or one handed to you, e.g. for a backtest).
  Fetch today's with:

  ```sh
  python3 ${CLAUDE_SKILL_DIR}/scripts/fetch_listing.py <workdir>/listing.json
  ```

  It holds the latest mailing (the RSS feed's), keeps only `new` and `cross` announcements, and
  numbers papers `n = 1..N`.
- `references/interest_profile.md` — tiers, skip list, author signal, selection rules. **Read it
  in full every time.**
- `references/recent_boost.md` — temporary topic boosts.
- `references/calibration_examples.md` — four days Gabriele ranked by hand, showing where the bar
  sits and what an earlier ranking got wrong. Read it before ranking.

## Procedure

1. **Read the whole listing.** Titles, authors, categories, comments and abstracts of all N
   papers. Do not skim titles only: many top picks (e.g. a hep-ph DM theory paper, a dark-siren
   method paper) are only recognisable from the abstract.
2. **Triage every paper** into `1a`, `1b`, `release`, `2`, `3`, `wildcard?` or `skip`, using the
   profile. A paper is `1a` only if it builds on, tests or competes with one of Gabriele's own
   research lines — name which one in the reason.
3. **Pick the top list** (at most 5) from `1a`, `1b`, `release`, plus at most one exceptional
   Tier-2 DM-theory or ultralight-boson paper. Each must have a substantive new result; ask
   "would Gabriele open this PDF today?". Releases always go in. Aim for ~3 cosmology : 2 particle
   phenomenology when candidates are comparably strong. Never put non-warm inflation
   model-building in the top list.
4. **Pick the also list** (up to 5, the wildcard included) from what remains in Tier 1 and
   Tier 2, best first. Go beyond 5 only for papers tied directly to a `1a` topic. At most one of
   the 5 may be a wildcard: a paper outside the profile that is unusual or broadly striking; set
   `"wildcard": true` and put it last. Incremental Tier-2/3 papers do not need to fill the list.
5. **Write reasons** of one sentence each, specific to the paper: what it does and why it matters
   *to Gabriele* (which tier or own-research line it touches). No generic praise.
6. **Write the ranking JSON** and render the email:

   ```json
   {"mailing": "<listing['mailing']>",
    "note": "<optional one-line comment on the day, e.g. 'Quiet day: two picks.'>",
    "top":  [{"n": 12, "tier": "1a", "reason": "..."}],
    "also": [{"n": 30, "tier": "2", "reason": "...", "wildcard": false}]}
   ```

   ```sh
   python3 ${CLAUDE_SKILL_DIR}/scripts/render_email.py <workdir>/ranking.json <workdir>/listing.json
   ```

   The renderer takes titles and authors from the listing by `n`, so never copy titles into the
   JSON; get `n` right. It writes `ranking.html` and `ranking.txt` and prints the subject line.
7. **Report in chat** the top and also lists as `n. Title — reason` lines (unless running
   unattended, in which case the caller decides what to do with the files).

## Pitfalls seen in calibration

- Padding the top list to 5 with the day's least-bad papers (Gabriele rejected every padded pick).
- Over-ranking generic inflation model-building (Starobinsky UV completions, cosmological
  collider, Lyth-bound variants) because inflation is in the profile — only *warm* inflation is
  Tier 1.
- Missing papers whose relevance is in the abstract rather than the title: GW cosmology (dark
  sirens), black-hole superradiance, DM theory in hep-ph (Sommerfeld, portals).
- Treating "collider" as an automatic skip when the paper is about DM.
- Dropping "incremental" dark-energy parametrization / reconstruction papers fitted to DESI data.
  They are Gabriele's most-saved stream (competitor tracking): keep the best one or two in the
  also list.

## PDF summaries

The morning routine attaches a PDF of full-text technical summaries of the TOP papers, written by
the arxiv_skimmer shipped next to this skill in the mirror. `references/pdf_summaries.md` is the
procedure. It is a bonus: run it only when a caller asks for it, and never let it block the email.

## Reply-to-save

When Gabriele replies to a digest with papers they liked, `references/reply_to_save.md` is the
procedure that adds them to `arxiv_reads` on their website (via `scripts/save_reads.py`). It is
separate from ranking and runs on Gabriele's Mac, not in the cloud routine.

## Monthly feedback

Once a month, `references/feedback.md` compares the digests with what Gabriele saved and emails
numbered proposals for `recent_boost.md` and `interest_profile.md`; Gabriele approves them by
reply. Like reply-to-save it runs on the Mac. Never edit the profile from a ranking run.

## Scope

- Categories: astro-ph.CO and hep-ph only, including cross-lists into them. Replacements are
  ignored.
- If the listing's mailing date equals the last one already sent (holidays: arXiv posts no new
  mailing), say so and do not rank it again.
