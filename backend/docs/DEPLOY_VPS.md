# Deploying TradingSignal on a shared Hostinger VPS

Goal: put the site live on a VPS that already hosts 2-3 other sites, without touching them, with the domain
managed in a *different* Hostinger account. Everything below is **additive**: new Linux user, new folders, two new
systemd services, one new Nginx file for one hostname. Nothing existing is edited or restarted.

> This guide assumes the VPS fronts its sites with **Nginx** on Ubuntu/Debian. If step 1 shows Apache, OpenLiteSpeed,
> CloudPanel, Docker or Caddy instead, stop and adapt step 7 first (the Nginx file will not apply as is).

Replace these three values everywhere:

| Placeholder | Example | Meaning |
|---|---|---|
| `DOMAIN` | `signals.yourdomain.com` | the hostname the site will answer on |
| `PO_PORT` | `8001` | local port for the Pocket Option backend (must be free) |
| `QX_PORT` | `8002` | local port for the Quotex backend (must be free) |

**Subdomain or main domain.** A subdomain (`signals.example.com`) needs one DNS record and cannot disturb whatever the
main domain points at. The main domain (`example.com`) also works: add an `A` record for `@` (and keep `www` as a CNAME to
it), and the Nginx file redirects `www` to the main address. Only do this if nothing else uses the main domain.

---

## 1. Look before you touch (read-only)

Run on the VPS. None of this changes anything. Check the results before going on.

```bash
cat /etc/os-release | head -2; python3 --version        # need Python 3.11 or newer
free -h; df -h /                                         # RAM and disk headroom (need ~1.5 GB RAM free, 2 GB disk)
ss -ltnp | grep -E ':(80|443|8001|8002)\b'               # who owns 80/443; 8001/8002 should be empty
systemctl list-units --type=service --state=running | grep -Ei 'nginx|apache|httpd|litespeed|caddy|docker|cloudpanel'
ls /etc/nginx/conf.d /etc/nginx/sites-enabled 2>/dev/null   # how the existing sites are configured
nginx -T 2>/dev/null | grep -E 'server_name|listen' | sort -u   # hostnames already served
```

- If `8001` or `8002` is taken, pick other free ports and use them in every step.
- If Python is older than 3.11, install a newer one alongside the system one (do not replace it), e.g. Ubuntu 22.04:
  `add-apt-repository ppa:deadsnakes/ppa && apt install python3.12 python3.12-venv`, then use `python3.12` below.

## 2. DNS (domain in the other Hostinger account)

You do **not** need to move the domain or the nameservers. You only add one record where the domain's DNS is managed:

1. Log in to the Hostinger account that owns the domain, open **Domains > the domain > DNS / Nameservers**.
2. If it uses Hostinger's nameservers (`ns1.dns-parking.com` / `ns2.dns-parking.com`), edit the DNS zone there.
   If it uses someone else's nameservers (Cloudflare etc.), add the record there instead.
3. Add: **Type A**, **Name** `signals` (just the subdomain part), **Points to** the VPS IPv4 address, TTL 300.
   Do not add an AAAA record unless you have confirmed the VPS serves IPv6.
4. Check from your PC: `nslookup DOMAIN` should show the VPS IP (can take a few minutes up to a few hours).

The VPS account and the domain account never need to be linked.

## 3. Create an isolated user and folders

```bash
adduser --system --group --home /opt/tradingsignal --shell /usr/sbin/nologin tsignal
mkdir -p /var/lib/tradingsignal /var/www/ts-acme
chown tsignal:tsignal /var/lib/tradingsignal
apt install -y git python3-venv          # only adds packages; skip if already present

sudo -u tsignal git clone https://github.com/kritagyapandey21/Vedant.git /opt/tradingsignal/repo
# the services and Nginx use /opt/tradingsignal/{frontend,backend}; they point into the git checkout
sudo -u tsignal ln -s /opt/tradingsignal/repo/frontend /opt/tradingsignal/frontend
sudo -u tsignal ln -s /opt/tradingsignal/repo/backend  /opt/tradingsignal/backend
chmod 755 /opt/tradingsignal /opt/tradingsignal/repo /opt/tradingsignal/repo/frontend
chmod 750 /opt/tradingsignal/repo/backend       # Nginx (www-data) can read the frontend, never the backend or .env files
```

