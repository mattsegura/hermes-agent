# Kanban Board Factory Model

Hermes Kanban has one current board model. A board is a directory with a
`board.json`, an isolated SQLite database, workspaces, and logs. Existing
metadata-less board directories remain readable and can be migrated by writing a
current `board.json`, but they are safe-idle/unowned until dispatcher ownership
is explicit.

## board.json

`board.json` supports three current-model metadata groups:

- `objective` describes why the board exists: `statement`, `success`, and
  `constraints`.
- `runtime` describes how the board is operated: `mode`, `dispatcher.profile`,
  and optional role profiles for `ceo`, `optimizer`, and `worker`.
- `workflow` describes semantic work stages. These stages are not scheduler
  statuses.

Default `goal` and `company` boards get an objective-first scaffold with:

```text
intake -> plan -> execute -> verify -> deliver -> improve
```

`--runtime kernel` creates a plain isolated board and omits `objective`,
`runtime`, and `workflow` unless the caller supplies a custom workflow.

## Runtime Modes

- `kernel`: isolated board storage and task lifecycle only.
- `goal`: objective-first board for a bounded goal.
- `company`: objective-first board for a company/business operating surface.

## Dispatcher Ownership

Gateway dispatch is explicit. A gateway dispatches a board only when one of
these is true:

- `board.json` has `runtime.dispatcher.profile` equal to the gateway active
  profile.
- `kanban.dispatch_boards` includes the board slug.
- `kanban.dispatch_boards` is `"*"`.

Boards without ownership are safe-idle/unowned: list, inspect, migrate, and
edit still work, but gateway dispatch does not auto-spawn workers for them.
