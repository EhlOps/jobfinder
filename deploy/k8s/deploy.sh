#!/usr/bin/env bash
# Deploys one overlay at one commit's images (#43):
#
#   deploy/k8s/deploy.sh <staging|prod> sha-<commit>
#
#   1. Pins each image. images.yml only rebuilds what changed (a frontend-only push builds only web),
#      so per image, the first that exists in GHCR of: ghcr.io/ehlops/jobfinder-<name>:sha-<commit>;
#      the sha- tags in DEPLOY_CANDIDATE_TAGS (the commit's ancestors, newest first, which the SSH
#      wrapper passes, so a commit that only rebuilt web still gets the newest api built before it);
#      else the image that Deployment already runs; else (first deploy) the digest `dev`/`main`
#      points at now. Everything ends up pinned by an immutable tag or digest, never a moving tag.
#   2. Applies everything except the app Deployments, with a fresh migrate Job, and waits for the Job.
#      A failed migration stops here: no pod has rolled.
#   3. Applies the Deployments (label jobfinder.io/rollout=app; worker-io's ScaledObject too) and waits
#      for every rollout. Then lifts a KEDA pause a script left behind (pg-lib.sh, keda_pause).
#
# Uses the manifests in DEPLOY_K8S_DIR (default: next to this script); they must be that commit's.
# At the end it records the tag (and DEPLOY_COMMIT, when given) as annotations on the namespace.
# With DEPLOY_PINS_OUT=<file>, it also writes what step 1 pinned there, one `<image> <tag-or-digest>`
# line per image (api, worker, renderer, web), for the run's summary (#118).
# Needs kubectl (KUBECONFIG pointing at the cluster) and curl. GitHub Actions reaches it through the
# SSH forced command deploy-ssh.sh <staging|prod>; docs/k3s.md, "Staging", has the whole flow.
set -euo pipefail

usage() { echo "usage: $0 <staging|prod> sha-<7-40 hex>" >&2; exit 2; }
[ $# -eq 2 ] || usage
overlay=$1 tag=$2
[[ $tag =~ ^sha-[0-9a-f]{7,40}$ ]] || usage
case $overlay in
  staging) ns=jobfinder-staging fallback=dev ;;
  prod)    ns=jobfinder         fallback=main ;;
  *) usage ;;
esac

k8s_dir=$(cd "${DEPLOY_K8S_DIR:-$(dirname "$0")}" && pwd)
[ -d "$k8s_dir/overlays/$overlay" ] || usage
candidates=()
for t in ${DEPLOY_CANDIDATE_TAGS:-}; do
  [[ $t =~ ^sha-[0-9a-f]{7,40}$ ]] && candidates+=("$t")
done
REGISTRY=ghcr.io/ehlops
# image name -> a Deployment that runs it (the api image also runs the scheduler and the migrate Job;
# the worker image runs worker-ai and worker-io)
declare -A WORKLOAD=([api]=api [worker]=worker-ai [renderer]=renderer [web]=web)
JOB_TIMEOUT=${JOB_TIMEOUT:-900}          # seconds; the Job's activeDeadlineSeconds is 900 too
ROLLOUT_TIMEOUT=${ROLLOUT_TIMEOUT:-600s}

log() { printf '[deploy %s] %s\n' "$overlay" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# Prints the registry's digest for <name>:<ref>, nothing when it doesn't exist (404); exits on any
# other answer, so a registry outage never silently falls back to an old image.
remote_digest() {
  local repo="ehlops/jobfinder-$1" ref=$2 token headers code
  token=$(curl -fsS --max-time 20 "https://ghcr.io/token?scope=repository:${repo}:pull" \
          | sed -n 's/.*"token":"\([^"]*\)".*/\1/p') || die "ghcr.io token request failed for $repo"
  [ -n "$token" ] || die "no token from ghcr.io for $repo"
  headers=$(curl -sS --max-time 20 -I -o - -w '\n%{http_code}' \
      -H "Authorization: Bearer ${token}" \
      -H "Accept: application/vnd.oci.image.index.v1+json,application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.docker.distribution.manifest.v2+json" \
      "https://ghcr.io/v2/${repo}/manifests/${ref}") || die "ghcr.io unreachable checking ${repo}:${ref}"
  code=$(tail -n1 <<<"$headers")
  case $code in
    200) grep -i '^docker-content-digest:' <<<"$headers" | awk '{print $2}' | tr -d '\r' ;;
    404) ;;
    *) die "ghcr.io answered HTTP $code for ${repo}:${ref}" ;;
  esac
}

# Records <image> <tag-or-digest> in DEPLOY_PINS_OUT, when set (#118).
record_pin() {
  [ -z "${DEPLOY_PINS_OUT:-}" ] || printf '%s %s\n' "$1" "$2" >> "$DEPLOY_PINS_OUT"
}

