# EC2 deployment with Docker, Nginx, and Let's Encrypt

This deployment runs the FastAPI service only on Docker's private network.
Nginx is the sole public service and terminates HTTPS on ports 80 and 443.
MongoDB remains external (for example Atlas), using the existing `MONGODB_URL`
setting. Do not publish port 8000 or run MongoDB on this instance unless it is
separately hardened and backed up.

## 1. Prepare the EC2 host

Use an Ubuntu LTS instance with a public IPv4 address. In its security group,
allow inbound TCP 80 and 443 from the internet; restrict SSH (22) to the
administrator's IP. Install Docker Engine and the Docker Compose plugin, then
clone this backend repository on the instance.

Point `mlsctimetable.zssh.dev` at the EC2 public IP. The DNS record must
resolve before issuing a certificate. If the frontend is hosted separately,
set its production build variable to:

```text
VITE_BACKEND_URL=https://mlsctimetable.zssh.dev
```

## 2. Configure environment

On the EC2 instance, copy the sample file and fill every production setting:

```bash
cp .env.example .env
```

Add the hostname to that untracked `.env` file:

```text
DOMAIN=mlsctimetable.zssh.dev
PORT=8000
WEB_CONCURRENCY=1
CORS_ORIGINS=https://timetable.mlsctiet.com,https://www.timetable.mlsctiet.com
GOOGLE_OAUTH_REDIRECT_URI=https://mlsctimetable.zssh.dev/api/calendar/oauth/callback
JSON_MIRROR=0
```

Also add the existing MongoDB, Clerk, admin, and Google Calendar secrets. Do
not copy a local `.env` to a public repository. If MongoDB Atlas is used, add
the EC2 public IP to its network access list.

## 3. Issue the first certificate

Nginx needs a certificate file when it starts. Create a one-day placeholder,
then replace it through the HTTP-01 challenge:

```bash
export DOMAIN=mlsctimetable.zssh.dev
mkdir -p certbot/conf/live/$DOMAIN certbot/www
openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
  -keyout certbot/conf/live/$DOMAIN/privkey.pem \
  -out certbot/conf/live/$DOMAIN/fullchain.pem \
  -subj "/CN=$DOMAIN"
docker compose up -d --build
rm -rf "certbot/conf/live/$DOMAIN"
docker compose run --rm certbot certonly --webroot --webroot-path /var/www/certbot \
  --email you@example.com --agree-tos --no-eff-email -d "$DOMAIN"
docker compose exec nginx nginx -s reload
```

After validation, verify both the service and the TLS redirect:

```bash
curl -fsS https://$DOMAIN/healthz
curl -I http://$DOMAIN/healthz
docker compose ps
```

The first command must return `{"ok":true}` and the second a `301` response.

## 4. Renew automatically

Make the renewal helper executable and run it daily with cron (renewal itself
only occurs when a certificate is close to expiry):

```bash
chmod +x deploy/scripts/renew-certificates.sh
crontab -e
```

```cron
17 3 * * * cd /opt/mlsc-timetable-backend && ./deploy/scripts/renew-certificates.sh >> /var/log/mlsc-certbot.log 2>&1
```

Replace `/opt/mlsc-timetable-backend` with the repository path on the EC2
instance. The script reloads Nginx after Certbot checks the certificate.

## Updates and operations

For an application update, build and replace only the API container:

```bash
git pull
docker compose up -d --build
docker compose logs -f api nginx
```

The compose stack uses `restart: unless-stopped`, so it returns after an EC2
reboot once Docker itself is enabled. Check `https://<api-host>/healthz` from
your monitoring system. Keep `WEB_CONCURRENCY=1` while calendar sync is an
in-process worker; scale it only after moving that worker into a dedicated
service.
