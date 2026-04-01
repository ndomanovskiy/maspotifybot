#!/bin/bash
# Daily database backup — dumps to backups/ and pushes to GitHub
set -e

cd /opt/maspotifybot

BACKUP_FILE="backups/maspotify_$(date +%Y-%m-%d).sql.gz"

# Dump and compress
docker exec maspotifybot-db-1 pg_dump -U maspotify maspotify | gzip > "$BACKUP_FILE"

# Keep only last 7 backups in repo
ls -t backups/maspotify_*.sql.gz 2>/dev/null | tail -n +8 | xargs -r rm

# Commit and push
git add backups/
git diff --cached --quiet && exit 0  # nothing to commit
git commit -m "backup: $(date +%Y-%m-%d)"
git push origin main
