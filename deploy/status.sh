#!/usr/bin/env bash
# Quick health overview of the Vučić monitor on the VPS:  sudo bash /opt/vucic-monitor/deploy/status.sh
set -uo pipefail

echo "== Services"
for svc in vucic-monitor cloudflared vucic-quicktunnel; do
  if systemctl list-unit-files "${svc}.service" --no-legend 2>/dev/null | grep -q .; then
    printf '  %-20s %s\n' "$svc" "$(systemctl is-active "$svc")"
  fi
done

echo "== App"
if out=$(curl -fsS --max-time 5 http://127.0.0.1:8000/api/v1/status 2>/dev/null); then
  python3 - "$out" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
print(f"  stream:  {d['stream_status']}  {d.get('stream_title') or ''}")
print(f"  speaker: {d['speaker_state']}")
print(f"  last frame: {d.get('last_frame_at')}")
PY
else
  echo "  not responding — check: journalctl -u vucic-monitor -n 50"
fi

URL=$(journalctl -u vucic-quicktunnel --no-pager -o cat 2>/dev/null \
      | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | tail -1)
[[ -n "${URL:-}" ]] && echo "== Quick tunnel URL: $URL"

echo "== Security"
if command -v fail2ban-client >/dev/null; then
  banned=$(fail2ban-client status sshd 2>/dev/null | awk -F': *' '/Currently banned/ {print $2}')
  echo "  fail2ban sshd: ${banned:-?} IP(s) currently banned"
fi
if [[ -f /etc/ssh/sshd_config.d/10-vucic-hardening.conf ]]; then
  echo "  SSH: key-only login"
else
  echo "  SSH: password login allowed"
fi
if [[ -f /var/run/reboot-required ]]; then
  echo "  Security updates installed; a reboot is pending (sudo reboot)"
fi

echo "== Resources"
pid=$(systemctl show -p MainPID --value vucic-monitor)
if [[ -n "$pid" && "$pid" != "0" ]]; then
  ps -o pid,pcpu,rss,etime,cmd --ppid "$pid" -p "$pid" | awk 'NR==1 {print "  "$0; next} {$3=int($3/1024)"MB"; print "  "$0}'
fi
free -m | awk 'NR<=2 {print "  "$0}'
