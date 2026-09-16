# Commit discipline

When creating commits, keep each one small, atomic, and independently reviewable.
A commit must represent one user-visible change, bug fix, refactor, or test-only
change. Include tests and required translations for that change in the same
commit.

Do not combine unrelated cleanup, formatting, dependency changes, or generated
files with functional work. Review the staged diff before committing and split
independent changes into separate commits. Never create a commit unless the user
explicitly requests it.
