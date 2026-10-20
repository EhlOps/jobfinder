#!/usr/bin/env bash
# Install (or re-converge) the single-node k3s cluster on this host (#38). Safe to re-run.
#
# - Host systemd Caddy keeps 80/443; Traefik answers on 127.0.0.1:8280 only (manifests/traefik-config.yaml).
# - SELinux stays enforcing: the k3s-selinux policy is installed first.
# - firewalld trusts the pod/service CIDRs so pod-to-service traffic isn't dropped. Nothing is opened publicly,
#   and firewalld is never reloaded (a reload can drop Docker's rules for the running compose stack).
# - sealed-secrets is deployed by k3s itself from manifests/sealed-secrets.yaml; kubeseal is installed for sealing.
# - KEDA (#52, autoscales worker-io) likewise, from manifests/keda.yaml; its chart version is pinned there.
#
# Usage: sudo bash deploy/k3s/install.sh      Runbook: docs/k3s.md
set -euo pipefail
export PATH="/usr/local/bin:$PATH"   # sudo's secure_path leaves out where k3s and kubeseal live

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
# k3s reads config.yaml only at start; remember it so a change restarts a running server below.
# Manifests need no restart: k3s watches that directory.
config_before="$(sha256sum /etc/rancher/k3s/config.yaml 2>/dev/null || true)"
cat > /etc/rancher/k3s/config.yaml <<'EOF'
# Managed by deploy/k3s/install.sh
write-kubeconfig-mode: "0600"
selinux: true
disable:
  - servicelb   # no klipper-lb: it would bind 80/443 on the host, which belong to host Caddy
EOF
install -m 0600 "$here"/manifests/*.yaml /var/lib/rancher/k3s/server/manifests/
config_changed=false
[[ "$(sha256sum /etc/rancher/k3s/config.yaml)" == "$config_before" ]] || config_changed=true

log "k3s $K3S_VERSION"
version_before="$(k3s --version 2>/dev/null | head -1 || true)"
if [[ "$version_before" != *"$K3S_VERSION"* ]]; then
    # The policy RPM is already installed above; the installer's own repo has no el10 entry.
    # INSTALL_K3S_SKIP_START: with the RPM step skipped the installer also skips relabelling, and the binary
    # keeps the user_tmp_t label it was downloaded with, which systemd (init_t) may not execute.
    curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="$K3S_VERSION" INSTALL_K3S_SKIP_SELINUX_RPM=true \
        INSTALL_K3S_SKIP_START=true sh -s - server
fi
restorecon -R /usr/local/bin/k3s /etc/rancher /var/lib/rancher
# `enable --now` is a no-op on a running unit, so an upgrade or config change would leave the old server up.
if systemctl is-active --quiet k3s \
    && { $config_changed || [[ "$(k3s --version | head -1)" != "$version_before" ]]; }; then
    echo "  binary or config changed: restarting k3s"
    systemctl restart k3s
fi
systemctl enable --now k3s

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
# A dedicated file, so other contexts in ~/.kube/config are never clobbered.
install -m 0600 -o "$KUBE_USER" -g "$KUBE_USER" /etc/rancher/k3s/k3s.yaml "$home/.kube/k3s.yaml"
# k3s's bundled kubectl ignores ~/.kube/config and falls back to the root-only /etc/rancher/k3s/k3s.yaml.
# KUBECONFIG lists k3s.yaml first (its context is current) with ~/.kube/config merged in; a KUBECONFIG
# the user already set is left alone.
cat > /etc/profile.d/k3s-kubeconfig.sh <<'EOF'
# Managed by deploy/k3s/install.sh
if [ "$(id -u)" -ne 0 ] && [ -z "${KUBECONFIG:-}" ] && [ -r "$HOME/.kube/k3s.yaml" ]; then
    KUBECONFIG="$HOME/.kube/k3s.yaml"
    if [ -r "$HOME/.kube/config" ]; then KUBECONFIG="$KUBECONFIG:$HOME/.kube/config"; fi
    export KUBECONFIG
fi
EOF

log "kubeseal $KUBESEAL_VERSION"
if [[ "$(kubeseal --version 2>/dev/null)" != *"$KUBESEAL_VERSION"* ]]; then
    tmp="$(mktemp -d)"
    curl -sfL "https://github.com/bitnami/sealed-secrets/releases/download/v${KUBESEAL_VERSION}/kubeseal-${KUBESEAL_VERSION}-linux-arm64.tar.gz" \
        | tar -xz -C "$tmp" kubeseal
    install -m 0755 "$tmp/kubeseal" /usr/local/bin/kubeseal
    rm -rf "$tmp"
fi

log "Waiting for the node, Traefik, sealed-secrets and KEDA"
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
k3s kubectl wait --for=condition=Ready node --all --timeout=180s
for _ in $(seq 150); do   # KEDA's Deployments appear only once helm-controller has fetched the chart
    k3s kubectl -n kube-system get deploy traefik sealed-secrets-controller >/dev/null 2>&1 \
        && k3s kubectl -n keda get deploy keda-operator keda-operator-metrics-apiserver >/dev/null 2>&1 && break
    sleep 2
done
k3s kubectl -n kube-system rollout status deploy/traefik --timeout=300s
k3s kubectl -n kube-system rollout status deploy/sealed-secrets-controller --timeout=300s
k3s kubectl -n keda rollout status deploy/keda-operator --timeout=300s
k3s kubectl -n keda rollout status deploy/keda-operator-metrics-apiserver --timeout=300s

log "Checks"
k3s kubectl get nodes -o wide
ss -ltnp | grep -E ':(80|443|8280)\b' || true
printf '  traefik 127.0.0.1:8280 %s (404 = up, no routes yet)\n' "$(code http://127.0.0.1:8280/)"
kubeseal --kubeconfig /etc/rancher/k3s/k3s.yaml --fetch-cert >/dev/null && echo "  kubeseal --fetch-cert ok"
k3s kubectl wait --for=condition=Available apiservice/v1beta1.external.metrics.k8s.io --timeout=120s

log "Host services after"
check_host | tee /tmp/k3s-install-after.txt
if ! diff -q /tmp/k3s-install-before.txt /tmp/k3s-install-after.txt >/dev/null; then
    echo "!! host services changed status. Roll back: see docs/k3s.md 'Uninstall'" >&2
    exit 2
fi
log "Done. Smoke test: kubectl apply -k deploy/k8s/hello (see deploy/k8s/README.md)"