## 4. Python environment (one shared venv for both backends)

```bash
sudo -u tsignal python3 -m venv /opt/tradingsignal/venv
cd /opt/tradingsignal/backend
sudo -u tsignal /opt/tradingsignal/venv/bin/pip install -U pip
sudo -u tsignal /opt/tradingsignal/venv/bin/pip install -r pocketoption/requirements.txt
sudo -u tsignal /opt/tradingsignal/venv/bin/pip install -r quotex/requirements.txt
```

Quotex sits behind Cloudflare, which blocks every Python connection from a server (HTTP 403) and even Playwright's
Chromium. The only thing that gets through is a **real Chrome** on a virtual screen, so the Quotex backend opens its
socket from JavaScript inside Chrome (see `backend/quotex_lib/api_quotex/browser_ws.py`, background in
`tackle.md` in your quotex-api repo). Install Chrome for Testing, its driver and the virtual screen. Skip this if you only run Pocket Option:

```bash
bash /opt/tradingsignal/backend/deploy/install_quotex_browser.sh
```

It only adds missing apt packages and puts Chrome in `/opt/tradingsignal/browsers/cft`.

## 5. Secrets (`.env` files, never committed)

Generate two different long random values:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

`/opt/tradingsignal/backend/quotex/.env`
```
QX_EMAIL=your quotex login email
QX_PASSWORD=your quotex password
QX_IS_DEMO=true
ACCESS_IDS=comma,separated,trader,ids        # who may log in. Empty means ANYONE can, so always set it
ADMIN_KEY=<random value>
SESSION_SECRET=<random value>
ANTHROPIC_API_KEY=<optional, enables the Claude review>
```

`/opt/tradingsignal/backend/pocketoption/.env` (copy `.env.example` and fill it in)
```
SESSION_SECRET=<a different random value>
ADMIN_KEY=<random value>
LO_UID=<Pocket Option session token>         # without it the backend falls back to Binance crypto data
REQUIRE_VERIFICATION=false                    # false = any trader ID may sign in; true needs the TELEGRAM_* values (Telegram bot check)
ANTHROPIC_API_KEY=<optional>
```

```bash
chown tsignal:tsignal /opt/tradingsignal/backend/*/.env && chmod 600 /opt/tradingsignal/backend/*/.env
```

## 6. Services (systemd)

```bash
cd /opt/tradingsignal/backend/deploy
for s in pocketoption quotex; do
  sed -e 's/__DOMAIN__/DOMAIN/g' -e 's/__PO_PORT__/8001/g' -e 's/__QX_PORT__/8002/g' \
      tradingsignal-$s.service > /etc/systemd/system/tradingsignal-$s.service
done
systemctl daemon-reload
systemctl enable --now tradingsignal-pocketoption tradingsignal-quotex
systemctl status tradingsignal-pocketoption tradingsignal-quotex --no-pager | head -20
curl -s http://127.0.0.1:8001/health; echo; curl -s http://127.0.0.1:8002/health; echo
```

Both units bind to `127.0.0.1` only, run as the unprivileged `tsignal` user, and are capped (`MemoryMax`, `CPUQuota`)
so a runaway process cannot starve the other sites.

## 7. Nginx and HTTPS (reload, never restart)

Step A, a tiny HTTP-only block so Let's Encrypt can verify the hostname:

```bash
cat > /etc/nginx/conf.d/tradingsignal-acme.conf <<'EOF'
server {
    listen 80;
    server_name DOMAIN www.DOMAIN;
    location /.well-known/acme-challenge/ { root /var/www/ts-acme; }
    location / { return 404; }
}
EOF
nginx -t && systemctl reload nginx
apt install -y certbot
certbot certonly --webroot -w /var/www/ts-acme -d DOMAIN -d www.DOMAIN
```

`certonly --webroot` only creates the certificate; unlike `certbot --nginx` it does **not** edit any Nginx file, so the
other sites' configs stay untouched. Renewal is automatic (`systemctl list-timers | grep certbot`).

Step B, the real site (replaces the temporary file):

