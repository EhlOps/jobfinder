# k3s runbook

JobFinder is moving from docker compose to a single-node k3s cluster on the same host (#36, #38).
Until the cutover (#47) production keeps running on compose; k3s runs alongside it.

## Shape

- **Edge:** host systemd Caddy keeps 80/443 and terminates TLS (JobFinder and draw.ehlops.com).
- **Ingress:** k3s's Traefik listens on **127.0.0.1:8280** only, plain HTTP (`deploy/k3s/manifests/traefik-config.yaml`).
- **No servicelb:** klipper-lb would bind host ports 80/443.
- **SELinux:** enforcing, with the `k3s-selinux` policy.
- **firewalld:** 10.42.0.0/16 (pods) and 10.43.0.0/16 (services) are sources in the `trusted` zone. Nothing else is opened; the API server (6443) and kubelet (10250) stay unreachable from outside because the `public` zone doesn't allow them.
- **Secrets:** sealed-secrets controller in `kube-system`; `kubeseal` on the host. See `deploy/k8s/README.md` for sealing.
- **Manifests:** `deploy/k8s/` (Kustomize base + overlays).

Pinned versions live at the top of `deploy/k3s/install.sh` and in `manifests/sealed-secrets.yaml`. Upgrades are PRs that bump them.

## Install / re-converge

```sh
sudo bash deploy/k3s/install.sh
```

The script is idempotent. It prints the status of the compose site, the api and the canvas before and after, and exits 2 if any of them changed.

1. Installs `container-selinux` and the `k3s-selinux` policy. No el10 build exists yet, so it uses the el9 RPM, which only needs `container-selinux` (shipped by OL10).
2. Writes `/etc/rancher/k3s/config.yaml` (`write-kubeconfig-mode: 0600`, `selinux: true`, `disable: [servicelb]`) and copies `deploy/k3s/manifests/*.yaml` into `/var/lib/rancher/k3s/server/manifests/`, which k3s applies automatically.
3. Installs k3s at the pinned version (skipped if already at it).
4. Adds the cluster CIDRs to firewalld's `trusted` zone, runtime and permanent, **without** `firewall-cmd --reload` (a reload can drop Docker's rules for the running compose stack).
5. Copies the kubeconfig to `~opc/.kube/config` (0600) and installs `kubeseal`.
6. Waits for the node, Traefik and the sealed-secrets controller.

## Verify

```sh
kubectl get nodes                                   # Ready
kubectl -n kube-system get pods                     # all Running / Completed
sudo ss -ltnp | grep -E ':(80|443)\b'               # only caddy
sudo iptables-save | grep 'dport 8280.*DNAT'        # hostPort is a DNAT rule, -d 127.0.0.1 only (no socket in ss)
curl -s -m 4 http://$(hostname -I | cut -d' ' -f1):8280/ || echo 'not reachable off loopback: good'
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8280/          # 404: Traefik, no routes
kubeseal --fetch-cert | head -1                     # -----BEGIN CERTIFICATE-----
kubectl apply -k deploy/k8s/hello && kubectl -n jobfinder-hello rollout status deploy/hello
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8280/healthz   # 200
```

Gotchas this script already handles (keep them if you change it):

- **SELinux label.** With the installer's RPM step skipped it doesn't relabel either, and `/usr/local/bin/k3s` keeps `user_tmp_t`, which systemd can't execute ("Permission denied" in `journalctl -u k3s`). The script runs `restorecon` before starting k3s.
- **Traefik bind address.** The chart reuses `ports.web.hostIP` as the in-pod entrypoint address, so Traefik would listen only on the pod's loopback and refuse the DNAT'd traffic. `additionalArguments` re-binds the entrypoint to `:8000` inside the pod.
- **Rolling update on one node.** The new Traefik pod can't bind hostPort 8280 while the old one runs, so the strategy is `maxSurge: 0`.
- **kubectl as opc.** k3s's bundled kubectl ignores `~/.kube/config`; `/etc/profile.d/k3s-kubeconfig.sh` sets `KUBECONFIG` for non-root logins.
- **sudo PATH.** `secure_path` omits `/usr/local/bin`; the script prepends it.

If 8280 doesn't answer, check that the Traefik pod has `hostPort 8280` with `hostIP 127.0.0.1`
(`kubectl -n kube-system get pod -l app.kubernetes.io/name=traefik -o yaml | grep -A3 hostPort`) and that the
HelmChartConfig was applied (`kubectl -n kube-system get helmchartconfig traefik -o yaml`).

## Back up the sealed-secrets key

Every SealedSecret in git is useless without the controller's private key. Back it up **off the box, never in git**:

```sh
kubectl -n kube-system get secret -l sealedsecrets.bitnami.com/sealed-secrets-key -o yaml > sealed-secrets-key.yaml
# move it to a password manager / offline storage, then: shred -u sealed-secrets-key.yaml
```

Restore on a rebuilt cluster before the controller starts sealing with a new key:
`kubectl apply -f sealed-secrets-key.yaml && kubectl -n kube-system delete pod -l app.kubernetes.io/name=sealed-secrets`.
The controller renews its key every 30 days, so repeat the backup after renewals.

## Uninstall / roll back

Compose is never touched, so removing k3s returns the host to its previous state:

```sh
sudo /usr/local/bin/k3s-uninstall.sh
for c in 10.42.0.0/16 10.43.0.0/16; do
  sudo firewall-cmd --zone=trusted --remove-source=$c
  sudo firewall-cmd --permanent --zone=trusted --remove-source=$c
done
sudo dnf remove -y k3s-selinux && sudo rm -f /usr/local/bin/kubeseal ~opc/.kube/config
```

Then check `curl -sI http://127.0.0.1:8180/` and `https://draw.ehlops.com/`.

## Resources

The host has 4 vCPU / 22 GB. k3s with Traefik, CoreDNS, metrics-server and sealed-secrets adds roughly 0.6–1 GB; watch `free -g` while staging (#43) runs next to compose.
