## Summary

<!-- What does this PR change, and why? -->

## Type of change

- [ ] Bug fix (`fix:`)
- [ ] New feature (`feat:`)
- [ ] Documentation (`docs:`)
- [ ] Refactor / chore / CI

## Safety checklist (this tool touches a shared remote — be careful)

- [ ] No code path can delete remote data during `push`/`pull`
- [ ] All remote paths go through `paths.remote_path_for` + `assert_within_prefix`
- [ ] The token is never logged, printed, or written to a tracked file
- [ ] No new runtime dependencies (standard library only)

## Quality checklist

- [ ] Tests added/updated and passing locally (`python -m pytest tests`)
- [ ] `ruff check src tests` and `ruff format --check src tests` pass
- [ ] CHANGELOG updated under `## [Unreleased]`
- [ ] Commit messages follow Conventional Commits

## Related issues

Closes #
