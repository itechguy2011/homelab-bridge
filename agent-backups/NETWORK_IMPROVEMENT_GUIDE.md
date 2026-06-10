# 🌐 HOMELAB NETWORK & CONNECTIVITY IMPROVEMENT GUIDE
> For DevilDollz homelab — Dell PowerEdge R730xd, ESXi 7.0, 192.168.0.x
> Focus: improving device-to-device connections + reliable internet exposure

---

## Current State (what you have)

- **Internal network:** 192.168.0.x, gateway .1, DNS 8.8.8.8 / 8.8.4.4
- **Internet exposure:** Single Cloudflare tunnel (`homelab`, ID `d56a2691-5975-42d7-88fe-9643e50c5569`) running on vm-claude-hub (.191) in systemd token mode
- **Public routes:** n8n, bridge, sakura, hermes-api, remy — all → cutegrafiks.com subdomains
- **Recurring problem:** DNS resets to nothing (you fix with `echo "nameserver 8.8.8.8" | sudo tee /etc/resolv.conf`)

---

## TIER 1 — Highest Impact, Do These First

### 1. Fix DNS permanently across ALL VMs (stop the recurring reset)
The DNS keeps resetting because `systemd-resolved` or DHCP overwrites `/etc/resolv.conf`. The real fix is to lock it:

```bash
# On EACH VM:
# Option A — if using systemd-resolved (Debian 13 default):
sudo mkdir -p /etc/systemd/resolved.conf.d
sudo tee /etc/systemd/resolved.conf.d/dns.conf << 'EOF'
[Resolve]
DNS=8.8.8.8 8.8.4.4
FallbackDNS=1.1.1.1 1.0.0.1
EOF
sudo systemctl restart systemd-resolved
sudo ln -sf /run/systemd/resolve/resolv.conf /etc/resolv.conf

# Option B — if NOT using systemd-resolved, make resolv.conf immutable:
echo -e "nameserver 8.8.8.8\nnameserver 8.8.4.4" | sudo tee /etc/resolv.conf
sudo chattr +i /etc/resolv.conf   # locks the file so nothing can overwrite it
```

> `chattr +i` is the nuclear option — nothing can change resolv.conf until you run `chattr -i`. This permanently kills the recurring DNS problem.

### 2. Set static IPs via DHCP reservation (not per-VM static config)
Right now if a VM reboots and DHCP hands out a different IP, everything breaks. Two approaches:

**Best:** In your router (192.168.0.1), create DHCP reservations binding each VM's MAC address to its fixed IP. This means every VM always gets the same IP, but config stays centralized.

**Alternative:** Per-VM static config in `/etc/network/interfaces` or netplan — but this is what you likely have, and it's fragile across rebuilds.

### 3. Add a local DNS server (Pi-hole or dnsmasq) for internal name resolution
Instead of memorizing `192.168.0.202`, you'd use `vm-hermes.local`. This is huge for maintainability:

```bash
# On vm-dashboard or a lightweight VM, install dnsmasq:
sudo apt install dnsmasq -y
sudo tee -a /etc/dnsmasq.d/homelab.conf << 'EOF'
address=/vm-claude-hub.lab/192.168.0.191
address=/vm-backups.lab/192.168.0.192
address=/vm-firefly.lab/192.168.0.196
address=/vm-nextcloud.lab/192.168.0.198
address=/vm-openclaw.lab/192.168.0.199
address=/vm-vaultwarden.lab/192.168.0.200
address=/vm-dashboard.lab/192.168.0.201
address=/vm-hermes.lab/192.168.0.202
address=/vm-mcp-services.lab/192.168.0.204
EOF
sudo systemctl restart dnsmasq
```
Then point all VMs' DNS at this server's IP. Now `ssh jose@vm-hermes.lab` works everywhere.

---

## TIER 2 — Better Internet Exposure & Reliability

