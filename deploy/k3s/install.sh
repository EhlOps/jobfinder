#!/usr/bin/env bash
# Install (or re-converge) the single-node k3s cluster on this host (#38). Safe to re-run.
#
# - Host systemd Caddy keeps 80/443; Traefik answers on 127.0.0.1:8280 only (manifests/traefik-config.yaml).
# - SELinux stays enforcing: the k3s-selinux policy is installed first.
# - firewalld trusts the pod/service CIDRs so pod-to-service traffic isn't dropped. Nothing is opened publicly,
#   and firewalld is never reloaded (a reload can drop Docker's rules for the running compose stack).
# - sealed-secrets is deployed by k3s itself from manifests/sealed-secrets.yaml; kubeseal is installed for sealing.
#
# Usage: sudo bash deploy/k3s/install.sh      Runbook: docs/k3s.md
set -euo pipefail

K3S_VERSION="v1.36.5+k3s1"
K3S_SELINUX_RPM="https://github.com/k3s-io/k3s-selinux/releases/download/v1.6.latest.1/k3s-selinux-1.6-1.el9.noarch.rpm"
KUBESEAL_VERSION="0.40.0"
CLUSTER_CIDRS=(10.42.0.0/16 10.43.0.0/16)   # k3s defaults: pods, services
KUBE_USER="${SUDO_USER:-opc}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
log() { printf '\n==> %s\n' "$*"; }
code() { curl -s -o /dev/null -m 10 -w '%{http_code}' "$1" || true; }

[[ $EUID -eq 0 ]] || { echo "run as root: sudo bash $0" >&2; exit 1; }

# What must keep working on this host: the compose stack behind host Caddy, and the canvas.
check_host() {
    printf '  jobfinder 127.0.0.1:8180  %s\n' "$(code http://127.0.0.1:8180/)"
    printf '  api       127.0.0.1:8100  %s\n' "$(code http://127.0.0.1:8100/api/health)"
    printf '  canvas    draw.ehlops.com %s\n' "$(code https://draw.ehlops.com/)"
}
log "Host services before"
check_host | tee /tmp/k3s-install-before.txt

log "SELinux policy for k3s"
if ! rpm -q k3s-selinux >/dev/null 2>&1; then
    # No el10 build is published yet; the el9 policy only needs container-selinux, which OL10 ships.
    dnf install -y container-selinux "$K3S_SELINUX_RPM"
fi
rpm -q k3s-selinux

log "k3s config and auto-deployed manifests"
install -d -m 0755 /etc/rancher/k3s /var/lib/rancher/k3s/server/manifests
cat > /etc/rancher/k3s/config.yaml <<'EOF'
# Managed by deploy/k3s/install.sh
write-kubeconfig-mode: "0600"
selinux: true
disable:
  - servicelb   # no klipper-lb: it would bind 80/443 on the host, which belong to host Caddy
EOF
install -m 0600 "$here"/manifests/*.yaml /var/lib/rancher/k3s/server/manifests/

log "k3s $K3S_VERSION"
if [[ "$(k3s --version 2>/dev/null | head -1)" != *"$K3S_VERSION"* ]]; then
    # The policy RPM is already installed above; the installer's own repo has no el10 entry.
    curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="$K3S_VERSION" INSTALL_K3S_SKIP_SELINUX_RPM=true sh -s - server
else
    systemctl enable --now k3s
fi

log "firewalld: trust cluster CIDRs (runtime + permanent, no reload)"
for cidr in "${CLUSTER_CIDRS[@]}"; do
    firewall-cmd -q --zone=trusted --query-source="$cidr" || firewall-cmd -q --zone=trusted --add-source="$cidr"
    firewall-cmd -q --permanent --zone=trusted --query-source="$cidr" \
        || firewall-cmd -q --permanent --zone=trusted --add-source="$cidr"
done
firewall-cmd --zone=trusted --list-sources

log "kubeconfig for $KUBE_USER"
home="$(getent passwd "$KUBE_USER" | cut -d: -f6)"
install -d -m 0700 -o "$KUBE_USER" -g "$KUBE_USER" "$home/.kube"
for _ in $(seq 60); do [[ -s /etc/rancher/k3s/k3s.yaml ]] && break; sleep 2; done
install -m 0600 -o "$KUBE_USER" -g "$KUBE_USER" /etc/rancher/k3s/k3s.yaml "$home/.kube/config"

log "kubeseal $KUBESEAL_VERSION"
if [[ "$(kubeseal --version 2>/dev/null)" != *"$KUBESEAL_VERSION"* ]]; then
    tmp="$(mktemp -d)"
    curl -sfL "https://github.com/bitnami/sealed-secrets/releases/download/v${KUBESEAL_VERSION}/kubeseal-${KUBESEAL_VERSION}-linux-arm64.tar.gz" \
        | tar -xz -C "$tmp" kubeseal
    install -m 0755 "$tmp/kubeseal" /usr/local/bin/kubeseal
    rm -rf "$tmp"
fi

log "Waiting for the node, Traefik and sealed-secrets"
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
k3s kubectl wait --for=condition=Ready node --all --timeout=180s
for _ in $(seq 90); do
    k3s kubectl -n kube-system get deploy traefik sealed-secrets-controller >/dev/null 2>&1 && break
    sleep 2
done
k3s kubectl -n kube-system rollout status deploy/traefik --timeout=300s
k3s kubectl -n kube-system rollout status deploy/sealed-secrets-controller --timeout=300s

log "Checks"
k3s kubectl get nodes -o wide
ss -ltnp | grep -E ':(80|443|8280)\b' || true
printf '  traefik 127.0.0.1:8280 %s (404 = up, no routes yet)\n' "$(code http://127.0.0.1:8280/)"
kubeseal --kubeconfig /etc/rancher/k3s/k3s.yaml --fetch-cert >/dev/null && echo "  kubeseal --fetch-cert ok"

log "Host services after"
check_host | tee /tmp/k3s-install-after.txt
if ! diff -q /tmp/k3s-install-before.txt /tmp/k3s-install-after.txt >/dev/null; then
    echo "!! host services changed status. Roll back: see docs/k3s.md 'Uninstall'" >&2
    exit 2
fi
log "Done. Smoke test: kubectl apply -k deploy/k8s/hello (see deploy/k8s/README.md)"
