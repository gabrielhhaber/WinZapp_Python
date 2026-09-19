# AGENTS.md

**The guidance for working in this repository lives in [CLAUDE.md](CLAUDE.md). Open and read
that file before doing anything else here.** None of it is Claude-specific: it holds the rules
that apply to every task (the two-process split, JID normalization, the sync gate, the five
locales, the test that must never open a window) and, at the end, an index of `docs/traps/` —
the measured history of every bug this project has already shipped, one file per area, to read
before touching that area. `.claude/rules/` carries the same pointers scoped by file path for
tools that support it. All of it applies to whatever assistant is reading it.

It is deliberately not duplicated into this file. This one started life as a verbatim copy, and
a copy only agrees with the original until the next edit — `CLAUDE.md` is amended as the
architecture moves, so the copy would quietly become a second, wrong description of the same
codebase. That is worse than having no `AGENTS.md` at all: the failure mode is an agent
confidently following instructions for a version of the project that no longer exists, in an
area (JID handling, the sync gate, the WPPConnect patch mechanism) where the details are the
whole point.

So: one document, one place to update it. If something here needs saying, say it in
`CLAUDE.md`.
