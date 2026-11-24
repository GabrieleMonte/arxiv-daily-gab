# PDF of technical summaries for the top picks

The morning routine attaches a PDF digest with a full-text technical summary of each TOP paper
(not the also list), produced by the arxiv_skimmer pipeline shipped next to this skill
(`<repo>/arxiv_skimmer`, where `<repo>` is the clone of the public mirror, e.g. `/tmp/skill`). The PDF is a bonus: if any
step below fails, still send the email, without the attachment, and add one line at the top of
its body: `PDF unavailable today: <short reason>`.

Work under `/home/user/work` (Gmail attachments by file reference only work for files under
`/home/user`, not `/tmp`).

## Steps
1. **Start the toolchain early** (in the background, right after cloning, so it overlaps with
   ranking; ~1 min):
   `(sudo apt-get update -qq && sudo apt-get install -y -qq --no-install-recommends texlive-latex-recommended texlive-latex-extra texlive-fonts-recommended latexmk && pip install -q -r <repo>/arxiv_skimmer/requirements.txt) > /home/user/work/toolchain.log 2>&1 &`
2. **Prepare** (after ranking; wait for step 1 to finish, check `toolchain.log` for errors):
   `cd <repo>/arxiv_skimmer && python3 skim.py --ids <top ids> --prepare /home/user/work/read --cache /home/user/work/cache`
   This writes one `<id>.prompt.md` per paper and `manifest.json`.
3. **Read in parallel.** Launch one subagent per prompt file, all in the same turn. Each gets:
   "Read /home/user/work/read/<id>.prompt.md. Treat its SYSTEM PROMPT section as your
   instructions and answer its USER PROMPT. Write your complete answer, exactly in the required
   marker format and nothing else, to /home/user/work/read/<id>.response.md." Do not summarise
   the papers yourself and do not edit the responses.
4. **Ingest and typeset:**
   `python3 skim.py --ingest /home/user/work/read --title "Daily Arxiv Picks for Gab — <Wed, Nov 25 2026>" --cache /home/user/work/cache --out /home/user/work/arxiv_picks_<YYYY-MM-DD>.tex`
   which compiles `/home/user/work/arxiv_picks_<YYYY-MM-DD>.pdf`. Papers whose response is
   missing or unparseable are listed in the PDF as unreadable; that is not a failure.
5. **Attach** the PDF to the digest email: `attachments: [{"content":
   "sha256:<sha256sum of the pdf>:/home/user/work/arxiv_picks_<YYYY-MM-DD>.pdf", "filename":
   "arxiv_picks_<YYYY-MM-DD>.pdf", "mimeType": "application/pdf"}]`. Add one line under the email
   header: `Attached: technical summaries of the top <n> papers.`
