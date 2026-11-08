# Examples

- `upstatus.toml`: a configuration with every kind of check, a webhook and a private CA.
- `cron.txt`: crontab lines (no daemon needed).
- `upstatus.service`: a hardened systemd unit for `upstatus watch`. Put the webhook URL in an environment file with
  restricted rights (`EnvironmentFile=`), not in the unit.
- Publishing the page: copy `public/` to any web server (`rsync -a public/ web:/var/www/status/`), or push it to a
  branch served by GitHub Pages.
