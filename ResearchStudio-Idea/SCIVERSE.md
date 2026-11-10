# Sciverse integration

This fork adds optional Sciverse retrieval to Paper Search and IdeaSpark, including
Phase 0 literature grounding, Phase 3.1 collision retrieval, and introduction/method
reading. Existing sources continue to work when Sciverse is not configured or fails.

## Configure

Set `SCIVERSE_API_TOKEN` in the repository's ignored `.env`, or export it in your
shell. Shell values take precedence. The adapter uses Python's standard library;
no Node runtime or separate Sciverse skill installation is needed.

```dotenv
SCIVERSE_API_TOKEN=
SCIVERSE_BASE_URL=https://api.sciverse.space
# SCIVERSE_DISABLED=1    # turn off retrieval and full-text fallback
# SCIVERSE_SEMANTIC=0    # metadata retrieval only (default also recalls chunks)
# SCIVERSE_MAX_PAGES=4   # metadata request cap per query, maximum 20
```

Use your normal HTTPS proxy if necessary, for example
`export https_proxy=http://127.0.0.1:7890`. The adapter only sends the token to
HTTPS `sciverse.space` hosts, refuses redirects, and redacts service error bodies.
Credentials and generated agent installations are ignored by Git.

## Search and read

```bash
conda activate researchstudio
python ResearchStudio-Idea/skills/paper_search/scripts/search_papers.py \
  --query 'retrieval augmented generation' --start-year 2024 --end-year 2026 \
  --sources sciverse --max-papers 10 --json /tmp/sciverse-papers.json

python ResearchStudio-Idea/skills/idea_spark/scripts/run.py check_connectors
```

Paper Search includes Sciverse in its default sources only when a token is set
and `SCIVERSE_DISABLED` is not `1`. `--sources sciverse` selects it explicitly.
The JSON file retains unranked records grouped by source, including abstracts and
Sciverse pointers. Text output continues to show ranked papers.

IdeaSpark automatically enables two additional Phase 0 jobs (0–6 months and
6–24 months) and the same signature/alias collision retrieval used by other
sources. Set `IDEASPARK_POOL=sciverse_recent=0,sciverse=0` to disable the Phase 0
jobs only, or `SCIVERSE_DISABLED=1` to disable the integration everywhere.
The published-only job requires an explicit journal/conference venue type;
unknown types are not claimed as peer-reviewed. Search results with unknown exact
dates retain their year precision; month-window membership remains uncertain.

Sciverse metadata search (`/meta-search`) and semantic chunk search
(`/agentic-search`) contribute papers in interleaved order. Multiple chunks for
the same normalized title/year collapse into one paper. Cross-source dedup keeps
existing source priority while preserving `sciverse_records`, so an OpenAlex or
Semantic Scholar record can still be read through Sciverse.

Each Sciverse pointer keeps `unique_id` (metadata identity), `doc_id` (full-text
artifact), and retrieved snippets with their byte offsets. Only a `doc_id` may be
passed to `/content`. Do not substitute a metadata ID or treat snippets as a
full-paper read.

When a document pointer is known, IdeaSpark tries Sciverse first and falls back
to the existing HTML/PDF paths if necessary. For other papers, Sciverse is a final
fallback after those paths fail; resolution requires an exact DOI or normalized
title. Reads follow the provider's `next_offset`, not Python character counts.
They are bounded by the per-paper time budget and a 16-page (16 KiB/page) cap.

The full-text cache retains `sciverse_doc_id`, `sciverse_unique_id`, read ranges,
`sciverse_next_offset` and `sciverse_content_complete`. Page-capped reads carry a
warning. A read error cannot promote retrieved snippets or abstracts into method
evidence. Existing phase/audit rules still decide whether a paper is sufficiently
grounded; adding this source does not replace the research audit.

## Tests and maintenance

```bash
python -m unittest discover -s ResearchStudio-Idea/tests -p 'test_sciverse.py' -v
python ResearchStudio-Idea/skills/paper_search/scripts/selftest_postprocess.py
python ResearchStudio-Idea/skills/paper_search/scripts/selftest_runtime.py
```

CI runs these offline checks without API credentials. The runtime tests start a
local HTTP server. Live provider availability, corpus completeness and data
accuracy require separate checks; neither citations nor full text are guaranteed
for every record.

The small `sciverse_client.py` module is mirrored in the two independently
installable skills. A regression test checks that the copies are identical. Edit
both copies together; this avoids depending on paths outside an installed skill.
Current service requests use `filters` and `fields` as verified against
`/meta-catalog`; some external skill examples using `year_from` or
`filters_advanced` are not accepted by this service version.

## Merge official updates

`origin` points to your fork; `upstream` points to Microsoft's repository. After
committing local work, merge the official branch into the branch you use:

```bash
git fetch upstream
# Run on main after the integration has been merged there;
# otherwise run on feature/sciverse.
git merge upstream/main
# Resolve any conflicts, then run the tests above.
git push origin HEAD
```

Keep the Sciverse implementation in separate files and preserve the small
registration/dedup/full-text hooks when resolving upstream conflicts. Avoid
force-pushing or rebasing shared published history for routine upstream updates.