```bash
rm /etc/nginx/conf.d/tradingsignal-acme.conf
sed -e 's/__DOMAIN__/DOMAIN/g' -e 's/__PO_PORT__/8001/g' -e 's/__QX_PORT__/8002/g' \
    /opt/tradingsignal/backend/deploy/nginx-tradingsignal.conf > /etc/nginx/conf.d/tradingsignal.conf
nginx -t && systemctl reload nginx
```

If `nginx -t` prints an error, **do not reload**: delete `/etc/nginx/conf.d/tradingsignal.conf`, fix, and retry. Nothing
changes for live sites until a reload succeeds, and a reload keeps serving the old config if the new one is invalid.

## 8. Check it

- `https://DOMAIN/` landing page, `https://DOMAIN/login/` login, pick Quotex or Pocket Option, sign in with an allowed ID.
- Quotex needs about 6 minutes after the first start before `/signal` returns anything.
- Other sites: open them once to confirm they are unchanged.
- Logs: `journalctl -u tradingsignal-quotex -f` and `journalctl -u tradingsignal-pocketoption -f`.

## 9. Updating later

```bash
sudo -u tsignal git -C /opt/tradingsignal/repo pull
systemctl restart tradingsignal-pocketoption tradingsignal-quotex     # only needed for backend changes
```
`.env` files and `*.sqlite3` are git-ignored, so a pull never touches your secrets or user data. Frontend-only
changes need no restart. Restart the Quotex service sparingly (see the rate-limit note below).

## 10. Removing everything cleanly

```bash
systemctl disable --now tradingsignal-pocketoption tradingsignal-quotex
rm /etc/systemd/system/tradingsignal-*.service /etc/nginx/conf.d/tradingsignal.conf
systemctl daemon-reload && nginx -t && systemctl reload nginx
certbot delete --cert-name DOMAIN
userdel tsignal && rm -rf /opt/tradingsignal /var/lib/tradingsignal /var/www/ts-acme
```
Then delete the `signals` A record in the domain account. Nothing else on the server was changed.

---

## Why this will not interfere with your other sites

| Risk | How it is avoided |
|---|---|
| Port clash | backends use free local ports, bound to 127.0.0.1 (step 1 checks they are free) |
| Nginx clash | one new file, one `server_name`, never the default server, all zone names prefixed `ts_`, no global settings |
| HSTS / cookies leaking to the parent domain | HSTS is per-hostname with no `includeSubDomains`, no cookies are set by the site |
| Certbot rewriting other configs | `certonly --webroot`, so no existing Nginx file is edited |
| Downtime | only `reload` is used, and only after `nginx -t` passes |
| Resource hogging | systemd `MemoryMax` / `CPUQuota` caps per service |
| File access | dedicated `tsignal` user, backend folder not readable by Nginx or other users, `.env` is mode 600 |

## Known limits to expect

- **Quotex on a server:** Quotex cannot be logged into with plain Python from a server (Cloudflare 403), and a session
  copied from another machine is rejected or expires within hours. Log in once inside the server's own Chrome instead:

  ```bash
  systemctl stop tradingsignal-quotex
  sudo -u tsignal env QUOTEX_BROWSER_DIR=/opt/tradingsignal/browsers/cft xvfb-run -a       /opt/tradingsignal/venv/bin/python /opt/tradingsignal/backend/quotex_lib/tools/browser_login.py       --env /opt/tradingsignal/backend/quotex/.env       --out /opt/tradingsignal/backend/quotex/qx_data/sessions/session.json
  systemctl start tradingsignal-quotex
  ```

  The tool signs in with `QX_EMAIL` / `QX_PASSWORD` from that `.env`, asks you to type the code Quotex emails, and saves a
  `session.json` (mode 600) with the session and the long-lived account cookies. When Quotex later rejects the session the
  service renews it through Chrome with those cookies; if that fails the log says to run this tool again. Use a **demo**
  account.
- **Quotex rate limit:** restarting the Quotex service often triggers HTTP 429. Restart it rarely.
- **Pocket Option:** needs a valid `LO_UID` token; tokens expire and must be replaced.
- **Memory:** the Quotex service plus headless Chromium needs roughly 600-900 MB at login time. If the VPS is already tight,
  watch `free -h` after step 6.
- **Restarts sign everyone out** unless `SESSION_SECRET` is set (it is, in step 5).
