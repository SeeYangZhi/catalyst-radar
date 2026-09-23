# Security Policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Report vulnerabilities privately through GitHub: open the repository's
**Security** tab and click **Report a vulnerability** (GitHub private
vulnerability reporting / security advisories). Include:

- a description of the issue and its impact;
- steps to reproduce or a proof of concept;
- the affected version or commit;
- any suggested fix.

You should get an acknowledgement within a few days. Once a fix is available we
will publish an advisory and credit you unless you prefer otherwise.

## Supported versions

Only the latest commit on `main` is supported. Self-hosters should update
regularly.

## Scope and hardening notes

Catalyst Radar is designed to be self-hosted by one operator. When deploying:

- Set `ENVIRONMENT=production` and replace `JWT_SECRET_KEY`,
  `DEFAULT_ADMIN_PASSWORD` and `POSTGRES_PASSWORD`. The api refuses to start in
  production with the shipped JWT secret or admin password.
- Set `TELEGRAM_ADMIN_CHAT_ID` to your own chat id and keep
  `TELEGRAM_OPEN_SUBSCRIBE=false` unless you intend anyone who finds the bot to
  see your watchlist and alerts.
- Set `TELEGRAM_WEBHOOK_SECRET` when using webhook mode.
- Do not expose Postgres or Redis ports publicly; `docker-compose.prod.yml`
  keeps them on the internal network and binds the app to `127.0.0.1`.
- Keep `backend/.env` out of version control.

Reports about third-party data sources (EODHD, OpenAI, Telegram, exchange
websites) should go to those providers.
