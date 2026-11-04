# data/

Datasets for callscore.

| Dir | Committed? | Contents |
| --- | --- | --- |
| `profiles/` | yes | Scale profiles (`small` / `medium` / `full`) consumed by `callscore-synth`. |
| `fixtures/` | yes | Tiny, hand-checked inputs used by tests and docs. |
| `raw/` | no | Unprocessed source data dumps. |
| `db/` | no | Generated SQLite databases (`callscore.db`). |
| `exports/` | no | Reports and exports produced by the app. |

`raw/`, `db/` and `exports/` are gitignored except for their `.gitkeep` files.