# Prints a kustomize `images:` entry pinning <name>.
pin_image() {
  local name=$1 deployed digest t
  for t in "$tag" ${candidates[@]+"${candidates[@]}"}; do
    # Assigned on its own line so a die() in the substitution stops the script (set -e).
    digest=$(remote_digest "$name" "$t")
    if [ -n "$digest" ]; then
      if [ "$t" = "$tag" ]; then log "$name: $t"; else log "$name: not rebuilt at $tag, newest is $t"; fi
      printf '  - name: %s/jobfinder-%s\n    newTag: %s\n' "$REGISTRY" "$name" "$t"
      record_pin "$name" "$t"
      return
    fi
  done
  deployed=$(kubectl -n "$ns" get deployment "${WORKLOAD[$name]}" \
             -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)
  case $deployed in
    "$REGISTRY/jobfinder-$name@"sha256:*)
      log "$name: not rebuilt at $tag, keeping ${deployed#*@}"
      printf '  - name: %s/jobfinder-%s\n    digest: %s\n' "$REGISTRY" "$name" "${deployed#*@}"
      record_pin "$name" "${deployed#*@}" ;;
    "$REGISTRY/jobfinder-$name:"sha-*)
      log "$name: not rebuilt at $tag, keeping ${deployed##*:}"
      printf '  - name: %s/jobfinder-%s\n    newTag: %s\n' "$REGISTRY" "$name" "${deployed##*:}"
      record_pin "$name" "${deployed##*:}" ;;
    *)
      digest=$(remote_digest "$name" "$fallback")
      [ -n "$digest" ] || die "$name: neither $tag nor $fallback exists in GHCR, and nothing is deployed"
      log "$name: not rebuilt at $tag and not deployed yet, using $fallback ($digest)"
      printf '  - name: %s/jobfinder-%s\n    digest: %s\n' "$REGISTRY" "$name" "$digest"
      record_pin "$name" "$digest" ;;
  esac
}

# Polls until the Job completes (0) or fails (1).
wait_for_job() {
  local deadline=$((SECONDS + JOB_TIMEOUT)) status
  while [ $SECONDS -lt $deadline ]; do
    status=$(kubectl -n "$ns" get job migrate \
      -o jsonpath='{range .status.conditions[?(@.status=="True")]}{.type}{" "}{end}')
    case " $status " in
      *" Complete "*) return 0 ;;
      *" Failed "*) return 1 ;;
    esac
    sleep 5
  done
  log "migrate Job still running after ${JOB_TIMEOUT}s"
  return 1
}

# A scratch kustomization next to the overlays (kustomize wants relative paths) that layers the
# pinned images over the overlay.
# Traefik trusts X-Forwarded-For only from cni0's address (deploy/k3s/manifests/traefik-config.yaml).
if command -v ip >/dev/null && ip -4 addr show cni0 >/dev/null 2>&1 \
   && ! ip -4 addr show cni0 | grep -q 'inet 10\.42\.0\.1/'; then
  log "WARNING: cni0 is not 10.42.0.1; update Traefik's forwardedHeaders.trustedIPs or every client shares one IP"
fi

work=$(mktemp -d "$k8s_dir/.deploy.XXXXXX")
trap 'rm -rf "$work"' EXIT
[ -z "${DEPLOY_PINS_OUT:-}" ] || : > "$DEPLOY_PINS_OUT"
{
  echo "apiVersion: kustomize.config.k8s.io/v1beta1"
  echo "kind: Kustomization"
  echo "resources:"
  echo "  - ../overlays/$overlay"
  echo "images:"
  for name in api worker renderer web; do pin_image "$name"; done
} > "$work/kustomization.yaml"
kubectl kustomize "$work" > "$work/rendered.yaml"
# worker-io's ScaledObject (#52) needs KEDA's CRD, which the cluster install provides; without it phase 1
# would apply everything else and then fail.
if grep -q '^kind: ScaledObject$' "$work/rendered.yaml" && ! kubectl get crd scaledobjects.keda.sh >/dev/null 2>&1; then
  die "KEDA is not installed (no CRD scaledobjects.keda.sh): kubectl apply -f deploy/k3s/manifests/keda.yaml (docs/k3s.md, \"KEDA\")"
fi

log "phase 1: config, secrets, services and the migrate Job (namespace $ns)"
# A Job's pod template is immutable; a fresh one also makes sure the migration really runs again.
kubectl -n "$ns" delete job migrate --ignore-not-found --wait=true
kubectl apply -f "$work/rendered.yaml" -l 'jobfinder.io/rollout!=app'

log "waiting for the migrate Job (up to ${JOB_TIMEOUT}s)"
if ! wait_for_job; then
  kubectl -n "$ns" logs job/migrate --all-containers --tail=100 >&2 || true
  die "migration failed; the Deployments were not touched"
fi
log "migrations done"

log "phase 2: the Deployments"
kubectl apply -f "$work/rendered.yaml" -l 'jobfinder.io/rollout=app'
for d in $(kubectl -n "$ns" get deployments -l jobfinder.io/rollout=app -o name); do
  kubectl -n "$ns" rollout status "$d" --timeout="$ROLLOUT_TIMEOUT"
done
# A pause of worker-io's autoscaler that a script set to take the app down (jobfinder.io/paused-by,
# pg-lib.sh's keda_pause) ends with the deploy that brings the app back, also when that script failed
# halfway and the runbook says to redeploy. A pause set by hand (no marker) stays.
if grep -q '^kind: ScaledObject$' "$work/rendered.yaml"; then
  paused_by=$(kubectl -n "$ns" get scaledobject worker-io --ignore-not-found \
              -o jsonpath='{.metadata.annotations.jobfinder\.io/paused-by}')
  if [ -n "$paused_by" ]; then
    kubectl -n "$ns" annotate scaledobject worker-io autoscaling.keda.sh/paused-replicas- jobfinder.io/paused-by- >/dev/null
    log "worker-io's autoscaler resumed (paused by $paused_by)"
  fi
fi
kubectl annotate namespace "$ns" --overwrite \
  jobfinder.io/deployed-tag="$tag" jobfinder.io/deployed-commit="${DEPLOY_COMMIT:-unknown}" >/dev/null
log "deployed $tag to $ns"
