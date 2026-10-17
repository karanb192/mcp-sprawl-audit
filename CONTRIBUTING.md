# Contributing

Bug reports and pull requests are welcome.

## Adding a known server

`scripts/recipes.json` lists servers whose transport has been checked. A new entry needs:

- `http` (true or false), or `hosted` with the vendor's endpoint URL
- `how`: the flags or env vars that start it over HTTP, or why it cannot
- `auth`: how it treats callers without credentials, if it holds any
- `source`: a link to the docs or code that proves the above

Leave `stateful` set when one shared copy would mix state between sessions (a browser, a
working directory).

## A new agent or log format

Config readers live in `claude_entries`, `codex_entries` and `grok_entries`, and usage
patterns in `scan_usage`, all in `scripts/audit.py`. Add a fixture for the new format to
`tests/test_audit.py`. Fixtures are made-up machines: no real configs, paths or logs.

## Tests

```sh
python3 -m unittest discover -s tests
```

The script must stay read-only and must never print an env or header value.