### 4. Add Cloudflare Tunnel redundancy (replica)
Right now your ENTIRE public presence depends on vm-claude-hub staying up. If .191 dies, every public URL goes down. Cloudflare supports running the **same tunnel** on a second machine as a hot replica:

```bash
# On a second VM (e.g. vm-dashboard .201), install cloudflared and use the SAME tunnel token:
sudo cloudflared service install <SAME_TUNNEL_TOKEN>
```
Cloudflare automatically load-balances and fails over between the two. Zero downtime if one VM reboots.

### 5. Move the tunnel off vm-claude-hub onto a dedicated lightweight VM
vm-claude-hub runs n8n, Gitea, cloudflared, the poller, Claude Code — it's overloaded. A tunnel competing for resources with n8n can cause dropped connections. Consider a tiny dedicated `vm-gateway` (.190?) that ONLY runs cloudflared. More stable, easier to reason about.

### 6. Enable Cloudflare Tunnel health checks + auto-restart
Add to the cloudflared systemd service:
```ini
[Service]
Restart=always
RestartSec=10
```
And set up a Uptime Kuma monitor (you already have it on .201:3010) pinging each public URL every 60s so you get alerted the moment a route goes down.

### 7. WARP / Tailscale for secure device access (instead of exposing ports)
For services you DON'T want public (Portainer, ESXi, Backrest), don't open them to the internet at all. Use **Tailscale** (free for personal use) to create a private mesh — you reach them from your phone/laptop anywhere as if you were on the LAN, with zero public exposure. This is far safer than Cloudflare tunnels for admin interfaces.

```bash
# On each VM you want private remote access to:
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

---

## TIER 3 — Internal Connection Quality

### 8. Verify ESXi virtual switch isn't bottlenecking
All 12 VMs share the physical NIC(s) through ESXi's vSwitch. If they're all on one vSwitch with one uplink, heavy traffic (backups, Nextcloud sync, Ollama calls) competes. Check:
- ESXi → Networking → Virtual Switches → confirm uplink speed (should be 1GbE or 10GbE)
- Consider a second vSwitch / NIC for backup traffic so it doesn't compete with live services

### 9. The Ollama latency problem is a NETWORK + COMPUTE issue
Your agents call `192.168.0.39:11434` (Windows desktop) for inference. This crosses from the ESXi VM network to a physical Windows box. Two improvements:
- **Move Ollama into a VM** with GPU passthrough (if the R730xd has a GPU) — keeps inference on the same vSwitch, much lower latency
- **Or** run a small fast model (llama3.2) for chat agents and reserve the Windows box's big models (qwen3:32b) for heavy coding tasks only

### 10. Jumbo frames for backup/storage traffic
If your switch and NICs support it, enabling jumbo frames (MTU 9000) on the storage/backup VLAN can significantly speed up Restic backups and Nextcloud sync between .192, .198. Only do this if ALL devices on that path support it.

---

## Priority Order (if you do nothing else)

1. **Lock DNS with `chattr +i`** — kills your #1 recurring problem (5 min)
2. **dnsmasq local DNS** — makes everything addressable by name (20 min)
3. **Cloudflare tunnel replica** on .201 — eliminates single point of failure (15 min)
4. **Tailscale** for admin interfaces — security + remote access (15 min)
5. **Move/dedicate Ollama** — fixes the agent latency that broke Sakura (varies)

---

## Quick Wins Summary Table

| Action | Effort | Impact | Fixes |
|---|---|---|---|
| `chattr +i` resolv.conf | 5 min | High | Recurring DNS resets |
| dnsmasq local DNS | 20 min | High | Name resolution, maintainability |
| CF tunnel replica | 15 min | High | Single point of failure |
| Tailscale mesh | 15 min | High | Secure admin access |
| Dedicated Ollama VM | varies | High | Agent response latency |
| DHCP reservations | 30 min | Medium | IP stability across reboots |
| Jumbo frames | 30 min | Medium | Backup/sync speed |
| ESXi vSwitch audit | 30 min | Medium | Internal bandwidth contention |
