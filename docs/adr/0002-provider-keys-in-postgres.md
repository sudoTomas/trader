# Provider keys are entered on any surface and stored encrypted in Postgres

Until the model picker, every credential lived in `.env` on the box and was changed by editing that file and restarting. Provider keys now come in through the desk, the CLI or MCP and are stored in Postgres, encrypted with a Fernet master key that stays in `.env` (`TRADER_SECRETS_KEY`). We chose this so a Provider can be added and tried without shell access or a restart, and kept MCP at full parity with the other surfaces (one service layer, three surfaces) rather than reserving keys for humans. The cost is a new secret-writing surface, so keys are write-only (only the last four characters are ever returned), only the full operator API key may add, replace or remove one (the capture-scoped key is refused), and every change is audited without its value.

## Considered Options

- Keys stay in `.env`, the UI only picks among configured Providers: no secret crosses the API, but adding a Provider means shell access and a restart.
- An encrypted file beside the xAI token store, or the OS keyring: kept out of database backups, but invisible to MCP and the CLI, and the keyring is awkward on a headless box.

## Consequences

- Database backups now contain ciphertext whose key is in `.env`; losing `.env` loses the stored keys, and leaking both leaks them.
- An agent with the operator key, via MCP, can wire in a credential.
