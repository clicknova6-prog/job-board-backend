# Production deployment: Oracle Cloud Free (ARM)

One VM runs the whole backend with Docker Compose (`compose.prod.yaml`):

| Service | Role |
|---|---|
| `caddy` | Public HTTPS entry (ports 80/443), automatic Let's Encrypt certificate |
| `api` | FastAPI (uvicorn, 2 workers) |
| `worker` | Celery worker: imports, routing, sitemaps, cleanup |
| `beat` | Celery Beat scheduler |
| `migrate` | Runs `alembic upgrade head` on every start, then exits |
| `postgres` | PostgreSQL 16 (data in the `pgdata` volume) |
| `redis` | Broker, rate limits, filters cache |

Backups: `deploy/backup.sh` runs daily from a systemd timer: a compressed `pg_dump`, an integrity check, 14 days kept locally, plus an optional copy to OCI Object Storage.

---

## 1. Create the VM (once)

1. Sign up at cloud.oracle.com (Free Tier). Pick a home region close to your users; it can't be changed later.
2. **Compute → Instances → Create instance**
   - Image: **Canonical Ubuntu 24.04**
   - Shape: **Ampere → VM.Standard.A1.Flex**, **2 OCPU, 12 GB memory**
   - Networking: create a new VCN with a public subnet and **assign a public IPv4 address**
   - SSH keys: upload your public key, or let Oracle generate a pair and download it
   - Boot volume: 100 GB (Always Free covers 200 GB in total)
   - If you get "Out of capacity", retry later or try another availability domain. Upgrading the account to Pay As You Go also helps, and Always Free resources stay free.
3. **Networking → VCN → Security List → Ingress rules**: add TCP **80** and TCP **443** from `0.0.0.0/0` (22 is already open).
4. Optional but recommended: **Reserve the public IP** (Networking → IP Management) so it survives instance re-creation.

## 2. DNS

Create an **A record** for the API hostname (for example `api.yourdomain.com`) pointing at the VM's public IP. Caddy needs it to issue the certificate.

## 3. Prepare the server (once)

```bash
ssh ubuntu@<VM_IP>

# Ubuntu images on OCI ship an iptables REJECT rule: open 80/443 before it.
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p udp --dport 443 -j ACCEPT
sudo netfilter-persistent save

# Docker Engine + Compose plugin
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu && newgrp docker

# Clone (private repo: use a GitHub fine-grained token with read access)
sudo mkdir -p /opt/job-board-backend && sudo chown ubuntu: /opt/job-board-backend
git clone https://github.com/clicknova6-prog/job-board-backend.git /opt/job-board-backend
cd /opt/job-board-backend && git checkout main
```

## 4. Configure

```bash
cd /opt/job-board-backend
deploy/init-env.sh --domain api.yourdomain.com --email you@yourdomain.com
nano .env   # fill GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, CORS_ALLOWED_ORIGINS
```

`init-env.sh` generates the database password and JWT secret and writes `.env` with mode 600. Never commit `.env`. Keep a copy in your password manager.

## 5. Start

```bash
docker compose -f compose.prod.yaml up -d --build
docker compose -f compose.prod.yaml ps          # all "running"/"healthy", migrate "exited (0)"
curl https://api.yourdomain.com/health/ready    # {"status":"ok"}
```

The first build takes a few minutes; later builds reuse the dependency layer.

## 6. Bootstrap data (once)

```bash
c="docker compose -f compose.prod.yaml exec api"

# Admin login
$c python -m scripts.create_admin --email you@yourdomain.com --password '<strong password>'

# Jobg8 provider row, then set its feed URL + schedule via the admin API
$c python -m scripts.seed_provider

# IT site with its real domain and an unguessable key
SITE_KEY=$(openssl rand -hex 16); echo "IT site key: $SITE_KEY"   # give this to the frontend
$c python -m scripts.seed_sites it-tech --base-url https://<it-site-domain> --site-key "$SITE_KEY"
```

Then, with an admin token (`POST /admin/auth/login`), call `PATCH /admin/api/providers/{id}` with `feed_url` and `schedule_interval_minutes: 60`. Trigger the first import with `POST /admin/api/imports/providers/{id}/trigger`. Routing and sitemaps follow automatically.

## 7. Backups

```bash
sudo mkdir -p /var/backups/job-board && sudo chown ubuntu: /var/backups/job-board
deploy/backup.sh                                  # first run by hand: must print "ok"
sudo cp deploy/systemd/job-board-backup.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now job-board-backup.timer
systemctl list-timers job-board-backup.timer      # shows the next run (03:15 PKT)
```

**Off-box copy (recommended).** If the VM is lost, local backups go with it.
1. OCI Console → Storage → Object Storage → create a private bucket `job-board-backups` (Always Free: 20 GB).
2. Profile → **Customer Secret Keys** → generate one (S3-compatible access key and secret).
3. On the VM: `sudo apt install -y rclone && rclone config`. Choose type `s3`, provider `Other`, endpoint `https://<namespace>.compat.objectstorage.<region>.oraclecloud.com`, and the key and secret.
4. In `.env`: `RCLONE_REMOTE=oci:job-board-backups`. The next backup also uploads there (7 days kept).

**Restore:** `deploy/restore.sh /var/backups/job-board/job_board-<stamp>.dump`. It stops the app, restores, and starts the app again. Test a restore once after setup.

## 8. Updating

```bash
cd /opt/job-board-backend
git pull
docker compose -f compose.prod.yaml up -d --build   # migrations run automatically
docker image prune -f
```

## 9. Day-to-day

| Task | Command |
|---|---|
| Logs | `docker compose -f compose.prod.yaml logs -f api worker` |
| Status | `docker compose -f compose.prod.yaml ps` |
| DB shell | `docker compose -f compose.prod.yaml exec postgres psql -U jobboard job_board` |
| Restart one service | `docker compose -f compose.prod.yaml restart worker` |
| Backup now | `deploy/backup.sh` |

Container logs are capped at 5 × 10 MB per service.
