# Writing the changelogs

> The unit is stable-to-stable; verify every claim; write the entry when the change lands.
>
> Moved verbatim out of `CLAUDE.md` so it is read when the area is touched, not on every session. Keep it here: this is measured history, not a summary.

## Writing the changelogs (`client/changelog_{pt-BR,pt-PT,en-US,es-ES,pl}.txt`)

**The unit is stable-to-stable, and that is the whole difficulty.** A changelog entry describes what a user *receives*, and a user upgrading from the previous stable release receives the net effect of everything between the two tags — never the path taken to get there. So the diff to read is `git log <previous stable tag>..main`, and the alpha line in between is working material, not content.

Three rules follow, and all three are about the same mistake:

- **Ignore the back-and-forth, and ask when the FEATURE landed, not just the bug.** A fix that was shipped, reverted and reshipped is one entry or none. A bug that only ever existed in an alpha never reaches a stable user, so "fixed X" is *wrong* when X was introduced after the last stable tag.

  The sharper form of that, and the one that is easy to miss: **a fix to a feature that itself arrives in this same release is not news either.** The feature is the entry; its internal history is not. Caught in review while writing 1.1.1.0 — "restoring the profile no longer deletes your messages" was written as a fix, but `client/core/profile_recovery.py` does not exist at `v1.1.0.0`, so a user upgrading stable-to-stable never had a profile restore at all, let alone one that deleted messages. It described a bug that only alpha testers could ever have seen. The test is one command: `git show <previous stable tag>:<file>` — if the file or the mechanism is not there, anything about its behaviour belongs in the feature's own entry or nowhere. The same pass removed a second item for the same reason, and kept only the part of it (the media sweep) whose code did predate the tag.

  Note what this does *not* exclude: a NEW mechanism that fixes an OLD user-visible problem is a legitimate entry, because the problem is what the user had. The staleness re-check and the reduced phone sync notifications are both new code in 1.1.1.0 and both stayed, since the symptoms they end ("only F5 fixes it", a stream of sync notifications) were there in the release before.
- **Condense whole runs of alphas into one item.** Profile recovery took ten PRs across two weeks; the user gets one feature. Read the intent, not the commits: several PRs with the same purpose are one sentence, and that sentence describes the end state, not the sequence.
- **Verify a claim before making it.** Comparing the raw `+` lines of a locale diff will show keys that only moved, and a key that already existed reads as a new feature. Diff the *key sets* (added / changed / removed), and check `git show <tag>:<file>` when unsure. Measured while writing 1.1.1.0: `search_contact_label`, `downloading_progress` and `uploading_progress` all appeared as additions in the line diff and all three shipped in the release before — the transfer progress belonged under fixes (its gauge was wrong), never under new features.

**What to read, in order of usefulness**: the merged PR titles (`git log <tag>..main --merges`), which state intent; the added/changed keys in `client/languages/pt-BR.json`, which are the closest thing to "what the user can now see or hear"; and `client/data/settings_default.json`, for anything that became a new option. **Include other contributors' work** — `git shortlog -sn <tag>..main` names them, and their PRs carry user-visible features as often as anyone's.

**Write the entry when the change lands, not at release time.** Everything above is recovery work — reconstructing weeks of intent from commit titles, and the reconstruction is where the errors come from: an item written days later cannot remember whether the thing it fixes existed before, which is exactly how both of the removals above got written in the first place. A change that adds or fixes something a user can see should carry its own changelog line in the same commit, while the author still knows what it replaced.

That does not change what a changelog *is*, and the rule survives intact at the other end: **entries are for stable releases only, and the unit stays stable-to-stable.** So a line added during the alpha cycle is a draft, not a commitment — at release time it still has to answer the two questions above (did the user have this before? is this the same item as three others?) and it can still be merged away or deleted. Writing as you go removes the archaeology, not the editing pass.

Keep the existing shape: version header, one short paragraph saying what the release is *about*, then NOVIDADES / MELHORIAS / CORREÇÕES. Newest version on top, older ones untouched below. Write for the person using the app, not for the person who fixed it — name the symptom they saw, not the function that caused it. **All five files, always**, same items in the same order; the locales are equal, exactly as they are for the UI strings.
