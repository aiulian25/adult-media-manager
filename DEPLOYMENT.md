# 🚀 Deployment Guide - Adult Media Manager

Advanced deployment scenarios, security hardening, and production best practices.

---

## 📋 Table of Contents

1. [Production Deployment](#production-deployment)
2. [Reverse Proxy Setup](#reverse-proxy-setup)
3. [Security Hardening](#security-hardening)
4. [Network Configuration](#network-configuration)
5. [Backup & Recovery](#backup--recovery)
6. [Performance Tuning](#performance-tuning)
7. [Monitoring](#monitoring)
8. [Troubleshooting](#troubleshooting)

---

## 🌐 Production Deployment

### Docker Compose Production Configuration

```yaml
services:
  adult-media-manager:
    image: aiulian25/adult-media-manager:latest
    container_name: adult-media-manager
    restart: always
    ports:
      - "127.0.0.1:8889:8889"  # Bind to localhost only
    environment:
      - PUID=${PUID}
      - PGID=${PGID}
      - TPDB_API_KEY=${TPDB_API_KEY}
      - PRIVACY_MODE=true
    volumes:
      - ./data:/data
      - /mnt/media:/media:ro  # Read-only mount
    deploy:
      resources:
        limits:
          cpus: '2'
          memory: 1G
        reservations:
          memory: 256M
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "5"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8889/api/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s
```

### Building from Source

```bash
# Clone repository
git clone https://github.com/yourusername/adult-media-manager.git
cd adult-media-manager

# Build image
docker build -t adult-media-manager:local .

# Run with custom image
docker-compose up -d
```

---

## 🔒 Reverse Proxy Setup

### Nginx

```nginx
server {
    listen 443 ssl http2;
    server_name amm.yourdomain.com;

    ssl_certificate /etc/ssl/certs/amm.crt;
    ssl_certificate_key /etc/ssl/private/amm.key;

    # Security headers
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-XSS-Protection "1; mode=block" always;
    add_header Referrer-Policy "no-referrer" always;

    # Content Security Policy (adjust as needed)
    add_header Content-Security-Policy "default-src 'self'; img-src 'self' https://cdn.theporndb.net; script-src 'self'; style-src 'self' 'unsafe-inline';" always;

    # Client body size for large file operations
    client_max_body_size 0;

    location / {
        proxy_pass http://127.0.0.1:8889;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # WebSocket support (if needed)
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        
        # Timeouts for long operations
        proxy_connect_timeout 600;
        proxy_send_timeout 600;
        proxy_read_timeout 600;
    }
}

# Redirect HTTP to HTTPS
server {
    listen 80;
    server_name amm.yourdomain.com;
    return 301 https://$server_name$request_uri;
}
```

### Traefik

```yaml
services:
  adult-media-manager:
    image: aiulian25/adult-media-manager:latest
    container_name: adult-media-manager
    networks:
      - traefik
    environment:
      - TPDB_API_KEY=${TPDB_API_KEY}
    volumes:
      - ./data:/data
      - /mnt/media:/media
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.amm.rule=Host(`amm.yourdomain.com`)"
      - "traefik.http.routers.amm.entrypoints=websecure"
      - "traefik.http.routers.amm.tls=true"
      - "traefik.http.routers.amm.tls.certresolver=letsencrypt"
      - "traefik.http.services.amm.loadbalancer.server.port=8889"
      
      # Security headers
      - "traefik.http.middlewares.amm-headers.headers.stsSeconds=31536000"
      - "traefik.http.middlewares.amm-headers.headers.stsIncludeSubdomains=true"
      - "traefik.http.middlewares.amm-headers.headers.contentTypeNosniff=true"
      - "traefik.http.middlewares.amm-headers.headers.browserXssFilter=true"
      - "traefik.http.middlewares.amm-headers.headers.frameDeny=true"
      - "traefik.http.routers.amm.middlewares=amm-headers"

networks:
  traefik:
    external: true
```

### Caddy

```caddyfile
amm.yourdomain.com {
    reverse_proxy localhost:8889
    
    # Security headers
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Frame-Options "SAMEORIGIN"
        X-Content-Type-Options "nosniff"
        X-XSS-Protection "1; mode=block"
        Referrer-Policy "no-referrer"
    }
    
    # TLS
    tls your-email@domain.com
    
    # Timeouts for long operations
    timeouts {
        read 10m
        write 10m
    }
}
```

---

## 🔐 Security Hardening

### 1. API Key Management

**Never expose your API key:**
```bash
# Store in secure environment file
echo "TPDB_API_KEY=your_secure_key" > .env
chmod 600 .env  # Owner read/write only
```

**Rotate keys periodically:**
- Generate new key at ThePornDB every 6-12 months
- Update `.env` file
- Restart container: `docker-compose restart`

### 2. Network Security

**Internal Network Only:**
```yaml
# Bind to localhost only
ports:
  - "127.0.0.1:8889:8889"  # Only accessible from host
```

**VPN Access:**
```yaml
# Use with WireGuard/OpenVPN
networks:
  vpn:
    external: true
```

**Firewall Rules (UFW):**
```bash
# Allow only from VPN subnet
sudo ufw allow from 10.0.0.0/24 to any port 8889
sudo ufw deny 8889
```

### 3. File System Security

**Read-Only Media Mounts:**
```yaml
volumes:
  - /mnt/media:/media:ro  # Prevent accidental deletion
```

**Restrict Data Directory:**
```bash
chmod 700 /path/to/adult-media-manager/data
chown -R 1000:1000 /path/to/adult-media-manager/data
```

### 4. Container Security

**Run as Non-Root:**
```dockerfile
# Already configured in Dockerfile
USER amm
```

**Limit Capabilities:**
```yaml
security_opt:
  - no-new-privileges:true
cap_drop:
  - ALL
```

**AppArmor/SELinux:**
```yaml
security_opt:
  - apparmor=docker-default
  # or
  - label:type:container_runtime_t
```

### 5. Authentication (Optional)

**Add Basic Auth with Nginx:**
```nginx
location / {
    auth_basic "Restricted";
    auth_basic_user_file /etc/nginx/.htpasswd;
    proxy_pass http://127.0.0.1:8889;
}
```

Generate password file:
```bash
htpasswd -c /etc/nginx/.htpasswd admin
```

---

## 🌍 Network Configuration

### Multi-Network Setup

```yaml
services:
  adult-media-manager:
    networks:
      - frontend  # Web access
      - backend   # Storage access
      
networks:
  frontend:
    driver: bridge
  backend:
    driver: bridge
    internal: true  # No internet access
```

### NFS/SMB Mounts

**NFS Mount:**
```bash
# On host
sudo mount -t nfs nas.local:/media /mnt/media

# In docker-compose.yml
volumes:
  - /mnt/media:/media
```

**CIFS/SMB Mount:**
```bash
# On host
sudo mount -t cifs //nas/media /mnt/media -o username=user,password=pass,uid=1000,gid=1000

# Or use systemd mount
cat /etc/systemd/system/mnt-media.mount
```

**Docker Volume with NFS:**
```yaml
volumes:
  media:
    driver: local
    driver_opts:
      type: nfs
      o: addr=nas.local,rw
      device: ":/media"
```

---

## 💾 Backup & Recovery

### What to Backup

1. **History database:** `/data/history.json`
2. **Configuration:** `.env`, `docker-compose.yml`
3. **Custom templates** (if any): `/data/templates/`

### Automated Backup Script

```bash
#!/bin/bash
# backup-amm.sh

BACKUP_DIR="/backups/adult-media-manager"
DATA_DIR="/path/to/adult-media-manager/data"
DATE=$(date +%Y%m%d_%H%M%S)

# Create backup directory
mkdir -p "$BACKUP_DIR"

# Backup data
tar -czf "$BACKUP_DIR/data_$DATE.tar.gz" -C "$DATA_DIR" .

# Backup configs
cp .env "$BACKUP_DIR/env_$DATE.bak"
cp docker-compose.yml "$BACKUP_DIR/compose_$DATE.yml"

# Keep only last 30 days
find "$BACKUP_DIR" -name "*.tar.gz" -mtime +30 -delete

echo "Backup completed: $BACKUP_DIR/data_$DATE.tar.gz"
```

**Cron job:**
```bash
# Backup daily at 3 AM
0 3 * * * /path/to/backup-amm.sh >> /var/log/amm-backup.log 2>&1
```

### Disaster Recovery

```bash
# Stop container
docker-compose down

# Restore data
tar -xzf /backups/adult-media-manager/data_20240115_030000.tar.gz -C ./data/

# Restore config
cp /backups/adult-media-manager/env_20240115_030000.bak .env

# Start container
docker-compose up -d
```

---

## ⚡ Performance Tuning

### Resource Limits

```yaml
deploy:
  resources:
    limits:
      cpus: '4'        # Increase for large batches
      memory: 2G       # More memory for API caching
    reservations:
      cpus: '1'
      memory: 512M
```

### API Rate Limiting

ThePornDB has rate limits. For large collections:
```python
# In app/api/tpdb.py (if modifying)
# Add delay between requests
await asyncio.sleep(0.5)  # 2 requests per second
```

### Caching (Future Enhancement)

```yaml
environment:
  - CACHE_ENABLED=true
  - CACHE_TTL=3600  # 1 hour
```

---

## 📊 Monitoring

### Health Checks

```bash
# Manual health check
curl http://localhost:8889/api/health

# Expected response:
{"status": "healthy", "timestamp": "2024-01-15T12:00:00"}
```

### Container Logs

```bash
# View logs
docker-compose logs -f adult-media-manager

# Last 100 lines
docker-compose logs --tail=100 adult-media-manager

# Filter for errors
docker-compose logs adult-media-manager | grep ERROR
```

### Resource Usage

```bash
# Container stats
docker stats adult-media-manager

# Disk usage
du -sh /path/to/adult-media-manager/data
```

---

## 🐛 Troubleshooting

### Container Won't Start

**Check logs:**
```bash
docker-compose logs adult-media-manager
```

**Common issues:**
- Missing TPDB_API_KEY in `.env`
- Port 8889 already in use
- Volume mount permissions

### Permission Issues

```bash
# Fix data directory ownership
sudo chown -R 1000:1000 ./data

# Check container user
docker exec adult-media-manager id
```

### API Connection Failed

```bash
# Test from container
docker exec adult-media-manager curl -I https://theporndb.net

# Test DNS
docker exec adult-media-manager nslookup theporndb.net

# Check firewall
sudo iptables -L -n | grep 443
```

### Performance Issues

**Slow matching:**
- Reduce batch size (match 50-100 files at a time)
- Check network latency to TPDB
- Increase memory limit

**High CPU usage:**
- Normal during scanning/matching
- Reduce concurrent operations
- Check for infinite loops in logs

### Database Corruption

```bash
# Backup current
cp data/history.json data/history.json.bak

# Validate JSON
python3 -m json.tool data/history.json

# If corrupt, clear history
echo '{"entries": []}' > data/history.json
```

---

## 🔄 Updates & Maintenance

### Updating the Container

```bash
# Pull latest image
docker-compose pull

# Restart with new image
docker-compose up -d

# Remove old images
docker image prune -a
```

### Database Maintenance

```bash
# Compact history (keep last 1000 entries)
docker exec adult-media-manager python3 -c "
import json
with open('/data/history.json') as f:
    data = json.load(f)
data['entries'] = data['entries'][-1000:]
with open('/data/history.json', 'w') as f:
    json.dump(data, f, indent=2)
"
```

---

## 📞 Support & Resources

- **GitHub Issues:** [Report bugs](https://github.com/yourusername/adult-media-manager/issues)
- **TPDB API Status:** [status.theporndb.net](https://status.theporndb.net)
- **Docker Docs:** [docs.docker.com](https://docs.docker.com)

---

## ⚖️ Legal Considerations

1. **Content Ownership:** Only organize legally obtained content
2. **Privacy:** Protect your collection with proper access controls
3. **API Terms:** Respect ThePornDB's Terms of Service
4. **Local Laws:** Ensure compliance with local regulations
5. **Copyright:** Do not distribute or share organized metadata

---

**Remember:** This tool is for personal use only. Always respect content creators and copyright holders.
